"""Route hook — the adapter: only Hermes-coupled code.

Wires Stage 0 (blocklist + signals + rules) → Stage 1 (classifier)
→ delegate_profile(). One decision path, one cause= log, one call.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from router.signals import extract
from router.rules import match, explain as rules_explain, lint as rules_lint
from router.blocklist import Blocklist
from router.classify import Classifier
from router.cache import Cache, SessionPin
from router.decision_log import DecisionLog


def route(
    task: str,
    config: Dict[str, Any],
    *,
    requested_model: str = "",
    requested_provider: str = "",
    classify_fn: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    blocklist: Optional[Blocklist] = None,
    cache: Optional[Cache] = None,
    session_pin: Optional[SessionPin] = None,
    decision_log: Optional[DecisionLog] = None,
) -> Dict[str, Any]:
    """Run the full routing pipeline.

    Returns {profile, model?, provider?, cause, ...} — always a valid
    delegation target.
    """
    bl = blocklist or Blocklist(config)
    cch = cache or Cache()
    pin = session_pin or SessionPin()
    dlog = decision_log or DecisionLog()

    # --- Stage 0: blocklist pre-filter ---
    blocked = bl.is_blocked(requested_model, requested_provider)
    if blocked:
        fallback_model = bl.fallback_for(requested_model)
        result = {"deny": True}
        if fallback_model:
            result["fallback_model"] = fallback_model
        dlog.record("blocklist_veto", result, task_preview=task[:120])
        return result

    # --- Stage 0: signal extraction ---
    features = extract(task)

    # --- Stage 0: rule matching ---
    rules = config.get("rules", [])
    default = config.get("default", {})
    tiers = config.get("tiers", {})

    output, rule_id = match(features, blocked, rules, default, tiers)

    # If rule matched and gave concrete output → route now
    if rule_id is not None and "action" not in output:
        # Concrete route — check session pin upward-only ratchet
        if pin.is_set() and output.get("model"):
            out_model = output.get("model", "")
            pin_tier = pin.tier or ""
            tier_cfg = tiers.get(pin_tier, {})
            pin_model = tier_cfg.get("model", "")
            # Only break pin upward for hard verbs (already handled by rules)
            # For other rules, respect the pin floor
            if pin_model and out_model != pin_model:
                # Check if pin tier is higher
                tier_order = {"glm-5.2-fast": 0, "glm-5.2": 1,
                             "claude-sonnet": 2, "claude-opus": 3}
                if tier_order.get(out_model, 0) < tier_order.get(pin_model, 0):
                    # Pin is higher — use pin's model
                    output["model"] = pin_model
                    if "provider" in tier_cfg:
                        output["provider"] = tier_cfg["provider"]
                    dlog.record("session_pin", output, matched_rule_id=rule_id,
                               task_preview=task[:120])

        dlog.record(
            _cause_from_rule(rule_id, output), output,
            matched_rule_id=rule_id, task_preview=task[:120],
        )
        return output

    # --- Stage 1: classifier ---
    if output.get("action") == "classify" or rule_id is None:
        # Check cache first
        cached = cch.get(task)
        if cached:
            dlog.record("classifier", cached, task_preview=task[:120])
            # Return with tier resolved
            return _resolve_output(cached, output, tiers)

        if classify_fn is None:
            # No classifier available → fail-safe
            fs = config.get("fail_safe", {})
            result = {
                "profile": fs.get("profile", "coder"),
                "model": fs.get("model", "claude-opus"),
                "provider": fs.get("provider", "anthropic"),
            }
            dlog.record("fail_safe_strong", result, task_preview=task[:120])
            return result

        # Call the classifier
        try:
            classification = classify_fn(task, features)
            tier = classification.get("tier", "T4")
            confidence = classification.get("confidence", "med")

            # Safety ratchet
            classifier = Classifier(config)
            final_tier, tier_cfg = classifier.safety_ratchet(tier, confidence)

            # Cache the result
            cch.set(task, {"tier": final_tier, **tier_cfg})

            # Set session pin
            pin.set(final_tier)

            # Merge profile from output (role axis) with model from classifier
            result = dict(output)
            result.pop("action", None)
            result["model"] = tier_cfg.get("model")
            result.setdefault("provider", tier_cfg.get("provider"))
            if "profile" not in result:
                result["profile"] = "coder"

            dlog.record("classifier", result, task_preview=task[:120])
            return result

        except Exception:
            # Classifier failed → fail-safe
            fs = config.get("fail_safe", {})
            result = {
                "profile": fs.get("profile", "coder"),
                "model": fs.get("model", "claude-opus"),
                "provider": fs.get("provider", "anthropic"),
            }
            dlog.record("fail_safe_strong", result, task_preview=task[:120])
            return result

    # Fail-safe fallback
    fs = config.get("fail_safe", {})
    result = {
        "profile": fs.get("profile", "coder"),
        "model": fs.get("model", "claude-opus"),
        "provider": fs.get("provider", "anthropic"),
    }
    dlog.record("fail_safe_strong", result, task_preview=task[:120])
    return result


def _cause_from_rule(rule_id: str, output: Dict[str, Any]) -> str:
    """Map rule to cause label."""
    if output.get("deny"):
        return "blocklist_veto"
    if "keyword" in rule_id.lower() or "review" in rule_id.lower():
        return "keyword_match"
    if "size" in rule_id.lower() or "trivial" in rule_id.lower():
        return "has_code_rule"
    return "default_fallthrough"


def _resolve_output(
    classifier_result: Dict[str, Any],
    rule_output: Dict[str, Any],
    tiers: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    """Merge classifier result with rule output."""
    result = dict(rule_output)
    result.pop("action", None)
    if "model" in classifier_result:
        result["model"] = classifier_result["model"]
    if "provider" in classifier_result:
        result.setdefault("provider", classifier_result["provider"])
    if "profile" not in result:
        result["profile"] = "coder"
    return result
