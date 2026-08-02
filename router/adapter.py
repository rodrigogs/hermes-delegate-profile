"""Route hook — the adapter: only Hermes-coupled code.

Wires Stage 0 (blocklist + signals + rules) → Stage 1 (classifier)
→ delegate_profile(). One decision path, one cause= log, one call.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .signals import extract
from .rules import match, explain as rules_explain, lint as rules_lint
from .blocklist import Blocklist
from .classify import Classifier
from .cache import Cache, SessionPin
from .decision_log import DecisionLog


def _copy_fallbacks(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """Copy validated cross-rail targets without sharing mutable config rows."""
    fallback = source.get("fallback")
    if isinstance(fallback, list):
        target["fallback"] = [
            dict(item) for item in fallback if isinstance(item, dict)
        ]
    return target


def _fail_safe_result(
    config: Dict[str, Any],
    rule_output: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the fail-safe routing target.

    Defaults are NON-Mac and routable (deepseek-v4-pro) — the old
    'claude-opus'/'anthropic' defaults were unroutable (no such provider) and
    Mac-only, which violated the 'Claude Code is never the sole option' rule
    when fail_safe fired (classifier down = exactly when you can't bet on the
    Mac being on-LAN). The nested `fallback` list (cross-rail targets) is
    PROPAGATED so the delegate_profile executor can try them in order.
    """
    fs = config.get("fail_safe", {}) or {}
    # A rule that matched has already decided the ROLE axis (`profile`); the
    # classifier was only ever going to choose the MODEL. Overwriting the role
    # with the fail-safe's own default throws away a decision that was made
    # deterministically and is still valid when the classifier is down. Measured:
    # "Review this PR for security issues" matched review-request (profile:
    # reviewer) and /explain reported reviewer, while route() returned coder -
    # the explanation surface and the dispatch disagreed about the same task.
    role = str((rule_output or {}).get("profile") or "") if rule_output else ""
    result = {
        "profile": role or fs.get("profile", "coder"),
        "model": fs.get("model", "deepseek-v4-pro"),
        "provider": fs.get("provider", "deepseek"),
    }
    fb = fs.get("fallback")
    if isinstance(fb, list) and fb:
        result["fallback"] = fb
    return result


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
    """Route a task, and hold the CHOSEN target to the blocklist.

    Stage 0 vets ``requested_model`` - what the caller asked for. That is the one
    model the router usually does not use: the whole point of auto-routing is that
    the caller names nothing, so the pre-filter tests an empty string and passes.
    Everything the pipeline then selects itself - from a rule, a tier, the
    classifier, or the fail-safe - reached the caller unvetted.

    Measured on the live router.yaml before this wrapper existed: with
    ``deepseek-v3.2`` in ``manual_ban``, is_blocked() answered True for it and
    route() still returned ``deepseek-v3.2 @ deepseek``. A ban and a tripped
    circuit breaker were both advisory over the router's own choices, which is
    the opposite of what a breaker is for - it exists precisely to steer traffic
    off a target that is failing, and the router is what picks the target.

    The chosen model is now vetted once, here, after every path has returned. A
    blocked choice is replaced from the fallback chain when one is available;
    when none is, the decision is denied rather than dispatched to a target known
    to be down. This is the only place that can enforce it: the pipeline below
    exits through seven separate returns.
    """
    decision = _route_unchecked(
        task,
        config,
        requested_model=requested_model,
        requested_provider=requested_provider,
        classify_fn=classify_fn,
        blocklist=blocklist,
        cache=cache,
        session_pin=session_pin,
        decision_log=decision_log,
    )
    if not isinstance(decision, dict) or decision.get("deny"):
        return decision

    chosen = str(decision.get("model") or "")
    if not chosen:
        return decision

    bl = blocklist or Blocklist(config)
    if not bl.is_blocked(chosen, str(decision.get("provider") or "")):
        return decision

    replacement = bl.fallback_for(chosen)
    # A replacement that is itself blocked is no replacement: walk the chain
    # rather than swapping one dead target for another.
    seen = {chosen}
    while replacement and replacement not in seen:
        if not bl.is_blocked(replacement, ""):
            vetted = dict(decision)
            vetted["model"] = replacement
            # The provider belonged to the model we just rejected; the chain does
            # not carry one, so drop it rather than pair a new model with a stale
            # provider and produce a target that exists nowhere.
            vetted.pop("provider", None)
            vetted["blocked_model"] = chosen
            vetted["cause"] = "blocklist_substituted"
            return vetted
        seen.add(replacement)
        replacement = bl.fallback_for(replacement)

    return {
        "deny": True,
        "blocked_model": chosen,
        "cause": "blocklist_veto",
        "reason": (
            f"routed model {chosen!r} is blocked and the fallback chain offers no "
            "reachable replacement"
        ),
    }



