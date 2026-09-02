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
from .rules import (
    match,
    resolve_tiers,
    explain as rules_explain,
    lint as rules_lint,
    with_global_price_windows,
)
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


class _VettedOnce:
    """One answer per ``(model, provider)`` for the whole DECISION.

    ``Blocklist.is_blocked`` is not a query: for an expired-OPEN breaker it
    transitions to HALF_OPEN and CONSUMES the single probe slot, answering False
    once and True thereafter. A decision asks about the same rail more than once —
    :func:`_veto_blocked` vets ``output["model"]``, then :func:`_vet_plan_chain`
    vets ``chain[0]``, which for an unsubstituted decision is that same pair — so
    the two halves of one decision got OPPOSITE answers.

    Measured on the shipped policy with T3's primary expired-OPEN:

        output.model         gpt-5.6-terra     <- the refused elo
        output.blocked_model (absent)
        output.cause         (absent)
        chain_plan.blocked   [gpt-5.6-terra]   <- says it WAS refused
        chain[0]             deepseek-v4-pro   <- what actually runs

    Three separate defects fall out of that one inconsistency: the decision NAMES a
    refused elo while every substitution field stays empty, so no surface reports a
    veto; the substitution machinery never engages because step 1 saw a clean
    primary; and the probe the breaker granted is never spent, because nothing is
    dispatched to that rail — and HALF_OPEN is left only by a RECORDED outcome, so
    the rail is out of rotation for good.

    Memoised PER DECISION and in memory only. Cross-process anti-stampede is
    untouched: ``delegate_profile`` builds a fresh ``Blocklist`` per call, so a
    second process still reads the persisted HALF_OPEN and stays blocked, which is
    the behaviour ``test_expired_cooldown_allows_one_probe_across_fresh_blocklists``
    pins.

    Only ``is_blocked`` is memoised. Everything else — ``fallback_for``,
    ``manual_bans``, ``record_failure`` — forwards untouched, so this is a
    drop-in for every existing caller and for a test that injects its own
    ``Blocklist``.
    """

    __slots__ = ("_bl", "_answers")

    def __init__(self, blocklist: Blocklist) -> None:
        self._bl = blocklist
        self._answers: Dict[Tuple[str, str], bool] = {}

    def is_blocked(self, model: Optional[str], provider: Optional[str]) -> bool:
        key = (model or "", provider or "")
        if key not in self._answers:
            self._answers[key] = self._bl.is_blocked(model, provider)
        return self._answers[key]

    def __getattr__(self, name: str) -> Any:
        # Reached only for names this class does not define, i.e. everything but
        # is_blocked. Keeps the proxy transparent without restating the API.
        return getattr(self._bl, name)


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
    warn_fn: Optional[Callable[..., List[Any]]] = None,
    assignee: str = "",
) -> Dict[str, Any]:
    """Route a task, and hold the CHOSEN CHAIN to the blocklist.

    Stage 0 vets ``requested_model`` - what the caller asked for. That is the one
    model the router usually does not use: the whole point of auto-routing is that
    the caller names nothing, so the pre-filter tests an empty string and passes.
    Everything the pipeline then selects itself - from a rule, a tier, the
    classifier, or the fail-safe - reached the caller unvetted.

    Measured on the live router.yaml before the veto existed: with
    ``deepseek-v3.2`` in ``manual_ban``, is_blocked() answered True for it and
    route() still returned ``deepseek-v3.2 @ deepseek``. A ban and a tripped
    circuit breaker were both advisory over the router's own choices, which is
    the opposite of what a breaker is for - it exists precisely to steer traffic
    off a target that is failing, and the router is what picks the target.

    WHAT THE VETO BINDS, and why it moved. The veto used to wrap this function
    and test ``decision["model"]`` — the DECLARED tier primary. Then the plan
    redefined which model the router actually chose: the executor iterates
    ``chain``, and after a capability filter, a time cap or a shuffle its head is
    not the declared primary. Measured on the shipped policy with
    ``gpt-5.6-luna`` in ``manual_ban``, routing "Look at this screenshot..." at
    07:00Z: the declared primary is glm-5.3 (clean, and dropped by the filter for
    ``no_vision``), so the veto passed the decision — and handed the executor a
    one-hop chain whose only hop was the BANNED elo. A safety control that vets
    the model a surface DISPLAYS while the executor runs a different one is not a
    safety control.

    So the veto now binds the PLAN, at :func:`_veto_blocked`, and it runs inside
    the single terminal funnel below — after the plan exists (it has to; there is
    nothing to vet before that) and BEFORE ``record()``, so the trace and the
    returned decision are the same vetted decision. The order inside the veto is
    primary first, then chain, and :func:`_veto_blocked` explains why that
    ordering is what makes "the head is never blocked" and "the chain is never
    empty" hold at the same time.

    ``chain`` in the returned decision is the PLANNED attempt order: the tier's
    elos with the ones that cannot meet this turn's capability requirements
    dropped, ordered by the tier's fallback strategy, minus any hop the blocklist
    refuses. The executor MUST iterate it. Rebuilding [primary] + declared
    fallbacks downstream is what kept the capability filter,
    ``fallback_strategy`` and ``pin_primary`` inert on real traffic while the
    console showed the filtered chain. It is absent when there is nothing to
    attempt (a blocklist veto, or an output with no model at all) and when the
    plan is the declared order, so those results keep their exact historical
    shape.

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

    ``warn_fn`` is the third injected impurity: Hermes' selection guard
    (``hermes_cli.model_selection_guards.selection_warnings``), evaluated at
    veto time as a RAIL veto. Every other model-selection surface in Hermes
    runs this guard when a human picks a model; the router picks autonomously
    and nothing called it, so the cost guard was inert by construction — even
    a policy edited to name a model above the $20/M-in / $100/M-out thresholds
    would have been dispatched to silently. Measured 2026-08-19 on the shipped
    policy: the guard returns [] for all 8 live links (deepseek-v4-pro, the
    priciest, is 0.435/0.87), so the veto binds nothing today — it exists so a
    guard firing cannot be ignored. When ``warn_fn`` is None the default is
    resolved lazily (:func:`_default_warn_fn`), which degrades to "no guard"
    on a host without hermes_cli; tests inject a fake for determinism on both.

    Returns {profile, model?, provider?, chain?, cause?, ...}, or
    {deny: True, ...}. There is deliberately no unvetted entry point: the veto
    lives at the one site every terminal path already funnels through, so a new
    ``return`` cannot skip it the way the eight returns below once skipped the
    plan.
    """
    config = with_global_price_windows(config)
    bl = _VettedOnce(blocklist or Blocklist(config))
    cch = cache or Cache()
    pin = session_pin or SessionPin()
    dlog = decision_log or DecisionLog()
    warn = warn_fn if warn_fn is not None else _default_warn_fn()

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
        """Plan the chain, vet it, record the decision, return the target.

        Every terminal site goes through here so no path can quietly skip the
        plan: the plan is what production attempts and the trace is what an
        operator replays, and the two drifting apart is exactly the defect this
        indirection exists to prevent. Eight record() sites each remembering to
        pass chain_plan= is the arrangement that produced that defect, so there
        is one site. It runs LAST on purpose — after the session-pin floor,
        whose tier lookup reads ``output["model"]``.

        The three steps are ordered, and the order is the fix for the veto that
        used to sit OUTSIDE this function:
          1. plan  — there is nothing to vet until the plan names the hops;
          2. vet   — the blocklist and the selection guard remove what must
             not be attempted;
          3. record — so the persisted trace is the decision that ran, not the
             one that would have run. Recording before the veto is how a banned
             target ended up in the trace as the chosen model while the caller
             got a substitution, and how the substitution's own ``cause`` never
             reached the log at all.
        ``_with_chain`` runs last on the VETTED plan, so the ``chain`` the
        executor iterates and the ``chain_plan`` the console renders are the same
        list.
        """
        plan = _plan_chain_for(output, features, rng=turn_rng, when=when)
        # ``config`` rather than the ``tiers`` local below: the Stage-0 veto path
        # calls finish() before that local is bound, and the tier table is what
        # tells a substituted primary which rail it will be dispatched on.
        decided, plan, cause = _veto_blocked(
            output, plan, bl, cause, tiers=config.get("tiers"), warn_fn=warn,
        )
        dlog.record(
            cause, decided, matched_rule_id=matched_rule_id,
            task_preview=task[:120], steps=steps, chain_plan=plan,
        )
        return _with_chain(decided, plan)

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
    # The role is the other per-turn INPUT: whoever created the work already
    # chose it, and nothing here can change it — the dispatcher's hook applies
    # only model/provider. Injecting it lets a rule that is only correct for one
    # role SAY so in `when`, which is where an input belongs, instead of naming
    # the role in `then` where this path can never honor it.
    features.update(_role_features(assignee))
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


