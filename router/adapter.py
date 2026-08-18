"""Route hook — the adapter: only Hermes-coupled code.

Wires Stage 0 (blocklist + signals + rules) → Stage 1 (classifier)
→ delegate_profile(). One decision path, one cause= log, one call.

This module is also the EDGE, and the two impure inputs the pure core refuses
to reach for are injected from HERE:

  * the per-turn ``random.Random`` that makes ``fallback_strategy: random`` a
    real spread on live traffic. It is seeded from the task text and the seed is
    written into the trace, so the exact order that ran can be replayed later.
  * the wall clock. It is read here, once per turn, and passed down as a value,
    so signals.py and rules.py stay pure/deterministic/IO-free while `time_cap`,
    `time_policy` and `cheapest_now` are live in production.

Nothing below this module reads either one; both arrive as parameters. The one
other rule the layout encodes: the chain plan is built LAST, after the
session-pin floor, because the floor identifies a tier by looking
``output["model"]`` up in the tier table and a chain reordered ahead of it would
silently unenforce the ratchet.
"""

from __future__ import annotations

import hashlib
import inspect
import random
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .signals import extract
from .rules import match, resolve_tiers, explain as rules_explain, lint as rules_lint
from .blocklist import Blocklist
from .classify import Classifier
from .cache import Cache, SessionPin
from .decision_log import DecisionLog

# The planner. Resolved defensively for the same reason service.py inspects
# ``rules.explain``'s signature and cli.py getattrs this name: this plugin is
# deployed by copy, so router/rules.py can land a version behind router/
# adapter.py. A rules.py with no planner routes the DECLARED chain — the
# pre-capability behaviour — instead of failing to import, and the mismatch is
# not silent: ``test_the_installed_planner_is_wired_into_production`` fails, and
# so does every two-surface-agreement test, because an inert feature is the
# defect this whole layer exists to prevent.
try:
    from .rules import plan_chain
except ImportError:  # pragma: no cover - only on a rules.py behind this file
    plan_chain = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Tier route vs tier policy
# ---------------------------------------------------------------------------
#
# A resolved output carries two different things from its tier. `model`,
# `provider` and `fallback` are the ROUTE — where the request goes. Everything
# else a tier materialises is the PLANNING POLICY that rules.plan_chain reads.
# Both halves must always come from the SAME tier: a session-pin floor or a
# classifier answer that replaced the route but kept the previous tier's policy
# would plan the new chain under the old chain's rules (its requirements floor,
# its fallback strategy, its time policy).

_TIER_ROUTE_KEYS: frozenset = frozenset({"model", "provider", "fallback"})

# The policy keys known to this module, used ONLY to evict a previous tier's
# stale value. Carrying is done by copying whatever rules.resolve_tiers
# materialised, so a knob added there needs no change here — only a knob that
# must also be *evicted* on a tier switch belongs in this set.
_TIER_POLICY_KEYS: frozenset = frozenset({
    "fallback_strategy", "pin_primary", "billing_mode", "requirements",
    "declared_capabilities", "time_policy", "time_cap",
})

# Placeholder tier name used to materialise a single tier mapping through
# rules.resolve_tiers (see _resolve_tier_cfg). Never leaves this module.
_POLICY_ALIAS = "__tier__"

# Whether the installed rules.plan_chain accepts an injected clock. Resolved
# once by signature — not by catching TypeError — so a genuine TypeError raised
# INSIDE the planner is never masked by a silent second call. A checkout whose
# rules.py predates the time layer still routes; it just routes time-agnostic.
try:
    _PLAN_CHAIN_ACCEPTS_WHEN = "when" in inspect.signature(plan_chain).parameters
except (TypeError, ValueError):  # pragma: no cover - absent/unintrospectable
    _PLAN_CHAIN_ACCEPTS_WHEN = False


# ---------------------------------------------------------------------------
# Tier route vs tier policy
# ---------------------------------------------------------------------------
#
# A resolved output carries two different things from its tier. `model`,
# `provider` and `fallback` are the ROUTE — where the request goes. Everything
# else a tier materialises is the PLANNING POLICY that rules.plan_chain reads.
# Both halves must always come from the SAME tier: a session-pin floor or a
# classifier answer that replaced the route but kept the previous tier's policy
# would plan the new chain under the old chain's rules (its requirements floor,
# its fallback strategy, its time policy).

_TIER_ROUTE_KEYS: frozenset = frozenset({"model", "provider", "fallback"})