def _route_unchecked(
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
    """Run the full routing pipeline, without vetting the chosen target.

    Returns {profile, model?, provider?, cause, ...}. Callers want :func:`route`,
    which additionally holds the CHOSEN model to the blocklist - this function
    exits through seven separate returns and cannot enforce that itself.
    """
    bl = blocklist or Blocklist(config)
    cch = cache or Cache()
    pin = session_pin or SessionPin()
    dlog = decision_log or DecisionLog()

    # Per-stage in/out trace for visual replay. Purely observational: it mirrors
    # the values this function already computes and is passed to record() at the
    # terminal site. It changes NO routing behavior and adds NO early returns.
    steps: List[Dict[str, Any]] = []

    # --- Stage 0: blocklist pre-filter ---
    blocked = bl.is_blocked(requested_model, requested_provider)
    steps.append({
        "stage": "blocklist",
        "in": {"model": requested_model, "provider": requested_provider},
        "out": {"blocked": blocked},
        "cause": None,
    })
    if blocked:
        fallback_model = bl.fallback_for(requested_model)
        result = {"deny": True}
        if fallback_model:
            result["fallback_model"] = fallback_model
        steps.append({"stage": "veto", "in": {"model": requested_model},
                      "out": dict(result), "cause": "blocklist_veto"})
        dlog.record("blocklist_veto", result, task_preview=task[:120], steps=steps)
        return result

    # --- Stage 0: signal extraction ---
    features = extract(task)
    steps.append({"stage": "signals", "in": {"task": task[:120]},
                  "out": dict(features), "cause": None})

    # --- Stage 0: rule matching ---
    rules = config.get("rules", [])
    default = config.get("default", {})
    tiers = config.get("tiers", {})

    output, rule_id = match(features, blocked, rules, default, tiers)
    steps.append({"stage": "rules", "in": {"features": dict(features)},
                  "out": {"output": dict(output), "rule_id": rule_id},
                  "cause": _cause_from_rule(rule_id, output) if rule_id else "default_fallthrough"})

    # If rule matched and gave concrete output → route now
    if rule_id is not None and "action" not in output:
        # Concrete route — check session pin upward-only ratchet
        if pin.is_set() and output.get("model"):
            output, pin_applied = _apply_session_floor(output, pin, tiers)
            if pin_applied:
                steps.append({"stage": "session_pin", "in": {"pin": pin.tier},
                              "out": dict(output), "cause": "session_pin"})
                dlog.record("session_pin", output, matched_rule_id=rule_id,
                           task_preview=task[:120], steps=steps)
                return output

        dlog.record(
            _cause_from_rule(rule_id, output), output,
            matched_rule_id=rule_id, task_preview=task[:120], steps=steps,
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
            steps.append({"stage": "cache", "in": {"task": task[:120]},
                          "out": dict(result),
                          "cause": "session_pin" if pin_applied else "classifier"})
            dlog.record(
                "session_pin" if pin_applied else "classifier",
                result,
                task_preview=task[:120],
                steps=steps,
            )
            return result

        if classify_fn is None:
            # No classifier available → fail-safe
            result = _fail_safe_result(config, output)
            steps.append({"stage": "fail_safe", "in": {"reason": "no_classifier"},
                          "out": dict(result), "cause": "fail_safe_strong"})
            dlog.record("fail_safe_strong", result, matched_rule_id=rule_id,
                        task_preview=task[:120], steps=steps)
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
            _copy_fallbacks(result, tier_cfg)
            if "profile" not in result:
                result["profile"] = "coder"

            steps.append({
                "stage": "classifier",
                "in": {"tier": tier, "confidence": confidence},
                "out": {"effective_tier": effective_tier, "model": result.get("model")},
                "cause": "classifier",
            })
            dlog.record("classifier", result, task_preview=task[:120], steps=steps)
            return result

        except Exception:
            # Classifier failed → fail-safe
            result = _fail_safe_result(config, output)
            steps.append({"stage": "fail_safe", "in": {"reason": "classifier_error"},
                          "out": dict(result), "cause": "fail_safe_strong"})
            dlog.record("fail_safe_strong", result, matched_rule_id=rule_id,
                        task_preview=task[:120], steps=steps)
            return result

    # Fail-safe fallback
    result = _fail_safe_result(config)
    steps.append({"stage": "fail_safe", "in": {"reason": "fallthrough"},
                  "out": dict(result), "cause": "fail_safe_strong"})
    dlog.record("fail_safe_strong", result, task_preview=task[:120], steps=steps)
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
    _copy_fallbacks(result, pin_cfg)
    return result, True


def _cause_from_rule(rule_id: Any, output: Dict[str, Any]) -> str:
    """Map rule to cause label.

    ``rule_id`` is annotated str but is not one in practice: YAML gives an int for
    a numbered rule and the classifier path passes None, and every branch here
    called ``.lower()`` on it. Measured before the fix: _cause_from_rule(7, out)
    and (None, out) both raised AttributeError, from inside the code whose only
    job is to label a decision the operator wants explained - so a numbered rule
    took down the explanation of the very route it selected.
    """
    if output.get("deny"):
        return "blocklist_veto"
    ident = "" if rule_id is None else str(rule_id).lower()
    if "keyword" in ident or "review" in ident:
        return "keyword_match"
    if "size" in ident:
        return "size_rule"
    if "code" in ident or "trivial" in ident:
        return "has_code_rule"
    if "hard" in ident:
        return "hard_rule"
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
    _copy_fallbacks(result, classifier_result)
    if "profile" not in result:
        result["profile"] = "coder"
    return result