def _role_features(assignee: str) -> Dict[str, Any]:
    """The injected role feature, or {} when the caller fixed no role.

    Named ``assignee`` after the field the dispatcher hands the hook, and NOT
    ``profile``, because ``then.profile`` in the same policy file is the other
    axis — the role a rule had in mind. One word for both would make a config
    where ``when.profile`` and ``then.profile`` point in opposite directions.

    Empty means the key is absent, and a `when` clause whose field is absent
    never matches — so a role-scoped rule is inert wherever no role was fixed,
    the same property the clock features have.
    """
    return {"assignee": assignee} if assignee else {}


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


# ---------------------------------------------------------------------------
# The blocklist veto — it binds the PLAN, because the plan is what runs
# ---------------------------------------------------------------------------
#
# Reason string stamped on every hop the veto removes. `Blocklist.is_blocked`
# unions operator manual bans with auto-breaker cooldowns into ONE boolean
# (blocklist.py's stated contract), so it cannot tell the two apart and this
# module must not invent a distinction it did not measure. The operator reads
# `manual_ban` / `hermes-router breaker` for which of the two fired.
_BLOCKED_REASON = "blocked"

# Reason string stamped on a hop the SELECTION guard vetoed (cost/data-policy).
# Distinguished from `_BLOCKED_REASON` because the two have different fixes: a
# blocked hop is a ban or a breaker cooldown; a selection-vetoed hop is a model
# the operator pointed the policy at without noticing it trips Hermes' own
# expensive-model guard.
_SELECTION_REASON = "selection_warning"


