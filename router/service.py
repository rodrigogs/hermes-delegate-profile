"""Read-only service over the capability-router policy.

The Dashboard, CLI and Hermes One sidecar must observe the same ``router.yaml``
and core routing functions.  This service is intentionally read-only: it
reloads the YAML on every request, exposes only non-secret operational state,
and performs deterministic Stage-0 simulations only — it never calls the LLM
classifier or mutates breaker state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from router.blocklist import Blocklist
from router.rules import explain as rules_explain
from router.rules import lint as rules_lint
from router.signals import extract

_DEFAULT_MAX_TASK_CHARS = 8_192


class RouterService:
    """A fail-safe, read-only view over one ``router.yaml`` path."""

    def __init__(self, config_path: Path, max_task_chars: int = _DEFAULT_MAX_TASK_CHARS):
        self._config_path = Path(config_path)
        self._max_task_chars = max_task_chars

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