# The policy keys known to this module, used ONLY to evict a previous tier's
# stale value. Carrying is done by copying whatever rules.resolve_tiers
# materialised, so a knob added there needs no change here — only a knob that
# must also be *evicted* on a tier switch belongs in this set.
_TIER_POLICY_KEYS: frozenset = frozenset({
    "fallback_strategy", "pin_primary", "billing_mode", "requirements",
    "declared_capabilities", "time_policy", "time_cap",
})

# Placeholder tier name used to materialise a single tier mapping through
# rules.resolve_tiers (see _resolve_tier_cfg). Never leaves this module.
_POLICY_ALIAS = "__tier__"

# Whether the installed rules.plan_chain accepts an injected clock. Resolved
# once by signature — not by catching TypeError — so a genuine TypeError raised
# INSIDE the planner is never masked by a silent second call. A checkout whose
# rules.py predates the time layer still routes; it just routes time-agnostic.
try:
    _PLAN_CHAIN_ACCEPTS_WHEN = "when" in inspect.signature(plan_chain).parameters
except (TypeError, ValueError):  # pragma: no cover - unintrospectable callable
    _PLAN_CHAIN_ACCEPTS_WHEN = False


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
    prompt_text: str = "",
    rng: Optional[random.Random] = None,
    now: Optional[datetime] = None,
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

    ``chain`` in the returned decision is the PLANNED attempt order: the tier's
    elos with the ones that cannot meet this turn's capability requirements
    dropped, ordered by the tier's fallback strategy. The executor MUST iterate
    it. Rebuilding [primary] + declared fallbacks downstream is what kept the
    capability filter, ``fallback_strategy`` and ``pin_primary`` inert on real
    traffic while the console showed the filtered chain. It is absent when there
    is nothing to attempt (a blocklist veto, or an output with no model at all)
    and when the plan is the declared order, so those results keep their exact
    historical shape.

    ``task`` is the goal. ``prompt_text`` is the full text the model will
    actually receive (context + goal) and defaults to ``task``. Signals are read
    from ``prompt_text`` because est_input_tokens has to measure the real input;
    the CLASSIFIER and the response cache still key on ``task`` alone, since
    embedding a 120k-char context into a 128-token classification prompt would
    cost more than the turn being routed. That split is deliberate, not
    incidental.

    ``rng`` and ``now`` are the injected impurities and both default to a live
    value: ``rng`` to a generator seeded from the task text (deterministic,
    replayable from the trace, and different per turn so traffic really spreads
    across the tail), ``now`` to the current UTC time — the ONLY wall-clock read
    on the decision path. Tests pass both to pin the outcome.
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
        prompt_text=prompt_text,
        rng=rng,
        now=now,
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
            # The plan was built around the primary this wrapper just rejected,
            # and the executor prefers ``chain`` over the declared order — so a
            # stale plan would send the substituted decision straight back to
            # the banned target and undo the substitution silently.
            _revet_chain(vetted, bl)
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
    prompt_text: str = "",
    rng: Optional[random.Random] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run the full routing pipeline, without vetting the chosen target.

    Returns {profile, model?, provider?, chain?, cause, ...}. Callers want
    :func:`route`, which additionally holds the CHOSEN model to the blocklist -
    this function exits through seven separate returns and cannot enforce that
    itself. See :func:`route` for ``chain``, ``prompt_text``, ``rng`` and
    ``now``.
    """
    bl = blocklist or Blocklist(config)
    cch = cache or Cache()
    pin = session_pin or SessionPin()
    dlog = decision_log or DecisionLog()

    # Per-turn ordering seed and clock, derived/read HERE and passed down as
    # values. ``seed`` is None when the caller injected its own generator, so
    # the trace never advertises a seed that did not produce the recorded order.
    seed = None if rng is not None else _turn_seed(task)
    turn_rng = rng if rng is not None else random.Random(seed)
    when = now if now is not None else datetime.now(timezone.utc)

    # Per-stage in/out trace for visual replay. Purely observational: it mirrors
    # the values this function already computes and is passed to record() at the
    # terminal site. It changes NO routing behavior and adds NO early returns.
    steps: List[Dict[str, Any]] = []

    # Bound before the blocklist stage: a veto returns before extraction and
    # still has to record a (necessarily empty) plan.
    features: Dict[str, Any] = {}

    def finish(
        cause: str,
        output: Dict[str, Any],
        *,
        matched_rule_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Plan the chain, record the decision, return the routable target.

        Every terminal site goes through here so no path can quietly skip the
        plan: the plan is what production attempts and the trace is what an
        operator replays, and the two drifting apart is exactly the defect this
        indirection exists to prevent. Eight record() sites each remembering to
        pass chain_plan= is the arrangement that produced that defect, so there
        is one site. It runs LAST on purpose — after the session-pin floor,
        whose tier lookup reads ``output["model"]``.
        """
        plan = _plan_chain_for(output, features, rng=turn_rng, when=when)
        dlog.record(
            cause, output, matched_rule_id=matched_rule_id,
            task_preview=task[:120], steps=steps, chain_plan=plan,
        )
        return _with_chain(output, plan)

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
        return finish("blocklist_veto", result)

    # --- Stage 0: signal extraction ---
    # Read from the text the model will actually receive, so est_input_tokens
    # measures the real input instead of the goal line alone.
    features = extract(prompt_text or task)
    # The clock is a per-turn INPUT to the feature vector, injected here because
    # signals.extract() is pure and must never read it. A rule keyed on
    # utc_hour/utc_weekday is therefore live in production, and inert (never
    # spuriously matching) anywhere no clock is supplied.
    features.update(_clock_features(when))
    steps.append({"stage": "signals", "in": {"task": task[:120], "seed": seed},
                  "out": dict(features), "cause": None})

    # --- Stage 0: rule matching ---
    rules = config.get("rules", [])
    default = config.get("default", {})
    tiers = config.get("tiers", {})

    output, rule_id = match(features, blocked, rules, default, tiers)
    steps.append({"stage": "rules", "in": {"features": dict(features)},
                  "out": {"output": dict(output), "rule_id": rule_id},
                  "cause": _cause_from_rule(rule_id, output) if rule_id else "default_fallthrough"})

    # A concrete decision routes now, whether a rule or the default made it. This
    # used to require `rule_id is not None`, so a `default:` naming a model was
    # resolved by rules.match and then thrown away: the task fell through to the
    # classifier, or with the classifier down to fail_safe. Measured with
    # default {profile: coder, model: T2} (resolving to deepseek-v4-pro): the
    # classifier gave deepseek-v3.2 (cheaper and weaker than configured), and with
    # no classifier it gave claude-opus-5 on the Mac-gated rail. A default that
    # names a model is an instruction, not a hint.
    if "action" not in output and output.get("model"):
        # Concrete route — check session pin upward-only ratchet
        if pin.is_set() and output.get("model"):
            output, pin_applied = _apply_session_floor(output, pin, tiers)
            if pin_applied:
                steps.append({"stage": "session_pin", "in": {"pin": pin.tier},
                              "out": dict(output), "cause": "session_pin"})
                return finish("session_pin", output, matched_rule_id=rule_id)

        return finish(
            _cause_from_rule(rule_id, output), output, matched_rule_id=rule_id,
        )

    # --- Stage 1: classifier ---
    # Enter the classifier when the POLICY asks for it, or when nothing concrete
    # was decided. The gate used to read `or rule_id is None`, which sent every
    # unmatched task to the classifier even when `default:` had named a concrete
    # target that rules.match had already resolved through _resolve_tiers.
    # Measured with default {profile: coder, model: T2}, which resolves to
    # deepseek-v4-pro: with a classifier the task got deepseek-v3.2 (T1, cheaper
    # and weaker than configured), and with the classifier down it got
    # claude-opus-5 on the Mac-gated rail - the opposite of the cheap
    # deterministic default that was asked for. A default that names a model is an
    # instruction, not a hint.
    needs_classifier = output.get("action") == "classify" or not output.get("model")
    if needs_classifier:
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
            return finish(
                "session_pin" if pin_applied else "classifier", result,
            )

        if classify_fn is None:
            # No classifier available → fail-safe
            result = _fail_safe_result(config, output)
            steps.append({"stage": "fail_safe", "in": {"reason": "no_classifier"},
                          "out": dict(result), "cause": "fail_safe_strong"})
            return finish("fail_safe_strong", result, matched_rule_id=rule_id)

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

            # Materialise the tier exactly as a rule-matched alias would, so the
            # classified route carries its tier's PLANNING POLICY and not just
            # its model. Without this a classifier answer planned the new tier's
            # chain under whatever policy the default/rule output was still
            # carrying.
            resolved = _resolve_tier_cfg(tier_cfg)

            # Cache the effective result, not the raw classifier answer.
            cch.set(task, {"tier": effective_tier, **resolved})

            # Merge the ROLE axis from the rule with the MODEL axis from the tier.
            # (model, provider) is one decision: assigning the model while the
            # provider used setdefault let a provider named by the rule or the
            # default outlive the model it belonged to. Measured with
            # default {provider: zai, action: classify} and the classifier
            # answering T4: `gpt-5.6-terra @ zai`, while T4 is openai-codex - a
            # pair that names no real rail, failing opaquely at spawn.
            result = dict(output)
            result.pop("action", None)
            result["model"] = resolved.get("model")
            tier_provider = resolved.get("provider")
            if tier_provider:
                result["provider"] = tier_provider
            else:
                result.pop("provider", None)
            _copy_fallbacks(result, resolved)
            _adopt_tier_policy(result, resolved)
            if "profile" not in result:
                result["profile"] = "coder"

            steps.append({
                "stage": "classifier",
                "in": {"tier": tier, "confidence": confidence},
                "out": {"effective_tier": effective_tier, "model": result.get("model")},
                "cause": "classifier",
            })
            return finish("classifier", result)

        except Exception:
            # Classifier failed → fail-safe
            result = _fail_safe_result(config, output)
            steps.append({"stage": "fail_safe", "in": {"reason": "classifier_error"},
                          "out": dict(result), "cause": "fail_safe_strong"})
            return finish("fail_safe_strong", result, matched_rule_id=rule_id)

    # Fail-safe fallback
    result = _fail_safe_result(config)
    steps.append({"stage": "fail_safe", "in": {"reason": "fallthrough"},
                  "out": dict(result), "cause": "fail_safe_strong"})
    return finish("fail_safe_strong", result)