def _default_warn_fn() -> Optional[Callable[..., List[Any]]]:
    """Resolve the Hermes selection guard, or None when it is not importable.

    Deferred and guarded exactly like the plugin's other hermes_cli accesses:
    the router runs inside Hermes where ``hermes_cli`` exists, and on CI (no
    hermes_cli) the guard degrades to "no guard" rather than failing the
    import. Call-time resolution — not a module-level import — so a test can
    fake the module away and back without reloading this module.
    """
    try:
        from hermes_cli.model_selection_guards import selection_warnings
    except ImportError:  # pragma: no cover - CI has no hermes_cli
        return None
    return selection_warnings


def _selection_vetoes(
    warn_fn: Optional[Callable[..., List[Any]]],
    model: str,
    provider: str,
) -> bool:
    """True when the selection guard fires for ``model@provider``.

    A firing warning is a rail veto: the model was not merely pricier than a
    sibling, it tripped a guard every interactive selection surface in Hermes
    would have made a human confirm. The router has no human, so it refuses
    the rail instead.

    Defensive by construction: the guard is on the DECISION path, and a
    misbehaving guard must never refuse a turn — the registry already swallows
    its own exceptions, but the injected callable is a test fake half the time
    and ``warn_fn=None`` (no hermes_cli) is a supported shape, so both degrade
    to "not vetoed" here.
    """
    if warn_fn is None:
        return False
    try:
        return bool(warn_fn(str(model), provider=(provider or None)))
    except Exception:  # noqa: BLE001 - a guard must never break routing
        return False


