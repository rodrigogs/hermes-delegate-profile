"""Service over the capability-router policy.

The Dashboard, CLI and Hermes One sidecar must observe the same ``router.yaml``
and core routing functions.  Read paths are fail-safe: they reload the YAML on
every request, expose only non-secret operational state, and perform
deterministic Stage-0 simulations only — they never call the LLM classifier or
mutate breaker state.

The write paths (:meth:`plan`, :meth:`apply`, :meth:`apply_revert`) edit only
``router.yaml`` — the HOT config the router re-reads per request, so a
successful apply is visible with no restart.  Every write is lint-gated,
optimistic-concurrency guarded (a ``base_hash`` mismatch is a 409-style
conflict, never a silent clobber), serialized behind an instance lock (so two
concurrent applies in one process cannot interleave), written atomically via a
temp-file + ``os.replace`` rename, and revertable from a ``.bak`` snapshot.
``config.yaml`` (Hermes core / compaction) is RESTART-class and is deliberately
NOT reachable here.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from router.blocklist import Blocklist
from router.rules import explain as rules_explain
from router.rules import lint as rules_lint
from router.signals import extract

_DEFAULT_MAX_TASK_CHARS = 8_192

# The only top-level ``router.yaml`` keys an operator may edit through the write
# path. ``fail_safe`` is included (last-resort routing must be editable); every
# other top-level key in a change set is ignored. ``router.yaml`` is HOT
# (re-read per request); ``config.yaml``/compaction is RESTART-class and is not
# routed through here.
_HOT_KEYS = frozenset(
    {"rules", "default", "tiers", "classifier", "fail_safe", "blocklist", "enabled"}
)


def _deep_merge_value(old: Any, new: Any) -> Any:
    """Merge ``new`` over ``old``: dicts recurse, everything else REPLACES.

    Lists (rules, manual_ban, fallback_chain, tier.fallback) and scalars replace
    wholesale so an operator can delete or reorder an entry by sending the full
    new list. An index/union merge would make deletion impossible and could
    corrupt rule order, which ``rules.lint`` shadow-detection depends on.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        result = dict(old)
        for key, value in new.items():
            result[key] = _deep_merge_value(result.get(key), value)
        return result
    return copy.deepcopy(new)


