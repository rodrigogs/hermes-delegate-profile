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
