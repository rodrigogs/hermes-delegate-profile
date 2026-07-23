"""Route hook — the adapter: only Hermes-coupled code.

Wires Stage 0 (blocklist + signals + rules) → Stage 1 (classifier)
→ delegate_profile(). One decision path, one cause= log, one call.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

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
            output, pin_applied = _apply_session_floor(output, pin, tiers)
            if pin_applied:
                dlog.record("session_pin", output, matched_rule_id=rule_id,
                           task_preview=task[:120])
                return output

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
            result = _resolve_output(cached, output, tiers)
            result, pin_applied = _apply_session_floor(
                result, pin, tiers, output_tier=cached.get("tier"),
            )
            dlog.record(
                "session_pin" if pin_applied else "classifier",
                result,
                task_preview=task[:120],
            )
            return result

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

            # SessionPin is an upward-only floor. A subsequent classifier
            # answer may be lower, but it must not downgrade this session.
            pin.set(final_tier)
            effective_tier = pin.tier or final_tier
            if effective_tier != final_tier:
                tier_cfg = dict(tiers.get(effective_tier, tier_cfg))

            # Cache the effective result, not the raw classifier answer.
            cch.set(task, {"tier": effective_tier, **tier_cfg})

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



_TIER_ORDER = {"T1": 0, "T2": 1, "T3": 2, "T4": 3}


def _apply_session_floor(
    output: Dict[str, Any],
    pin: SessionPin,
    tiers: Dict[str, Dict[str, Any]],
    *,
    output_tier: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Apply a SessionPin floor to a resolved routing result.

    A model can appear in direct-rule, classifier, or cache output. The
    session guarantee is independent of that source: whenever both capability
    tiers are known and the candidate is below the pin, return the pin tier.
    Unknown concrete models are left untouched because their relative capacity
    cannot be determined safely.
    """
    pin_tier = pin.tier
    pin_cfg = tiers.get(pin_tier or "", {})
    pin_model = pin_cfg.get("model")
    if not pin_tier or not pin_model:
        return output, False

    if output_tier is None:
        output_model = output.get("model")
        output_tier = next(
            (
                name for name, cfg in tiers.items()
                if cfg.get("model") == output_model
            ),
            None,
        )

    # A concrete model outside the policy table has no comparable capability
    # rank. Preserve it rather than guessing it is below the current floor.
    if output_tier not in _TIER_ORDER or pin_tier not in _TIER_ORDER:
        return output, False
    if _TIER_ORDER[output_tier] >= _TIER_ORDER[pin_tier]:
        return output, False

    result = dict(output)
    result["model"] = pin_model
    if "provider" in pin_cfg:
        result["provider"] = pin_cfg["provider"]
    return result, True


def _cause_from_rule(rule_id: str, output: Dict[str, Any]) -> str:
    """Map rule to cause label."""
    if output.get("deny"):
        return "blocklist_veto"
    if "keyword" in rule_id.lower() or "review" in rule_id.lower():
        return "keyword_match"
    if "size" in rule_id.lower():
        return "size_rule"
    if "code" in rule_id.lower() or "trivial" in rule_id.lower():
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