def _veto_blocked(
    output: Dict[str, Any],
    plan: Optional[Dict[str, Any]],
    bl: Blocklist,
    cause: str,
    *,
    tiers: Optional[Dict[str, Any]] = None,
    warn_fn: Optional[Callable[..., List[Any]]] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], str]:
    """Hold what will ACTUALLY be attempted to the blocklist AND the guard.

    Pure apart from the ``bl`` lookups and the ``warn_fn`` call; returns NEW
    dicts and never mutates ``output`` or ``plan`` (a caller keeps the pre-veto
    plan for comparison, and plan hops can be config rows). Returns
    ``(decision, plan, log_cause)``.

    Two things are attemptable and both are vetted, in this order:

      1. ``output["model"]`` — the declared tier primary. It is what the executor
         attempts when there is no plan, and what every display surface calls
         "the model". A refused primary is substituted by
         :func:`_reachable_replacement`, which searches the PLAN first, then the
         declared chain, then ``blocklist.fallback_chain``; the decision is
         DENIED only when all three offer no clean rail, i.e. when there is
         genuinely nothing to attempt. A candidate is vetted on the RAIL it
         would be dispatched on and that rail travels back paired with it —
         vetting the model alone left a hole in the invariant below.
      2. ``plan["chain"]`` — the hops the executor iterates. Refused hops are
         dropped.

    A rail can be refused for either of two reasons, kept distinct on the
    rejected hop (``reject_reason``) because the fixes are different:

      * ``blocked`` — the blocklist (a manual ban or a breaker cooldown).
      * ``selection_warning`` — :func:`_selection_vetoes`, i.e. Hermes' own
        selection guard (cost / data-policy) fired for the rail. Every other
        model-selection surface makes a human confirm this warning; the router
        has no human, so it refuses the rail. ``warn_fn`` defaults to the live
        guard via :func:`_default_warn_fn` and degrades to "no guard" on a
        host without hermes_cli.

    ``tiers`` is the policy tier table, read ONLY to recover which rail a
    ``fallback_chain`` entry would run on: that list is flat model ids and
    carries no provider of its own.

    The ordering is not cosmetic; it is what lets both invariants hold at once:

      * THE HEAD IS NEVER REFUSED. On the substitution path the head is the
         replacement, verified unblocked and unvetoed. On the ordinary path the
         primary is verified clean, so it is always available as a head.
      * THE CHAIN IS NEVER EMPTY. If dropping the refused hops would empty the
         planned chain, the veto falls back to the DECLARED chain's clean hops
         and flags ``blocklist_widened``. That set is non-empty WHENEVER
         THERE IS A PRIMARY, because step 1 then established it is clean and
         it is a member of the declared chain. The plan gives way, never the
         refusal: this lands in exactly the state the capability filter reaches
         on its own bypass ("routing beats correctness"), with the trace naming
         every hop the ban list removed.

    Both facts are reported in the plan rather than left to be reconstructed:
    ``blocked`` lists the removed hops, ``blocklist_widened`` says the planner's
    order gave way, and ``blocklist_bypassed`` says a refused hop is STILL in the
    chain — the last resort, which is what an output with NO primary at all falls
    to (see :func:`_vet_plan_chain`).
    """
    if not isinstance(output, dict) or output.get("deny"):
        # A Stage-0 veto (or any denial) attempts nothing. Nothing to vet.
        return output, plan, cause

    warn = _default_warn_fn() if warn_fn is None else warn_fn
    chosen = str(output.get("model") or "")
    chosen_provider = str(output.get("provider") or "")
    if not chosen or not (
        bl.is_blocked(chosen, chosen_provider)
        or _selection_vetoes(warn, chosen, chosen_provider)
    ):
        return output, _vet_plan_chain(plan, output, bl, head=None, warn_fn=warn), cause

    selection_vetoed = _selection_vetoes(warn, chosen, chosen_provider)
    replacement = _reachable_replacement(
        chosen, bl, output, tiers, warn_fn=warn, plan=plan,
    )
    if replacement is None:
        if selection_vetoed:
            denied = {
                "deny": True,
                "blocked_model": chosen,
                "cause": "selection_vetoed",
                "reason": (
                    f"routed model {chosen!r} fired the selection guard and "
                    "no clean rail remains in the plan, the declared chain or "
                    "the fallback chain"
                ),
            }
            return (
                denied,
                _plan_with_no_attempt(plan, chosen, reason=_SELECTION_REASON),
                "selection_vetoed",
            )
        denied = {
            "deny": True,
            "blocked_model": chosen,
            "cause": "blocklist_veto",
            "reason": (
                f"routed model {chosen!r} is blocked and no clean rail remains "
                "in the plan, the declared chain or the fallback chain"
            ),
        }
        return denied, _plan_with_no_attempt(plan, chosen), "blocklist_veto"

    # The substitution now comes from the PLAN first (see
    # :func:`_reachable_replacement`), which is what closes the capability-blind
    # hole this comment used to only report. Measured on the shipped policy with
    # glm-5.3 banned on a VISION turn: the flat ``blocklist.fallback_chain`` handed
    # back deepseek-v4-flash, which cannot see, while the plan was holding
    # gpt-5.6-luna, which can. The plan is capability-filtered, cost-ordered and
    # already provider-paired, so preferring it is strictly better on both axes —
    # and it is the order the executor iterates anyway, so the vetted head and
    # chain[0] now agree structurally instead of by coincidence.
    sub_model, sub_provider = replacement
    vetted = dict(output)
    vetted["model"] = sub_model
    # The provider that lands here is the one the replacement was VETTED on, and
    # _headed_chain copies it onto the head hop — so the rail the veto
    # cleared is the rail the executor dispatches, structurally rather than by
    # coincidence. The old provider belonged to the model we just rejected and is
    # never kept: pairing a new model with a stale provider names a target that
    # exists nowhere. "" (no rail declared anywhere in the policy) drops the key,
    # which is also what the dispatch will carry, so the "" lookup that vetted it
    # is the honest one.
    if sub_provider:
        vetted["provider"] = sub_provider
    else:
        vetted.pop("provider", None)
    vetted["blocked_model"] = chosen
    if selection_vetoed:
        vetted["cause"] = "selection_vetoed"
    else:
        vetted["cause"] = "blocklist_substituted"
    # Neither `blocklist_substituted` nor `selection_vetoed` is a member of
    # decision_log.VALID_CAUSES (either would be coerced to unknown_cause), so
    # the LOG keeps the cause the pipeline decided and the substitution
    # travels in output["cause"].
    return vetted, _vet_plan_chain(plan, output, bl, head=vetted, warn_fn=warn), cause