# ---------------------------------------------------------------------------
# Injected impurities — seed, clock, plan
# ---------------------------------------------------------------------------

def _turn_seed(task: str) -> int:
    """Deterministic per-turn seed for the ``random`` fallback strategy.

    blake2b over the task text, NOT ``hash()``: str hashing is salted per
    process (PYTHONHASHSEED), so a hash()-derived seed could not be replayed in
    a later process — which is the whole point of recording it in the trace.
    Same turn ⇒ same order, so a decision is reproducible; different turns ⇒
    different order, so traffic really spreads across the tail instead of
    hammering one rail.
    """
    digest = hashlib.blake2b(
        (task or "").encode("utf-8", "replace"), digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big")


def _clock_features(when: Optional[datetime]) -> Dict[str, Any]:
    """The two injected time features, or {} when there is no clock.

    Names and ranges are the addendum's: ``utc_hour`` 0-23 and ``utc_weekday``
    0=Monday. An aware datetime is normalised to UTC first so a clock from
    another zone cannot silently shift a window; a naive one is taken to be UTC
    already, which is what every caller here produces. No clock means the keys
    are absent, and a `when` clause whose field is absent never matches — so
    time-keyed routing is inert rather than arbitrary.
    """
    if not isinstance(when, datetime):
        return {}
    stamp = when.astimezone(timezone.utc) if when.tzinfo is not None else when
    return {"utc_hour": stamp.hour, "utc_weekday": stamp.weekday()}


def _plan_chain_for(
    output: Dict[str, Any],
    features: Dict[str, Any],
    *,
    rng: Optional[random.Random],
    when: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    """rules.plan_chain for one decision, or None when no plan could be built.

    The planner is a cost control and an audit record, never a gate: it must not
    be the thing that fails a delegation, so a rules.py with no planner at all
    and any exception from the planner both degrade to "no plan" and the executor
    falls back to the declared order.
    """
    if plan_chain is None:
        return None
    try:
        if _PLAN_CHAIN_ACCEPTS_WHEN:
            plan = plan_chain(output, features, rng=rng, when=when)
        else:
            plan = plan_chain(output, features, rng=rng)
    except Exception:  # noqa: BLE001 - a plan must never break the decision
        return None
    return plan if isinstance(plan, dict) else None


def _with_chain(
    output: Dict[str, Any],
    plan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Attach the planned attempt order to a routing result — when it differs.

    Returns a NEW dict (config rows are never shared), or ``output`` itself when
    there is nothing to add.

    ``chain`` is attached ONLY when the plan actually dropped or reordered
    something. Its absence means "the declared order stands", which is exactly
    what the executor rebuilds from ``model`` + ``fallback``, so both branches
    attempt the same targets in the same order — the executor never has to know
    which branch it got. Suppressing the redundant copy keeps this dict
    byte-identical for every turn the plan is a no-op, and this dict is every
    existing consumer's contract (a veto stays exactly ``{"deny": True}``).
    Nothing observable is lost: the trace records the FULL plan under
    ``chain_plan`` on every path, including the elos that were dropped and why.
    """
    if not isinstance(plan, dict):
        return output
    chain = [
        dict(hop) for hop in (plan.get("chain") or [])
        if isinstance(hop, dict) and hop.get("model")
    ]
    if not chain or _hops_of(chain) == _hops_of(_declared_chain(output)):
        return output
    result = dict(output)
    result["chain"] = chain
    return result


def _declared_chain(output: Dict[str, Any]) -> List[Dict[str, Any]]:
    """[primary] + declared fallback hops — what the executor falls back to."""
    chain: List[Dict[str, Any]] = []
    if output.get("model"):
        chain.append({"model": output["model"], "provider": output.get("provider")})
    fallback = output.get("fallback")
    if isinstance(fallback, list):
        chain.extend(
            hop for hop in fallback if isinstance(hop, dict) and hop.get("model")
        )
    return chain


def _hops_of(chain: List[Dict[str, Any]]) -> List[Tuple[Any, Any]]:
    """(model, provider) identity of each hop — what the executor actually uses."""
    return [(hop.get("model"), hop.get("provider")) for hop in chain]


def _revet_chain(vetted: Dict[str, Any], bl: Blocklist) -> None:
    """Re-point a substituted decision's planned chain at its new head.

    Mutates ``vetted`` in place. :func:`route` substitutes a blocked primary
    AFTER the pipeline has already planned around it, and the executor prefers
    ``chain`` over the declared order — so an untouched plan would hand the
    banned target back as the first attempt and the substitution would be
    cosmetic, which is the same class of defect as showing a filtered chain and
    attempting an unfiltered one.

    The substituted model leads (the wrapper, not the planner, owns that
    decision) and the planner's surviving order becomes the tail, minus any hop
    that is itself blocked. The chain therefore always has at least one hop:
    a cost or safety control must not be able to cause an outage.
    """
    planned = vetted.get("chain")
    if not isinstance(planned, list):
        return
    head_model = vetted.get("model")
    head: Dict[str, Any] = {"model": head_model}
    if vetted.get("provider"):
        head["provider"] = vetted["provider"]
    tail = [
        dict(hop) for hop in planned
        if isinstance(hop, dict) and hop.get("model")
        and hop.get("model") != head_model
        and not bl.is_blocked(str(hop["model"]), str(hop.get("provider") or ""))
    ]
    vetted["chain"] = [head] + tail


# ---------------------------------------------------------------------------
# Session pin / tier policy
# ---------------------------------------------------------------------------

_TIER_ORDER = {"T1": 0, "T2": 1, "T3": 2, "T4": 3}


def _resolve_tier_cfg(tier_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Materialise ONE tier mapping exactly as a rule-matched tier alias is.

    Delegates to rules.resolve_tiers through a one-entry tier table rather than
    mirroring its materialisation rules here, so every knob rules.py knows how
    to carry (the per-tier requirements floor, the fallback strategy, the
    per-elo declared capabilities) reaches the classifier and session-pin paths
    too — and stays in sync when a knob is added.

    A tier that declares no model of its own yields no ``model`` key rather
    than the placeholder alias: inventing a model id here would be worse than
    reporting none.
    """
    if not isinstance(tier_cfg, dict):
        return {}
    resolved = resolve_tiers({"model": _POLICY_ALIAS}, {_POLICY_ALIAS: tier_cfg})
    if not isinstance(tier_cfg.get("model"), str):
        resolved.pop("model", None)
    return resolved


def _adopt_tier_policy(target: Dict[str, Any], resolved: Dict[str, Any]) -> None:
    """Replace ``target``'s planning policy with the one on ``resolved``.

    Mutates in place. Every non-route key materialised on ``resolved`` is
    copied, and any policy key this module knows about that ``resolved`` does
    NOT declare is dropped — otherwise a tier switch (a session-pin ratchet, a
    classifier answer) would leave the previous tier's strategy or requirements
    floor governing the new chain. Measured: a T1→T3 ratchet planned T3's chain
    with T1's ``time_cap`` still attached, so a cost control the promoted tier
    never declared shrank its chain.
    """
    if not isinstance(resolved, dict):
        return
    policy = {
        key: value for key, value in resolved.items()
        if key not in _TIER_ROUTE_KEYS and key != "tier"
    }
    for key in _TIER_POLICY_KEYS - set(policy):
        target.pop(key, None)
    target.update(policy)


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

    Promotion swaps the whole tier — route AND policy. The floor used to copy
    model/provider/fallback only, which was invisible while nothing consumed
    the policy and became wrong the moment plan_chain did: a T1→T3 ratchet
    would have planned T3's chain without T3's own requirements floor, and with
    T1's time_cap still attached.
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
    resolved = _resolve_tier_cfg(pin_cfg)
    _copy_fallbacks(result, pin_cfg)
    _adopt_tier_policy(result, resolved)
    return result, True


# Explicit rule-id → cause for the shipped Table 1 rows whose id carries none of
# the substrings the heuristic below looks for. Consulted FIRST, because the
# substring probes are a courtesy for operator-renamed rows and a SHIPPED row
# must not depend on a courtesy. Before this table the two rows the conditional
# routing layer added — `vision-required` and `huge-context-read` — were both
# labelled `default_fallthrough`, so a live vision route was recorded as
# "nothing matched" and an operator counting hits per cause saw the new rules
# never fire. The cause set is CLOSED (decision_log.VALID_CAUSES coerces an
# unknown cause to fail_safe_strong), so each new row maps onto the existing
# member that names the signal it actually keyed on.
_RULE_ID_CAUSES: Dict[str, str] = {
    # Keyed on est_input_tokens — the size of the input picked the tier.
    "huge-context-read": "size_rule",
    # Keyed on num_files — the extent of the change picked the tier.
    "cross-file-or-protocol": "size_rule",
    # needs_vision is a keyword/marker signal (signals._VISION_MARKERS), so
    # keyword_match is the honest label; the closed set has no capability
    # member and inventing one would be coerced to fail_safe_strong.
    "vision-required": "keyword_match",
    # Keyed on has_code, exactly like trivial-mechanical-edit.
    "standard-implementation": "has_code_rule",
}


def _cause_from_rule(rule_id: Any, output: Dict[str, Any]) -> str:
    """Map rule to cause label.

    ``rule_id`` is annotated str but is not one in practice: YAML gives an int for
    a numbered rule and the classifier path passes None, and every branch here
    called ``.lower()`` on it. Measured before the fix: _cause_from_rule(7, out)
    and (None, out) both raised AttributeError, from inside the code whose only
    job is to label a decision the operator wants explained - so a numbered rule
    took down the explanation of the very route it selected.

    NOTE for whoever owns router/rules.py: ``rules._determine_cause`` has the
    same rule-id gap this table closes, and it is the one /explain reads. The two
    functions must agree or the console labels a decision differently from the
    trace of the same decision.
    """
    if output.get("deny"):
        return "blocklist_veto"
    ident = "" if rule_id is None else str(rule_id).lower()
    explicit = _RULE_ID_CAUSES.get(ident)
    if explicit is not None:
        return explicit
    # `vision`/`context` join the heuristic for the same reason `review` and
    # `trivial` are already there: a row renamed around the same signal should
    # keep its axis rather than fall to default_fallthrough.
    if "keyword" in ident or "review" in ident or "vision" in ident:
        return "keyword_match"
    if "size" in ident or "context" in ident:
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
    """Merge classifier result with rule output.

    ``classifier_result`` is a cache entry written by the classifier path, so it
    already carries its tier's materialised policy; that policy is adopted here
    for the same reason the floor adopts it — the plan must be built from the
    policy of the tier that actually won.
    """
    result = dict(rule_output)
    result.pop("action", None)
    # (model, provider) is ONE decision, not two. The model was assigned
    # unconditionally while the provider used setdefault, so a provider already
    # present in the rule or the default won over the tier that supplied the
    # model. Measured: default {provider: zai, action: classify} with the
    # classifier answering T4 produced `gpt-5.6-terra @ zai` - the T4 model on the
    # default's rail. gpt-5.6-terra lives on openai-codex, so that pair names a
    # target that exists nowhere, and the failure surfaces as an opaque provider
    # error at spawn time rather than as a routing fault.
    if "model" in classifier_result:
        result["model"] = classifier_result["model"]
        # The provider travels with the model it belongs to. Absent from the
        # classifier result, drop the stale one rather than pair them.
        if "provider" in classifier_result:
            result["provider"] = classifier_result["provider"]
        else:
            result.pop("provider", None)
    elif "provider" in classifier_result:
        result.setdefault("provider", classifier_result["provider"])
    _copy_fallbacks(result, classifier_result)
    _adopt_tier_policy(result, classifier_result)
    if "profile" not in result:
        result["profile"] = "coder"
    return result
