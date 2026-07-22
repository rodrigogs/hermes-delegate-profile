"""Capability Router dashboard plugin — backend API routes.

Mounted at /api/plugins/capability-router/ by the dashboard plugin system.
Imports the pure-core router from the delegate-profile plugin.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

# The router modules live in the parent plugin directory
_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # dashboard/ -> delegate-profile/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import yaml
from fastapi import APIRouter, Query

from router.signals import extract
from router.rules import explain as rules_explain, lint as rules_lint
from router.blocklist import Blocklist
from router.decision_log import DecisionLog

router = APIRouter()

_CONFIG_PATH = _PLUGIN_DIR / "router.yaml"
_log = DecisionLog()


def _load_config() -> Dict[str, Any]:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


# ── API ──────────────────────────────────────────────────────────────

@router.get("/status")
async def api_status():
    c = _load_config()
    bl = c.get("blocklist", {})
    ab = bl.get("auto_breaker", {})
    return {
        "enabled": c.get("enabled", False),
        "rules_count": len(c.get("rules", [])),
        "tiers": list(c.get("tiers", {}).keys()),
        "banned_models": [b["model"] for b in bl.get("manual_ban", [])],
        "classifier_model": c.get("classifier", {}).get("model", ""),
        "breaker_enabled": ab.get("enabled", False) if isinstance(ab, dict) else False,
    }


@router.get("/explain")
async def api_explain(task: str = Query(..., description="Task to classify")):
    c = _load_config()
    fv = extract(task)
    result = rules_explain(task, fv, False, c.get("rules", []),
                          c.get("default", {}), c.get("tiers", {}))
    _log.record(result["cause"], result["output"],
                matched_rule_id=result.get("matched_rule_id"),
                task_preview=task[:120])
    return result


@router.post("/lint")
async def api_lint():
    c = _load_config()
    errors = rules_lint(c)
    return {"valid": len(errors) == 0, "errors": errors}


@router.get("/blocklist")
async def api_blocklist():
    c = _load_config()
    bl = Blocklist(c)
    return {
        "manual_bans": bl.manual_bans(),
        "fallback_chain": bl.fallback_chain(),
        "breaker_enabled": bl.breaker_enabled(),
        "breaker_cooldowns": bl.breaker_status(),
    }


@router.get("/log")
async def api_log(tail: int = Query(50, ge=1, le=500)):
    return {"entries": _log.tail(tail)}


@router.get("/rules")
async def api_rules():
    c = _load_config()
    return {
        "rules": [{"id": r.get("id"), "status": r.get("status", "stable"),
                   "when": r.get("when", {}), "then": r.get("then", {})}
                  for r in c.get("rules", [])],
        "default": c.get("default", {}),
        "tiers": c.get("tiers", {}),
    }
