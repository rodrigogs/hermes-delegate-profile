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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .breaker import BreakerState

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    """Return the plugin state directory for breaker-state.json."""
    home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    return Path(home) / "delegate-profile" / "state"


def _state_path() -> Path:
    return _state_dir() / "breaker-state.json"


class Blocklist:
    """Fail-closed blocklist with manual bans, fallback chain, and auto-breaker.

    The config deny rows fire independently of any mutable state file.
    If breaker state is missing/corrupt, cooldowns are treated as empty —
    but config deny rows still fire. The blocklist never fails open.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._manual_bans: List[Dict[str, str]] = []
        self._fallback_chain: List[str] = []

        bl_conf = config.get("blocklist", {})
        self._manual_bans = bl_conf.get("manual_ban", [])
        self._fallback_chain = bl_conf.get("fallback_chain", [])

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
            ban_model = ban.get("model", "")
            ban_provider = ban.get("provider", "")
            if self._match(ban_model, ban_provider, model, provider or ""):
                return True

        # Check breaker cooldowns
        if self._breaker_enabled:
            key = f"{model}@{provider}" if provider else model
            if self._breaker.is_blocked(key, time.time()):
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
        """Record a failure event for a model. Returns True if breaker tripped."""
        if not self._breaker_enabled:
            return False
        key = f"{model}@{provider}" if provider else model
        tripped = self._breaker.record(key, failure_kind, time.time())
        if tripped:
            self._save_state()
        return tripped

    def record_success(self, model: str, provider: str) -> None:
        """Record a successful call — resets breaker if in HALF_OPEN."""
        if not self._breaker_enabled:
            return
        key = f"{model}@{provider}" if provider else model
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

        If the model matches the ban, block regardless of provider
        (fail-closed — a banned model is banned).
        An empty ban provider matches any provider.
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
