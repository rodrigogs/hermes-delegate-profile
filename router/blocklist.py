"""Blocklist pre-filter — the first stage of the routing pipeline.

Owns the only mutable ban state. Unions operator manual bans with
auto-breaker cooldowns into a single boolean `blocked_model`.
The pure rule engine only reads this boolean — never writes state.

v2: auto-breaker enabled — BreakerState monitors delegate_profile outcomes
and auto-blocks models that repeatedly stall.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .breaker import BreakerState

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    """Return the plugin state directory for breaker-state.json.

    Same ``profiles/<name>`` peel as ``durable_decision_log.routes_path``:
    the breaker state is written by the delegate_profile plugin process
    (whose HERMES_HOME is profile-scoped per delegation) and read back by
    the sidecar (pinned to one profile), so a profile-scoped path would
    split the two — a rail failing for trama-engineer would keep getting
    traffic from coder because its cooldown lives in a file the other
    profile never reads and the breaker never accumulates.
    """
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home / "hermes-smart-router" / "state"


def _state_path() -> Path:
    return _state_dir() / "breaker-state.json"


# Process-wide lock guarding the load -> mutate -> save critical section on
# breaker-state.json. ``_record_breaker_outcome`` constructs a FRESH Blocklist
# per delegate_profile call, so without serialization N concurrent writers
# each load the same on-disk state, mutate their private copy, and clobber
# each other on the atomic rename — silently dropping failure events so the
# breaker never trips. The lock is shared across all Blocklist instances in
# this process; cross-process writers (the sidecar is read-only) are not a
# concern here. Keyed by resolved path so distinct HERMES_HOME values don't
# alias. INSERT-ONLY and never pruned: every call site holds the lock through the
# whole load -> mutate -> save section, so the dict grows by one entry per distinct
# state path and that is negligible. It is NOT weakref-able, which this comment
# claimed — a `WeakValueDictionary` would be wrong here anyway, since the value is
# the lock itself and dropping it while a waiter holds a reference is the bug the
# registry exists to prevent.
_BLOCKER_LOCKS: "Dict[str, threading.Lock]" = {}
_BLOCKER_LOCKS_GUARD = threading.Lock()


def _state_lock(path: Path) -> threading.Lock:
    """Return the process-wide Lock guarding RMW on ``path``."""
    key = str(path.resolve())
    with _BLOCKER_LOCKS_GUARD:
        lock = _BLOCKER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _BLOCKER_LOCKS[key] = lock
    return lock


def _ban_row(ban: Any) -> Optional[Tuple[str, str]]:
    """``(model, provider)`` from one ``manual_ban`` row, or None if unusable.

    Rows come from the operator's YAML, so every field is untrusted shape. A bare
    string in the list, or a row whose ``model`` is a number, used to raise
    ``AttributeError`` out of the match loop — which took ALL routing down and
    unenforced every OTHER ban in the list along with it.
    None means "this row cannot ban anything", never "nothing is banned": the loop
    skips it and keeps evaluating the rows that ARE well formed. ``lint_warnings``
    reports the row so it is not silently ignored.
    """
    if not isinstance(ban, dict):
        return None
    model = ban.get("model", "")
    provider = ban.get("provider", "")
    if not isinstance(model, str) or not isinstance(provider, str):
        return None
    return model, provider


class Blocklist:
    """Fail-closed blocklist with manual bans, fallback chain, and auto-breaker.

    The config deny rows fire independently of any mutable state file.
    If breaker state is missing/corrupt, cooldowns are treated as empty —
    but config deny rows still fire. The blocklist never fails open.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._manual_bans: List[Dict[str, str]] = []
        self._fallback_chain: List[str] = []

        # EVERY read here is shape-guarded, and the reason is a fail-OPEN on the
        # one component whose whole job is to refuse.
        #
        # `config.get("blocklist", {}).get(...)` raised AttributeError for
        # `blocklist:` with nothing under it (None), `blocklist: off`, or a list.
        # Measured on the shipped policy with `blocklist: off` appended:
        # `rules.lint` returned [] — so `/status` said `valid: True` AND the write
        # gate ACCEPTED it, meaning the operator's own console would persist it —
        # while `adapter.route` raised, `_route_task` swallowed it at
        # logger.debug, every delegation came back `bad_args`, and every manual
        # ban was unenforced. A blocklist that cannot be constructed is a
        # blocklist that blocks nothing.
        #
        # `lint` now hard-errors on these coarse shapes (see
        # `rules._lint_blocklist_shape`), so an operator cannot write one. This
        # guard is the second half: the config is HOT and a file already on disk
        # must still route.
        bl_conf = config.get("blocklist")
        if not isinstance(bl_conf, dict):
            bl_conf = {}
        bans = bl_conf.get("manual_ban")
        self._manual_bans = bans if isinstance(bans, list) else []
        chain = bl_conf.get("fallback_chain")
        self._fallback_chain = [
            model for model in chain if isinstance(model, str)
        ] if isinstance(chain, list) else []

        # Auto-breaker config
        ab = bl_conf.get("auto_breaker", {})
        if isinstance(ab, dict):
            self._breaker_enabled = ab.get("enabled", False)
        else:
            self._breaker_enabled = False

        self._breaker = BreakerState(ab if isinstance(ab, dict) else {})

        # Load persisted state
        if self._breaker_enabled:
            self._load_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_blocked(self, model: Optional[str], provider: Optional[str]) -> bool:
        """Return True if (model, provider) is blocked.

        Checks manual bans first, then breaker cooldowns. Fail-closed.
        """
        if not model:
            return False

        # Check manual bans — these always fire
        for ban in self._manual_bans:
            row = _ban_row(ban)
            if row is None:
                continue
            if self._match(row[0], row[1], model, provider or ""):
                return True

        # Check breaker cooldowns
        if self._breaker_enabled:
            key = f"{model}@{provider}" if provider else model
            with _state_lock(_state_path()):
                # ``is_blocked`` is not a pure read: an expired OPEN breaker
                # transitions to HALF_OPEN and consumes the single probe slot.
                # ``delegate_profile`` constructs a fresh Blocklist for each
                # call, so the transition must be serialized and persisted here
                # or every concurrent/fresh caller reloads OPEN-expired and all
                # of them get through as probes.
                self._load_state()
                before = self._breaker.to_dict()
                blocked = self._breaker.is_blocked(key, time.time())
                if self._breaker.to_dict() != before:
                    self._save_state()
            if blocked:
                return True

        return False

    def would_block(self, model: Optional[str], provider: Optional[str]) -> bool:
        """:meth:`is_blocked`'s answer, WITHOUT consuming a breaker probe slot.

        For diagnostics and status surfaces. ``router explain`` and ``router chain``
        answer "why did this route there" — asking that question must not remove
        capacity, and it did: :meth:`is_blocked` transitions an expired OPEN entry
        to HALF_OPEN and burns the single probe, and since HALF_OPEN is only left by
        a recorded outcome, a rail nothing then dispatched to stayed excluded for
        good. See :meth:`breaker.BreakerState.would_block`.

        Manual bans are checked identically — they carry no state and no slot.
        Reads the freshest state off disk under the lock so the answer is not the
        snapshot taken at construction, but never writes.
        """
        if not model:
            return False

        for ban in self._manual_bans:
            row = _ban_row(ban)
            if row is None:
                continue
            if self._match(row[0], row[1], model, provider or ""):
                return True

        if self._breaker_enabled:
            key = f"{model}@{provider}" if provider else model
            with _state_lock(_state_path()):
                self._load_state()
                if self._breaker.would_block(key, time.time()):
                    return True

        return False

    def fallback_for(self, model: str) -> Optional[str]:
        """Return the next model in the fallback chain, or None."""
        try:
            idx = self._fallback_chain.index(model)
            if idx + 1 < len(self._fallback_chain):
                return self._fallback_chain[idx + 1]
        except ValueError:
            pass
        return None

    def record_failure(
        self,
        model: str,
        provider: str,
        failure_kind: str,
    ) -> bool:
        """Record a failure event for a model. Returns True if breaker tripped.

        Holds the process-wide state lock across load -> mutate -> save so that
        concurrent ``_record_breaker_outcome`` calls (each in its own fresh
        Blocklist) accumulate rather than clobber. The on-trip return value is
        preserved for the existing API/contracts; non-tripping events are now
        persisted too, otherwise the breaker could never reach the threshold
        under concurrent failures.
        """
        if not self._breaker_enabled:
            return False
        key = f"{model}@{provider}" if provider else model
        with _state_lock(_state_path()):
            # Reload the freshest on-disk state under the lock so we merge onto
            # it rather than onto the snapshot captured at construction time.
            self._load_state()
            tripped = self._breaker.record(key, failure_kind, time.time())
            self._save_state()
        return tripped

    def record_success(self, model: str, provider: str) -> None:
        """Record a successful call — resets breaker if in HALF_OPEN.

        Same lock discipline as :meth:`record_failure`: load -> mutate -> save
        is atomic against other in-process writers.
        """
        if not self._breaker_enabled:
            return
        key = f"{model}@{provider}" if provider else model
        with _state_lock(_state_path()):
            self._load_state()
            self._breaker.record_success(key, time.time())
            self._save_state()

    def manual_bans(self) -> List[Dict[str, str]]:
        """Return the current manual ban list (for CLI display)."""
        return list(self._manual_bans)

    def fallback_chain(self) -> List[str]:
        """Return the current fallback chain (for CLI display)."""
        return list(self._fallback_chain)

    def breaker_enabled(self) -> bool:
        """Return whether the auto-breaker is enabled."""
        return self._breaker_enabled

    def breaker_status(self) -> List[Dict[str, Any]]:
        """Return breaker state for CLI display."""
        if not self._breaker_enabled:
            return []
        return self._breaker.blocked_entries(time.time())

    def breaker_policy(self) -> Dict[str, Any]:
        """The breaker's effective thresholds and failure weights.

        Delegated rather than assembled here: the numbers in force live on the
        BreakerState that `record` consults, and `_load_state` REBUILDS that object
        from the persisted file — so reading them off anything else would be reading
        a copy that a state load can silently replace.
        """
        return self._breaker.policy()

    def breaker_state_dict(self) -> Dict[str, Any]:
        """Return full breaker state dict (for serialization)."""
        return self._breaker.to_dict()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _match(
        ban_model: str,
        ban_provider: str,
        model: str,
        provider: str,
    ) -> bool:
        """Check if model/provider matches a ban entry.

        The rules, in the order they are applied:

          * An empty ban ``model`` matches EVERY model.
          * An empty ban ``provider`` bans the model on every rail.
          * An empty QUERIED ``provider`` is fail-closed: a model banned on any
            named rail answers True. This is why ``adapter`` must never widen a
            lookup with ``is_blocked(model, "")`` — for a provider-scoped ban that
            call is strictly MORE blocking than the truth.
          * Otherwise both must match, case-insensitively.

        The docstring used to say "if the model matches the ban, block regardless
        of provider (fail-closed — a banned model is banned)", which is NOT what
        the last line does: a provider-scoped ban does not block the model on
        another rail. That reading is deliberate and pinned by
        ``test_banned_model_wrong_provider`` — a contributor "simplifying" the code
        to match the old sentence would start refusing rails the operator never
        named.
        """
        model_match = not ban_model or ban_model.lower() == model.lower()
        if not model_match:
            return False
        # Model matches — block unless provider is specifically non-matching
        if not ban_provider:
            return True  # ban all providers for this model
        if not provider:
            return True  # fail-closed: if model banned anywhere, block it
        return ban_provider.lower() == provider.lower()

    def _load_state(self) -> None:
        """Load breaker state from JSON file."""
        path = _state_path()
        try:
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            ab_config = {
                "threshold": self._breaker._threshold,
                "window_seconds": self._breaker._window_s,
                "base_cooldown_seconds": self._breaker._base_cooldown_s,
                "max_cooldown_seconds": self._breaker._max_cooldown_s,
                "backoff_multiplier": self._breaker._backoff_mult,
            }
            self._breaker = BreakerState.from_dict(data, ab_config)
        except json.JSONDecodeError:
            logger.warning(
                "breaker-state.json is corrupt — using empty cooldowns (fail-closed)"
            )
            # Keep the empty breaker — fail-closed on corrupt state
        except Exception as exc:
            logger.warning(
                "Failed to load breaker-state.json: %s — using empty cooldowns",
                exc,
            )

    def _save_state(self) -> None:
        """Persist breaker state atomically (temp file + rename)."""
        path = _state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self._breaker.to_dict()
            # Atomic write: write to temp file, then rename
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json",
                prefix="breaker-state-",
                dir=str(path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True)
                os.replace(tmp_path, str(path))
            except Exception:
                # Clean up temp file on write failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning("Failed to save breaker-state.json: %s", exc)