class RouterService:
    """A fail-safe view over one ``router.yaml`` path, with a guarded write path."""

    def __init__(self, config_path: Path, max_task_chars: int = _DEFAULT_MAX_TASK_CHARS):
        self._config_path = Path(config_path)
        self._max_task_chars = max_task_chars
        # Serializes the read-hash -> lint -> snapshot -> write critical section.
        # ThreadingHTTPServer runs one thread per request over a single shared
        # RouterService; without this, two concurrent applies carrying the same
        # base_hash would both pass the drift check and both write.
        self._write_lock = threading.Lock()

    def _load(self) -> Tuple[Dict[str, Any], List[str]]:
        """Return policy plus parse/topology errors instead of raising them."""
        try:
            raw = self._config_path.read_text(encoding="utf-8")
            config = yaml.safe_load(raw) or {}
        except (OSError, yaml.YAMLError) as exc:
            return {}, [f"could not load router config: {exc}"]
        if not isinstance(config, dict):
            return {}, ["router config root must be a mapping"]
        errors = rules_lint(config)
        return config, errors

    def status(self) -> Dict[str, Any]:
        """Compact health snapshot suitable for an operator UI."""
        config, errors = self._load()
        classifier = config.get("classifier", {}) or {}
        blocklist = config.get("blocklist", {}) or {}
        breaker = blocklist.get("auto_breaker", {}) or {}
        return {
            "valid": not errors,
            "validation_errors": errors,
            "enabled": config.get("enabled", False),
            "rules_count": len(config.get("rules", [])),
            "tiers": list(config.get("tiers", {}).keys()),
            "classifier": {
                "model": classifier.get("model", ""),
                "provider": classifier.get("provider", ""),
            },
            "breaker_enabled": bool(breaker.get("enabled", False)),
        }

    def policy(self) -> Dict[str, Any]:
        """Return only the declarative, non-secret policy material."""
        config, _errors = self._load()
        rules = config.get("rules", [])
        return {
            "rules": [
                {
                    "id": rule.get("id"),
                    "status": rule.get("status", "stable"),
                    "when": rule.get("when", {}),
                    "then": rule.get("then", {}),
                }
                for rule in rules
                if isinstance(rule, dict)
            ],
            "default": config.get("default", {}),
            "tiers": config.get("tiers", {}),
            "fail_safe": config.get("fail_safe", {}),
        }

    def blocklist(self) -> Dict[str, Any]:
        """Return manual bans and the real persisted breaker state."""
        config, _errors = self._load()
        blocklist = Blocklist(config)
        return {
            "manual_bans": blocklist.manual_bans(),
            "fallback_chain": blocklist.fallback_chain(),
            "breaker_enabled": blocklist.breaker_enabled(),
            "breaker_cooldowns": blocklist.breaker_status(),
        }

    def liveness(self) -> Dict[str, Any]:
        """Compose policy references with manual-ban and breaker health.

        This is deliberately observational: it reloads policy and persisted
        breaker state but never records, probes, or otherwise mutates either.
        Every returned target has one of four operator-facing states:
        ``alive``, ``degraded``, ``quota_exhausted``, or ``dead``.
        """
        try:
            config, errors = self._load()
            blocklist = Blocklist(config)
            references = self._policy_references(config, blocklist.fallback_chain())
            manual_bans = blocklist.manual_bans()
            breaker_status = {
                entry.get("model_key"): entry
                for entry in blocklist.breaker_status()
                if isinstance(entry, dict) and isinstance(entry.get("model_key"), str)
            }

            models: List[Dict[str, Any]] = []
            for model, provider in references:
                key = f"{model}@{provider}"
                breaker = breaker_status.get(key, {})
                if self._is_manually_banned(manual_bans, model, provider):
                    state = "dead"
                elif breaker.get("state") == "OPEN" and breaker.get(
                    "last_failure_kind"
                ) == "quota_exhausted":
                    state = "quota_exhausted"
                elif breaker.get("state") in ("OPEN", "HALF_OPEN"):
                    state = "degraded"
                else:
                    state = "alive"
                models.append(
                    {
                        "model_key": key,
                        "model": model,
                        "provider": provider,
                        "state": state,
                        "breaker": breaker,
                    }
                )

            worst = max((entry["state"] for entry in models), key=self._liveness_rank, default="alive")
            result: Dict[str, Any] = {"models": models, "worst": worst}
            if errors:
                result["validation_errors"] = errors
            return result
        except Exception as exc:
            return {
                "models": [],
                "worst": "degraded",
                "error": f"could not compose liveness: {exc}",
            }

    @staticmethod
    def _policy_references(
        config: Dict[str, Any], fallback_chain: List[str]
    ) -> List[Tuple[str, str]]:
        """Return unique ``(model, provider)`` pairs declared by policy."""
        references: List[Tuple[str, str]] = []

        def add(item: Any) -> None:
            if not isinstance(item, dict):
                return
            model = item.get("model")
            provider = item.get("provider")
            if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider:
                return
            pair = (model, provider)
            if pair not in references:
                references.append(pair)

        add(config.get("classifier", {}))
        tiers = config.get("tiers", {})
        if isinstance(tiers, dict):
            for tier in tiers.values():
                add(tier)
                if isinstance(tier, dict):
                    for fallback in tier.get("fallback", []) or []:
                        add(fallback)
        fail_safe = config.get("fail_safe", {})
        add(fail_safe)
        if isinstance(fail_safe, dict):
            for fallback in fail_safe.get("fallback", []) or []:
                add(fallback)

        # The historical fallback chain stores model names. Map each one to
        # every provider already declared elsewhere in policy; no provider is
        # invented for an unknown chain entry.
        known_models = {model for model, _provider in references}
        for fallback in fallback_chain:
            if isinstance(fallback, dict):
                add(fallback)
            elif isinstance(fallback, str) and fallback in known_models:
                continue
        return sorted(references)

    @staticmethod
    def _is_manually_banned(
        bans: List[Dict[str, str]], model: str, provider: str
    ) -> bool:
        """Match manual bans with the same model/provider semantics as Blocklist."""
        for ban in bans:
            if not isinstance(ban, dict):
                continue
            ban_model = str(ban.get("model", ""))
            ban_provider = str(ban.get("provider", ""))
            if ban_model and ban_model.lower() != model.lower():
                continue
            if not ban_provider or ban_provider.lower() == provider.lower():
                return True
        return False

    @staticmethod
    def _liveness_rank(state: str) -> int:
        return {"alive": 0, "degraded": 1, "quota_exhausted": 2, "dead": 3}.get(
            state, 1
        )

    def explain(self, task: str) -> Dict[str, Any]:
        """Run a deterministic Stage-0 dry-run without invoking a classifier."""
        task = task.strip()
        if not task:
            raise ValueError("task is required")
        if len(task) > self._max_task_chars:
            raise ValueError(f"task exceeds {self._max_task_chars} characters")

        config, errors = self._load()
        if errors:
            raise ValueError("router policy is invalid")
        decision = rules_explain(
            task,
            extract(task),
            False,
            config.get("rules", []),
            config.get("default", {}),
            config.get("tiers", {}),
        )
        requires_classifier = decision.get("output", {}).get("action") == "classify"
        return {
            "mode": "deterministic_dry_run",
            "requires_classifier": requires_classifier,
            "decision": decision,
        }

    def lint(self) -> Dict[str, Any]:
        """Expose the same validation data shown by :meth:`status`."""
        _config, errors = self._load()
        return {"valid": not errors, "errors": errors}

    # ------------------------------------------------------------------
    # Write path (router.yaml HOT edits only; lint-gated, atomic, revertable)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_fail_safe(config: Dict[str, Any]) -> List[str]:
        """Minimal structural check for ``fail_safe``.

        ``rules.lint`` never inspects ``fail_safe`` (it validates default/tiers/
        rules only), so a malformed last-resort target — the route every
        fall-through request lands on — would otherwise be written unchecked.
        Guard the shape here before it can reach the hot file.
        """
        if "fail_safe" not in config:
            return []
        fail_safe = config.get("fail_safe")
        if not isinstance(fail_safe, dict):
            return ["fail_safe must be a mapping"]
        errors: List[str] = []
        for field in ("model", "provider"):
            value = fail_safe.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"fail_safe.{field} must be a non-empty string")
        fallback = fail_safe.get("fallback", [])
        if fallback and not isinstance(fallback, list):
            errors.append("fail_safe.fallback must be a list")
        return errors

    def _merge_hot(self, changes: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Read current config and merge only the allowlisted top-level keys.

        Returns ``(current, merged)``. Any key outside :data:`_HOT_KEYS` in
        ``changes`` is ignored — never written.
        """
        # plan()/apply() validate that changes is a mapping before calling here.
        current = self._read_config_dict()
        merged = dict(current)
        for key, value in changes.items():
            if key not in _HOT_KEYS:
                continue
            merged[key] = _deep_merge_value(current.get(key), value)
        return current, merged

    def _read_config_bytes(self) -> bytes:
        """Return the exact on-disk bytes (hash basis for optimistic concurrency)."""
        return self._config_path.read_bytes()

    def _read_config_dict(self) -> Dict[str, Any]:
        """Parse the current config, raising a clear error on malformed YAML."""
        raw = self._read_config_bytes()
        config = yaml.safe_load(raw) or {}
        if not isinstance(config, dict):
            raise ValueError("router config root must be a mapping")
        return config

    @staticmethod
    def _hash_bytes(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def _lint_merged(self, merged: Dict[str, Any]) -> List[str]:
        """Full pre-write validation: rule/tier lint plus the fail_safe guard."""
        return list(rules_lint(merged)) + self._validate_fail_safe(merged)

    def plan(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Preview an edit: merge, lint, diff, and hash — WITHOUT writing.

        The returned ``base_hash`` pins the on-disk state this plan was computed
        against; :meth:`apply` refuses to write if the file has drifted since.
        """
        if not isinstance(changes, dict):
            raise ValueError("changes must be a mapping")
        try:
            current_raw = self._read_config_bytes()
            current, merged = self._merge_hot(changes)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            return {"valid": False, "errors": [f"could not read router config: {exc}"],
                    "diff": "", "preview": {}, "policy": {}, "base_hash": ""}
        errors = self._lint_merged(merged)
        before = yaml.safe_dump(current, sort_keys=False)
        after = yaml.safe_dump(merged, sort_keys=False)
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="router.yaml (current)",
                tofile="router.yaml (proposed)",
            )
        )
        return {
            "valid": not errors,
            "errors": errors,
            "diff": diff,
            "preview": merged,
            "policy": merged,
            "base_hash": self._hash_bytes(current_raw),
        }

    def apply(self, base_hash: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Commit an edit to ``router.yaml`` under optimistic concurrency.

        Serialized behind :attr:`_write_lock`. Refuses (409-style ``conflict``)
        if the file changed since ``base_hash`` was computed, refuses if the
        merged result fails lint, snapshots the prior bytes to ``.bak``, and
        writes atomically. Because the router re-reads per request, the change
        is live immediately with no restart.
        """
        if not isinstance(changes, dict):
            raise ValueError("changes must be a mapping")
        with self._write_lock:
            try:
                current_raw = self._read_config_bytes()
            except OSError as exc:
                return {"ok": False, "errors": [f"could not read router config: {exc}"]}
            current_hash = self._hash_bytes(current_raw)
            if base_hash != current_hash:
                return {"ok": False, "conflict": True, "base_hash": current_hash}
            try:
                _current, merged = self._merge_hot(changes)
            except (yaml.YAMLError, ValueError) as exc:
                return {"ok": False, "errors": [f"could not parse router config: {exc}"]}
            errors = self._lint_merged(merged)
            if errors:
                return {"ok": False, "errors": errors}
            # Snapshot the exact prior bytes, then write the merged config.
            self._atomic_write_bytes(self._backup_path(), current_raw)
            new_raw = yaml.safe_dump(merged, sort_keys=False).encode("utf-8")
            self._atomic_write_bytes(self._config_path, new_raw)
            # Hash the exact bytes we wrote — not a re-read, which could fail
            # transiently (returning ok with an empty hash) and, worse, differ
            # from the file and cause the next plan()'s base_hash to false-409.
            return {"ok": True, "base_hash": self._hash_bytes(new_raw)}

    def apply_revert(self) -> Dict[str, Any]:
        """Restore the last ``.bak`` snapshot atomically. No snapshot -> no-op."""
        with self._write_lock:
            backup = self._backup_path()
            try:
                snapshot = backup.read_bytes()
            except OSError:
                return {"ok": False, "error": "no snapshot"}
            self._atomic_write_bytes(self._config_path, snapshot)
            # Hash the exact restored bytes, not a re-read (same rationale as apply).
            return {"ok": True, "reverted": True,
                    "base_hash": self._hash_bytes(snapshot)}

    def _backup_path(self) -> Path:
        return self._config_path.with_suffix(self._config_path.suffix + ".bak")

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

        Mirrors ``Blocklist._save_state``: a partial file can never be observed
        because the rename is atomic. The caller serializes concurrent writers.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".yaml", prefix="router-", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