def _reachable_replacement(
    chosen: str,
    bl: Blocklist,
    output: Dict[str, Any],
    tiers: Optional[Dict[str, Any]],
    warn_fn: Optional[Callable[..., List[Any]]] = None,
    plan: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    """The first CLEAN (model, rail) that could replace a refused primary.

    Three sources are searched IN ORDER, and the order is the design:

      1. ``plan["chain"]`` — the planner's own attempt order. It is
         capability-filtered, cost-ordered and every hop already carries the rail
         it would be dispatched on, so it needs no rail recovery and cannot hand
         back an elo the capability filter already refused. It is also the order
         the executor iterates, so a head taken from here agrees with ``chain[0]``
         structurally rather than by coincidence.
      2. ``_declared_chain(output)`` — the tier's declared hops, including the ones
         the planner dropped (capability-rejected or cost-capped). Reached only
         when every planned hop is refused: a cost control must never cause an
         outage, the same reading ``apply_time_cap`` uses for its own bypass.
      3. ``blocklist.fallback_chain`` — the operator's flat CROSS-TIER escape
         hatch. It is the only source that can leave this tier, which is why it is
         kept, and the only one that needs :func:`_dispatch_provider` to recover a
         rail (it is bare model ids).

    Searching only source 3 was a hole with two separate consequences, both
    measured on the shipped policy:

      * CAPABILITY-BLINDNESS. With glm-5.3 banned on a vision turn the flat chain
        handed back deepseek-v4-flash, which cannot see, while the plan was
        holding gpt-5.6-luna, which can.
      * A TOTAL REFUSAL AN OPERATOR CAN REACH BY EDITING ONE FIELD.
        ``fallback_chain`` is documented as the union of every tier member and has
        to be REGENERATED by hand when ``tiers`` changes; nothing lints that. Point
        a tier's primary at a model absent from the list — which the console's tier
        editor permits — and ``fallback_for`` has no position to walk from, so a
        single ban denied the whole turn while that tier's own declared fallback
        was sitting there clean. That is what
        ``test_the_first_attempt_is_never_a_manually_banned_elo`` caught after
        commit bdb92f6 retired glm-5.3 from the tier table.

    A candidate that is itself refused — by the blocklist OR by the selection
    guard (``warn_fn``) — is no replacement, so the search continues rather than
    swapping one dead target for another. None means nothing anywhere is clean,
    which is the caller's cue to deny.

    Each candidate is vetted AT MOST ONCE across all three sources
    (``checked``). That is not just an optimisation: ``Blocklist.is_blocked``
    transitions an expired-OPEN breaker to HALF_OPEN and CONSUMES its single probe
    slot, so re-vetting the same elo would burn a second slot for one decision.
    ``walked`` stays a separate set from ``checked`` because source 3 needs a
    genuine cycle guard for a ``fallback_chain`` an operator made cyclic — a
    candidate already vetted above must be SKIPPED without stopping the walk.

    Each candidate is vetted WITH THE PROVIDER IT WOULD BE DISPATCHED ON,
    resolved by :func:`_dispatch_provider`, and that provider is returned paired
    with the model so the caller can put the vetted pair on the decision. Asking
    ``is_blocked(model, "")`` instead was a hole in exactly the invariant this
    veto exists to establish: ``Blocklist`` keys breaker cooldowns
    ``model@provider`` and the executor now always records them
    provider-qualified, so a lookup with no provider reads a cell nothing ever
    writes and cannot see a cooldown at all. Measured on the shipped policy with
    the zai and deepseek rails degraded together (the ordinary shape of a provider
    incident): glm-5.3's cooldown sent the veto here, the walk handed back
    deepseek-v4-flash whose OWN cooldown was open, and the plan then listed that
    elo in ``blocked`` — the DISPLAY — while ``chain[0]`` was the same elo, which
    is what RUNS.

    NOT fixed by also asking ``is_blocked(model, "")``: for a provider-scoped
    manual ban that call is strictly MORE blocking (``Blocklist._match`` reads an
    empty provider as "banned on every rail"), so ORing the two would refuse a
    model on rails the operator never named.
    """
    checked = {chosen}

    # Sources 1 and 2 are hop lists that already name their own rail; only fall
    # back to the policy scan for a hop that declares none.
    for hop in _planned_hops(plan) + _declared_chain(output):
        candidate = str(hop.get("model") or "")
        if not candidate or candidate in checked:
            continue
        checked.add(candidate)
        provider = str(hop.get("provider") or "") or _dispatch_provider(
            candidate, output, tiers,
        )
        if _is_clean(bl, warn_fn, candidate, provider):
            return candidate, provider

    # Source 3: the flat cross-tier list. Bare model ids, so the rail comes from
    # the policy scan.
    walked = {chosen}
    replacement = bl.fallback_for(chosen)
    while replacement and replacement not in walked:
        walked.add(replacement)
        if replacement not in checked:
            checked.add(replacement)
            provider = _dispatch_provider(replacement, output, tiers)
            if _is_clean(bl, warn_fn, replacement, provider):
                return replacement, provider
        replacement = bl.fallback_for(replacement)
    return None


def _planned_hops(plan: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The planner's attempt order as named hops, or [] when there is no plan.

    A plan is optional on this path (a caller may route with no capability layer
    at all), and an unattributable hop is not a candidate — the same reading
    :func:`_has_named_hop` uses.
    """
    if not isinstance(plan, dict):
        return []
    chain = plan.get("chain")
    if not isinstance(chain, list):
        return []
    return [hop for hop in chain if isinstance(hop, dict) and hop.get("model")]


def _is_clean(
    bl: Blocklist,
    warn_fn: Optional[Callable[..., List[Any]]],
    model: str,
    provider: str,
) -> bool:
    """True when ``model@provider`` is refused by neither veto.

    The two refusals are deliberately NOT distinguished here — a caller that needs
    to name which one fired (``reject_reason`` on a rejected hop) asks the two
    predicates separately. This helper exists for the callers that only need
    "is this rail attemptable", so the pair cannot drift between them.
    """
    return not bl.is_blocked(model, provider) and not _selection_vetoes(
        warn_fn, model, provider,
    )


def _dispatch_provider(
    model: str,
    output: Dict[str, Any],
    tiers: Optional[Dict[str, Any]],
) -> str:
    """The rail ``model`` would run on, or "" when the policy declares none.

    ``blocklist.fallback_chain`` is a flat list of model ids, so a substitution
    taken from it has to recover its rail from the policy — and it has to, because
    a breaker cooldown is keyed ``model@provider`` and half of that key cannot be
    guessed at lookup time.

    The scan is the tier table: primaries before per-tier ``fallback`` hops, which
    is what makes the answer total on the shipped policy, since router.yaml
    regenerates ``fallback_chain`` from the tier members. (The executor's
    ``_provider_of_declared_model`` scans the same places for the same reason: it
    is filling in the other half of the same key.)

    This decision's OWN declared hops used to be consulted first, and that loop is
    gone rather than left as belt-and-braces: it is now PROVABLY dead. Both callers
    live in :func:`_reachable_replacement`, which searches the planned and declared
    chains BEFORE the flat list and takes each hop's own ``provider`` from the hop
    itself — so by the time this function is asked anything, either the model is not
    in the declared chain at all (the cross-tier walk), or the hop naming it
    declared no rail, in which case that loop could not have answered either. A
    branch no caller can reach is not defence in depth; it is a line that reads as
    covering a case nobody can produce.

    A row that names no provider is not an answer, only the absence of one, so the
    scan keeps going rather than letting a half-edited tier shadow the row that
    does name the rail. Defensive rather than raising throughout: the config is
    HOT and may be mid-edit, so a non-mapping tier or a non-list ``fallback`` is
    skipped. "" means no rail is declared anywhere, which is also what the
    dispatch will then carry.
    """
    if not isinstance(tiers, dict):
        return ""
    rows = [cfg for cfg in tiers.values() if isinstance(cfg, dict)]
    for cfg in rows:
        if cfg.get("model") == model and cfg.get("provider"):
            return str(cfg["provider"])
    for cfg in rows:
        hops = cfg.get("fallback")
        if not isinstance(hops, list):
            continue
        for hop in hops:
            if (isinstance(hop, dict) and hop.get("model") == model
                    and hop.get("provider")):
                return str(hop["provider"])
    return ""


def _vet_plan_chain(
    plan: Optional[Dict[str, Any]],
    output: Dict[str, Any],
    bl: Blocklist,
    *,
    head: Optional[Dict[str, Any]],
    warn_fn: Optional[Callable[..., List[Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Remove the refused hops from a planned chain, never emptying it.

    ``head``, when given, is the substituted decision: its model LEADS the chain
    (the veto, not the planner, owns that hop) and the surviving planned hops
    become the tail. The executor prefers ``chain`` over the declared order, so a
    plan left pointing at the model the veto just rejected would hand that model
    straight back as the first attempt and make the substitution cosmetic.

    ``output`` is the pre-veto decision, read ONLY for its declared chain when the
    widening fallback below fires.

    Returns ``plan`` UNCHANGED (same object) when there was nothing to do, so a
    clean turn's trace stays byte-identical. A non-mapping plan or a plan with no
    chain list is passed through: there is nothing to vet, and a missing plan
    already means "the executor rebuilds the declared order", whose head is the
    primary that step 1 of :func:`_veto_blocked` vetted.

    ``bl`` is queried with each hop's OWN provider, so a cooldown recorded
    against ``model@provider`` binds on a fallback hop and not just on a tier
    primary. The write side agrees, which is what makes that pay off:
    ``_record_breaker_outcome`` takes the ATTEMPTED provider as a fourth argument
    and the executor passes the one that attempt actually dispatched on, so a
    failing fallback hop is recorded under the same ``model@provider`` key this
    lookup asks for. (Its policy scan is only a gap-filler for a caller that
    could not name the rail.) Both sides being provider-qualified is also why the
    substitution in :func:`_reachable_replacement` must carry a real provider:
    with the write side qualified, a lookup that drops the provider is guaranteed
    to miss.

    The selection guard (``warn_fn``) refuses hops exactly like the blocklist
    does, with ``reject_reason: "selection_warning"`` on the rejected row so the
    two causes are distinguishable in the trace. A hop the guard refuses is
    removed from the chain, and a widening fallback never reintroduces one.

    Still NOT an option here or there: widening a lookup by also querying
    ``is_blocked(model, "")``. That call is strictly MORE blocking for a
    provider-scoped manual ban, so it would ban a model on rails the operator did
    not name.
    """
    if not isinstance(plan, dict):
        return plan
    planned = plan.get("chain")
    if not isinstance(planned, list):
        return plan

    kept: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    for hop in planned:
        model = hop.get("model") if isinstance(hop, dict) else None
        if not model:
            # An unattributable hop has nothing to vet and nobody to blame, so
            # it passes through — exactly as capabilities.apply_time_cap does.
            kept.append(dict(hop) if isinstance(hop, dict) else hop)
            continue
        hop_provider = str(hop.get("provider") or "")
        if bl.is_blocked(str(model), hop_provider):
            row = dict(hop)
            row["reject_reason"] = _BLOCKED_REASON
            blocked.append(row)
        elif _selection_vetoes(warn_fn, str(model), hop_provider):
            row = dict(hop)
            row["reject_reason"] = _SELECTION_REASON
            blocked.append(row)
        else:
            kept.append(dict(hop))

    if head is None and not blocked:
        return plan  # nothing to do — keep the trace byte-identical

    vetted = dict(plan)
    if blocked:
        vetted["blocked"] = blocked

    if head is not None:
        vetted["chain"] = _headed_chain(head, kept)
        # The head is the verified-clean replacement, so neither degraded
        # outcome applies however the tail turned out.
        vetted["blocklist_widened"] = False
        vetted["blocklist_bypassed"] = False
        return vetted

    if _has_named_hop(kept):
        vetted["chain"] = kept
        vetted["blocklist_widened"] = False
        vetted["blocklist_bypassed"] = False
        return vetted

    # Dropping the refused hops emptied the plan. Widen to the DECLARED chain's
    # clean hops — provably non-empty, see _veto_blocked — so the refusal holds
    # and the turn still has a route. The declared chain is read off ``output``
    # (the same helper the executor's own fallback mirrors) rather than
    # reassembled from the plan's chain+rejected+capped lists: ``capped`` rows
    # carry a model and a multiplier but NO provider, so a hop recovered from
    # there would name a model on no rail at all.
    widened = [
        dict(hop) for hop in _declared_chain(output)
        if _is_clean(
            bl, warn_fn, str(hop.get("model")), str(hop.get("provider") or ""),
        )
    ]
    if _has_named_hop(widened):
        vetted["chain"] = widened
        vetted["blocklist_widened"] = True
        vetted["blocklist_bypassed"] = False
        return vetted

    # Last resort: every hop anyone declared is refused. Keep the planned chain
    # rather than return an empty one — an empty chain is an outage — and say so
    # loudly, since this is the one shape where an elo may be both refused and
    # attempted. NOT defence in depth, as this comment used to claim: _veto_blocked
    # step 1 vets `output["model"]`, so it establishes nothing for an output that
    # HAS no model, and a tier declaring only fallback hops resolves to exactly
    # that. Refuse every one of those hops and there is no primary to widen to
    # either. Covered by
    # test_a_chain_of_nothing_but_banned_hops_bypasses_itself_loudly.
    vetted["chain"] = [dict(hop) for hop in planned if isinstance(hop, dict)]
    vetted["blocklist_widened"] = False
    vetted["blocklist_bypassed"] = True
    return vetted


def _plan_with_no_attempt(
    plan: Optional[Dict[str, Any]],
    chosen: str,
    *,
    reason: str = _BLOCKED_REASON,
) -> Optional[Dict[str, Any]]:
    """The plan for a DENIED decision: nothing is attempted, and it says so.

    ``chain`` is emptied because a denial dispatches nothing — the same shape the
    Stage-0 veto records — while the planner's diagnostics (requirements,
    rejected, strategy) are kept so an operator can still see what the router
    had picked before the ban list refused it. An empty chain is legitimate
    HERE and only here: it sits next to ``deny: True`` and a named cause, which
    is a loud refusal rather than a filter silently leaving nothing to run.

    ``reason`` is the reject_reason stamped on the denied hop: ``blocked`` for
    the blocklist, ``selection_warning`` for the selection guard.
    """
    if not isinstance(plan, dict):
        return plan
    denied = dict(plan)
    denied["chain"] = []
    denied["blocked"] = [{"model": chosen, "reject_reason": reason}]
    denied["blocklist_widened"] = False
    denied["blocklist_bypassed"] = False
    return denied


def _headed_chain(
    head: Dict[str, Any],
    tail: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """``head``'s (model, provider) first, then ``tail`` minus that model."""
    hop: Dict[str, Any] = {"model": head.get("model")}
    if head.get("provider"):
        hop["provider"] = head["provider"]
    return [hop] + [
        item for item in tail
        if not isinstance(item, dict) or item.get("model") != head.get("model")
    ]


def _has_named_hop(chain: List[Any]) -> bool:
    """True when ``chain`` holds at least one hop that names a model.

    "Emptied" means no NAMED elo survived: a chain of unattributable hops is not
    a route. Same reading capabilities.apply_time_cap uses for its own bypass.
    """
    return any(isinstance(hop, dict) and hop.get("model") for hop in chain)


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
    # member and inventing one would be coerced to unknown_cause.
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

    This function is now the ONE rule-id labeller for both halves of that pair.
    ``rules._determine_cause`` — what /explain, RouterService.explain, the
    sidecar and the dashboard read — no longer keeps its own copy of the table or
    of the heuristic below: it fetches this function at call time
    (``rules._cause_labeller``) and delegates the rule-id axis to it, keeping only
    the two labels keyed on the OUTPUT rather than on the id (``deny`` and
    ``action: classify``). So the console and the trace of the same decision agree
    by construction, and an edit HERE moves both surfaces at once — including the
    substring probes, which is why a row renamed around a signal keeps its axis on
    both. Do not re-add a mirror of this table over there; the drifted copy is the
    defect that arrangement replaced.
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
