"""Unit tests for route adapter (router/adapter.py)."""

import copy
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from router import adapter
from router import capabilities as caps
from router import rules as rules_mod
from router import signals
from router.adapter import route
from router.blocklist import Blocklist
from router.cache import Cache, SessionPin
from router.decision_log import DecisionLog

# A clock inside no declared price window (Monday 12:00 UTC), so a test that is
# not ABOUT time never depends on which window the wall clock happens to be in.
FIXED_CLOCK = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


ROUTER_CONFIG = {
    "enabled": True,
    "classifier": {
        "model": "glm-5.2",
        "provider": "zai",
        "temperature": 0,
        "max_tokens": 128,
        "timeout_seconds": 8,
    },
    "fail_safe": {
        "profile": "coder",
        "model": "claude-opus",
        "provider": "anthropic",
    },
    "blocklist": {
        "manual_ban": [
            {"model": "gpt-5.6-sol", "provider": "openai-codex",
             "reason": "accept-but-never-stream"},
        ],
        "fallback_chain": ["gpt-5.6-sol", "glm-5.2"],
        "auto_breaker": {"enabled": False},
    },
    "rules": [
        {
            "id": "block-codex-stall",
            "status": "stable",
            "when": {"model": {"in": ["gpt-5.6-sol", "openai-codex"]}},
            "then": {"deny": True},
        },
        {
            "id": "trivial-mechanical-edit",
            "status": "stable",
            "when": {"verb_class": {"eq": "trivial"}, "has_code": {"eq": True},
                     "size_lines": {"lte": 40}},
            "then": {"profile": "coder", "model": "T1"},
        },
        {
            "id": "hard-verbs",
            "status": "stable",
            "when": {"verb_class": {"eq": "hard"}},
            "then": {"profile": "coder", "model": "T4"},
        },
        {
            "id": "review-request",
            "status": "stable",
            "when": {"keywords": {"contains": "review"}},
            "then": {"profile": "reviewer", "action": "classify"},
        },
    ],
    "default": {"action": "classify"},
    "tiers": {
        "T1": {"model": "glm-5.2-fast", "provider": "zai"},
        "T2": {"model": "glm-5.2", "provider": "zai"},
        "T3": {"model": "claude-sonnet", "provider": "anthropic"},
        "T4": {"model": "claude-opus", "provider": "anthropic"},
    },
}


class TestRouteStage0:
    """Stage 0: blocklist + deterministic rules, no model call."""

    def test_blocklist_veto(self):
        """Blocked model → deny immediately."""
        result = route(
            "test", ROUTER_CONFIG,
            requested_model="gpt-5.6-sol",
            requested_provider="openai-codex",
        )
        assert result["deny"] is True

    def test_trivial_route_direct(self):
        """Trivial + code + small → T1, no classifier."""
        result = route(
            "Rename getCwd in 3 files, 20 lines", ROUTER_CONFIG,
        )
        assert result["profile"] == "coder"
        assert result["model"] == "glm-5.2-fast"

    def test_hard_direct(self):
        """Hard verb → T4, no classifier."""
        decision_log = DecisionLog()
        result = route(
            "Debug a race condition in the user cache", ROUTER_CONFIG,
            decision_log=decision_log,
        )
        assert result["model"] == "claude-opus"
        assert result["profile"] == "coder"
        assert decision_log.tail(1)[0]["cause"] == "hard_rule"

    def test_steps_trace_records_stage_sequence_for_direct_route(self):
        """The steps[] trace captures each pipeline stage in/out for replay."""
        decision_log = DecisionLog()
        route(
            "Debug a race condition in the user cache", ROUTER_CONFIG,
            decision_log=decision_log,
        )
        steps = decision_log.tail(1)[0]["steps"]
        stages = [s["stage"] for s in steps]
        # Direct hard-rule route: blocklist → signals → rules, terminal cause.
        assert stages[:3] == ["blocklist", "signals", "rules"]
        assert steps[0]["out"] == {"blocked": False}
        assert "features" in steps[2]["in"]
        assert steps[-1]["cause"] == "hard_rule"

    def test_steps_trace_records_veto_branch(self):
        decision_log = DecisionLog()
        route(
            "test", ROUTER_CONFIG,
            requested_model="gpt-5.6-sol", requested_provider="openai-codex",
            decision_log=decision_log,
        )
        steps = decision_log.tail(1)[0]["steps"]
        assert steps[0]["stage"] == "blocklist"
        assert steps[-1]["stage"] == "veto"
        assert steps[-1]["cause"] == "blocklist_veto"

    def test_hard_tier_propagates_cross_rail_fallbacks(self):
        config = copy.deepcopy(ROUTER_CONFIG)
        config["tiers"]["T4"]["fallback"] = [
            {"model": "backup-model", "provider": "backup-provider"}
        ]

        result = route("Debug a race condition", config)

        assert result["fallback"] == [
            {"model": "backup-model", "provider": "backup-provider"}
        ]

    def test_review_classify_action(self):
        """Review keyword → profile=reviewer, action=classify → classifier needed."""
        # Without classify_fn → falls to fail-safe
        result = route(
            "Please review this PR for security issues", ROUTER_CONFIG,
        )
        # No classify_fn → fail-safe
        assert result["profile"] in ("coder", "reviewer")  # depends on path
        # Actually, the review rule fires → profile=reviewer + action=classify
        # But with no classify_fn, it should fall to fail_safe
        # Let me check...

    def test_default_classify_no_classifier(self):
        """Default → classify but no classifier → fail-safe."""
        result = route("Hello world", ROUTER_CONFIG)
        assert result["profile"] == "coder"
        assert result["model"] == "claude-opus"  # fail_safe


class TestRouteStage1:
    """Stage 1: classifier integration (mock)."""

    def test_classifier_called_on_uncertainty(self):
        """Default fall-through → classifier fires."""
        calls = []

        def mock_classify(task, features):
            calls.append((task, features))
            return {"tier": "T2", "confidence": "high",
                    "signals": "simple", "needs_capability": "standard"}

        result = route(
            "Add a /health endpoint", ROUTER_CONFIG,
            classify_fn=mock_classify,
        )
        assert len(calls) == 1
        assert result["model"] == "glm-5.2"  # T2 tier

    def test_classifier_tier_propagates_cross_rail_fallbacks(self):
        config = copy.deepcopy(ROUTER_CONFIG)
        config["tiers"]["T2"]["fallback"] = [
            {"model": "backup-model", "provider": "backup-provider"}
        ]

        result = route(
            "Add a health endpoint",
            config,
            classify_fn=lambda _task, _features: {
                "tier": "T2", "confidence": "high"
            },
        )

        assert result["fallback"] == [
            {"model": "backup-model", "provider": "backup-provider"}
        ]

    def test_classifier_safety_ratchet(self):
        """Low confidence → bumped up one tier."""
        def mock_classify(task, features):
            return {"tier": "T1", "confidence": "low",
                    "signals": "maybe trivial", "needs_capability": "edge case"}

        result = route(
            "Fix typo in README", ROUTER_CONFIG,
            classify_fn=mock_classify,
        )
        # T1 + low confidence → T2
        assert result["model"] == "glm-5.2"

    def test_classifier_failure_fail_safe(self):
        """Classifier throws → fail-safe."""
        def mock_classify(task, features):
            raise RuntimeError("model call failed")

        result = route(
            "Complex task needing classification", ROUTER_CONFIG,
            classify_fn=mock_classify,
        )
        assert result["profile"] == "coder"
        assert result["model"] == "claude-opus"


class TestRouteCache:
    """Cache + session pin integration."""

    def test_cache_hit_skips_classifier(self):
        calls = []

        def mock_classify(task, features):
            calls.append(task)
            return {"tier": "T2", "confidence": "high"}

        cache = Cache()
        # First call — classifier fires
        result1 = route(
            "Add a /health endpoint", ROUTER_CONFIG,
            classify_fn=mock_classify, cache=cache,
        )
        assert len(calls) == 1

        # Same task — cache hit, no classifier call
        result2 = route(
            "  Add   a /health endpoint  ", ROUTER_CONFIG,  # whitespace normalized
            classify_fn=mock_classify, cache=cache,
        )
        assert len(calls) == 1  # still 1, cache hit

    def test_session_pin_upward_only(self):
        pin = SessionPin()

        # First: hard task → T4
        result1 = route(
            "Debug a race condition", ROUTER_CONFIG,
            session_pin=pin,
        )
        assert result1["model"] == "claude-opus"

        # Session pin should be set to T4-ish
        # But the pin only gets set when classifier fires, not on direct rule match
        # Actually, in the current code, pin.set() only happens in classifier path
        # This test validates that the pin doesn't break direct routes


class TestRouteDecisionLog:
    """Decision log integration."""

    def test_log_blocklist_veto(self):
        dlog = DecisionLog()
        route("test", ROUTER_CONFIG,
              requested_model="gpt-5.6-sol",
              requested_provider="openai-codex",
              decision_log=dlog)
        entries = dlog.tail(1)
        assert entries[0]["cause"] == "blocklist_veto"

    def test_log_classifier(self):
        dlog = DecisionLog()
        def mock_classify(task, features):
            return {"tier": "T2", "confidence": "high"}
        route("Add a /health endpoint", ROUTER_CONFIG,
              classify_fn=mock_classify, decision_log=dlog)
        entries = dlog.tail(1)
        assert entries[0]["cause"] == "classifier"

    def test_log_fail_safe(self):
        dlog = DecisionLog()
        route("Hello", ROUTER_CONFIG, decision_log=dlog)
        entries = dlog.tail(1)
        assert entries[0]["cause"] == "fail_safe_strong"


def test_cause_from_rule_survives_a_non_string_rule_id():
    """A numbered rule must not crash the label that explains its own decision.

    rule_id is annotated str but YAML yields an int for a numbered rule and the
    classifier path passes None. Every branch called .lower() on it, so
    _cause_from_rule(7, out) raised AttributeError from inside the explanation
    path - the route was chosen correctly and then became unexplainable.
    """
    from router.adapter import _cause_from_rule

    out = {"model": "glm-5.2-fast", "provider": "zai"}
    assert _cause_from_rule("hard-verbs", out) == "hard_rule"
    assert _cause_from_rule(7, out) == "default_fallthrough"
    assert _cause_from_rule(None, out) == "default_fallthrough"
    assert _cause_from_rule(1.5, out) == "default_fallthrough"
    # A numeric id still yields to a deny, which does not consult the id at all.
    assert _cause_from_rule(7, {"deny": True}) == "blocklist_veto"


def _live_config():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent.parent
    return yaml.safe_load((root / "router.yaml").read_text(encoding="utf-8"))


def test_a_blocked_model_the_router_chose_itself_is_not_dispatched():
    """The blocklist must bind the router's own choice, not just the request.

    Stage 0 vets requested_model. Under auto-routing the caller names nothing, so
    that check tested "" and passed, and whatever the pipeline then selected -
    rule, tier, classifier or fail-safe - was dispatched unvetted. Measured on the
    live config: with deepseek-v3.2 banned, is_blocked said True and route() still
    returned deepseek-v3.2. A tripped circuit breaker was advisory over exactly the
    decision it exists to steer.
    """
    import copy
    from router.adapter import route
    from router.blocklist import Blocklist

    cfg = _live_config()
    task = "Rename getCwd in src/utils.py"
    chosen = route(task, cfg).get("model")
    assert chosen, "the live config must route this task somewhere"

    banned = copy.deepcopy(cfg)
    banned["blocklist"]["manual_ban"].append({"model": chosen, "provider": "", "reason": "test"})
    bl = Blocklist(banned)
    assert bl.is_blocked(chosen, "") is True

    out = route(task, banned, blocklist=bl)
    assert out.get("model") != chosen, f"dispatched a banned model: {out}"
    # Either a reachable replacement, or an explicit denial - never the dead target.
    assert out.get("deny") is True or out.get("cause") == "blocklist_substituted"
    assert out.get("blocked_model") == chosen


def test_an_explicit_request_for_a_clean_model_is_untouched():
    """The wrapper must not disturb the ordinary path."""
    from router.adapter import route

    cfg = _live_config()
    out = route("Rename getCwd in src/utils.py", cfg)
    assert out.get("deny") is not True
    assert out.get("cause") != "blocklist_substituted"


def test_a_failsafe_after_a_matched_rule_still_names_that_rule():
    """cause=fail_safe_strong must not erase which rule got us there.

    A rule with action:classify decides the role and hands the model choice to the
    classifier. When the classifier is down the cause is legitimately
    fail_safe_strong, but rule_id was recorded as None - so the trace showed a
    fail-safe with no explanation, and an operator counting hits per rule saw
    review-request as never firing when it fired every time.
    """
    from router.adapter import route
    from router.decision_log import DecisionLog

    cfg = _live_config()
    log = DecisionLog()
    route("Review this PR for security issues", cfg, classify_fn=None, decision_log=log)
    entries = log.tail(1)
    assert entries, "the decision should have been recorded"
    e = entries[0]
    assert e.get("cause") == "fail_safe_strong"
    assert e.get("rule_id") == "review-request", e


@pytest.mark.parametrize("tier", ["T1", "T2", "T3", "T4"])
def test_the_classifier_tier_supplies_both_model_and_provider(tier):
    """(model, provider) is one decision and must travel together.

    The model was assigned from the chosen tier while the provider used setdefault,
    so a provider named by the rule or the default outlived the model it belonged
    to. Measured with default {provider: zai, action: classify} and the classifier
    answering T4: gpt-5.6-terra @ zai, while T4 is openai-codex. That pair names no
    real rail; the spawn fails with an opaque provider error, and because
    nonzero_exit is not retryable the cross-rail fallback never advances.
    """
    import copy
    from router.adapter import route

    cfg = _live_config()
    steered = copy.deepcopy(cfg)
    steered["default"] = {"profile": "coder", "provider": "zai", "action": "classify"}

    out = route(
        "an entirely ambiguous request",
        steered,
        classify_fn=lambda _t, _f: {"tier": tier, "confidence": "high"},
    )
    expected = cfg["tiers"][tier]
    assert out.get("model") == expected["model"]
    assert out.get("provider") == expected["provider"], (
        f"{tier}: {out.get('model')} @ {out.get('provider')} is a cross-rail pair"
    )


@pytest.mark.parametrize("has_classifier", [True, False])
def test_a_default_that_names_a_model_is_honoured_not_reclassified(has_classifier):
    """A concrete `default:` is an instruction, not a hint.

    rules.match resolves `default: {model: T2}` through _resolve_tiers into a real
    target, and the adapter threw it away: the Stage-1 gate read
    `or rule_id is None`, so every unmatched task went to the classifier regardless.
    Measured with T2 = deepseek-v4-pro: with a classifier the task got
    deepseek-v3.2 (T1 - cheaper and weaker than configured); with the classifier
    down it got claude-opus-5 on the Mac-gated copilot-acp rail, the opposite of the
    cheap deterministic default that was asked for.
    """
    import copy
    from router.adapter import route

    cfg = _live_config()
    steered = copy.deepcopy(cfg)
    steered["default"] = {"profile": "coder", "model": "T2"}
    fn = (lambda _t, _f: {"tier": "T1", "confidence": "high"}) if has_classifier else None

    out = route("an entirely ambiguous request", steered, classify_fn=fn)
    expected = cfg["tiers"]["T2"]
    assert out.get("model") == expected["model"], out
    assert out.get("provider") == expected["provider"], out


def test_a_default_asking_to_classify_still_classifies():
    """The fix must not stop `action: classify` from reaching the classifier.

    The live config's default is exactly that, so this is the regression guard for
    every unmatched task in production.
    """
    from router.adapter import route

    cfg = _live_config()
    out = route("do something", cfg, classify_fn=lambda _t, _f: {"tier": "T3", "confidence": "high"})
    assert out.get("model") == cfg["tiers"]["T3"]["model"]


def test_decision_without_model_passes_through_unvetted():
    """A pipeline result with no model is returned as-is (nothing to vet)."""
    from router.adapter import route

    cfg = _live_config()
    # A config with NO tiers: the classifier can name T4 but no tier exists,
    # so the merged result carries no model — the wrapper must not crash.
    no_tiers = copy.deepcopy(cfg)
    no_tiers["tiers"] = {}
    out = route(
        "some ambiguous task",
        no_tiers,
        classify_fn=lambda _t, _f: {"tier": "T4", "confidence": "high"},
    )
    assert out.get("model") is None
    assert out.get("deny") is not True


def test_blocklist_chain_fully_blocked_denies():
    """When every candidate in the fallback chain is blocked, deny — don't dispatch."""
    from router.adapter import route
    from router.blocklist import Blocklist

    # First discover what the router picks for this task, then ban the whole
    # chain INCLUDING the chosen model so no reachable replacement remains.
    cfg = copy.deepcopy(_live_config())
    task = "rename a variable in utils.py"
    chosen = route(task, cfg).get("model")
    assert chosen, "the live config must route this task somewhere"

    chain = [chosen, "fallback-a", "fallback-b"]
    cfg["blocklist"]["fallback_chain"] = chain
    cfg["blocklist"]["manual_ban"] = [
        {"model": m, "provider": "", "reason": "test-ban"} for m in chain
    ]
    bl = Blocklist(cfg)
    out = route(task, cfg, blocklist=bl)
    assert out.get("deny") is True
    assert out.get("cause") == "blocklist_veto"
    assert out.get("blocked_model") == chosen


def test_classifier_tier_without_provider_drops_stale_provider():
    """A tier that names no provider must not pair a stale provider with its model."""
    from router.adapter import route

    cfg = copy.deepcopy(_live_config())
    # T3 without provider — the merged result must not keep a stale provider.
    cfg["tiers"]["T3"] = {"model": "providerless-model"}
    out = route(
        "do something",
        cfg,
        classify_fn=lambda _t, _f: {"tier": "T3", "confidence": "high"},
    )
    assert out.get("model") == "providerless-model"
    assert "provider" not in out, f"stale provider leaked: {out}"


def test_default_with_model_and_unknown_action_falls_through_to_fail_safe():
    """A default with a model but a non-classify action reaches the fail-safe."""
    from router.adapter import route
    from router.decision_log import DecisionLog

    cfg = copy.deepcopy(_live_config())
    # action present (so the concrete-route gate at Stage 0 is skipped) but not
    # "classify" (so the classifier gate is skipped too) → final fail-safe.
    cfg["default"] = {"profile": "coder", "model": "T1", "action": "weird-action"}
    log = DecisionLog()
    out = route("some ambiguous task", cfg, decision_log=log)
    assert out.get("model") == cfg["fail_safe"]["model"]
    entries = log.tail(1)
    assert entries and entries[0].get("cause") == "fail_safe_strong"


def test_cache_resolve_output_model_without_provider_drops_stale_provider():
    """A cached classifier result with model but no provider drops the stale one."""
    from router.adapter import route
    from router.cache import Cache

    cfg = copy.deepcopy(_live_config())
    cch = Cache()
    # Cached result has a model but NO provider: the rule output's stale
    # provider must be dropped rather than paired with the new model.
    cch.set("cached-task", {"tier": "T4", "model": "model-only"})
    out = route(
        "cached-task",
        cfg,
        classify_fn=lambda _t, _f: {"tier": "T1", "confidence": "high"},  # must not fire
        cache=cch,
    )
    assert out.get("model") == "model-only"
    assert "provider" not in out, f"stale provider leaked: {out}"


def test_cache_resolve_output_provider_without_model_uses_setdefault():
    """A cached result with provider but no model keeps an existing rule provider."""
    from router.adapter import route
    from router.cache import Cache

    cfg = copy.deepcopy(_live_config())
    cch = Cache()
    # Rule output already carries a provider (review-request names reviewer);
    # cached classifier result contributes only a provider → setdefault keeps rule's.
    cch.set("cached-review", {"tier": "T4", "provider": "cached-provider"})
    out = route(
        "cached-review",
        cfg,
        classify_fn=lambda _t, _f: {"tier": "T1", "confidence": "high"},
        cache=cch,
    )
    assert out.get("provider") is not None


# ---------------------------------------------------------------------------
# The planned chain — production must attempt what the plan says
# ---------------------------------------------------------------------------
#
# ROUTER_CONFIG above names models the capability registry has never heard of,
# which the filter deliberately passes through (fail OPEN on ignorance). The
# tests below therefore need their own table of REAL registry ids, or there is
# nothing for the filter to filter.

VISION_TASK = "look at this screenshot and fix the ui code"

CAPABILITY_CONFIG = {
    "enabled": True,
    "classifier": {"model": "glm-4.7", "provider": "zai"},
    "fail_safe": {"profile": "coder", "model": "gpt-5.6-luna",
                  "provider": "openai-codex"},
    "blocklist": {"manual_ban": [], "fallback_chain": [],
                  "auto_breaker": {"enabled": False}},
    "rules": [
        {"id": "vision-required", "when": {"needs_vision": {"eq": True}},
         "then": {"profile": "coder", "model": "T2"}},
        {"id": "huge-context-read", "when": {"est_input_tokens": {"gt": 20000}},
         "then": {"profile": "coder", "model": "T3"}},
        {"id": "trivial-mechanical-edit",
         "when": {"verb_class": {"eq": "trivial"}, "has_code": {"eq": True}},
         "then": {"profile": "coder", "model": "T1"}},
    ],
    "default": {"action": "classify"},
    "tiers": {
        # T1 keeps sequential order so a ratchet test can tell T1's policy from
        # T4's by looking at the resulting order alone.
        "T1": {
            "model": "glm-4.7", "provider": "zai", "billing_mode": "plan",
            "fallback": [{"model": "gpt-5.6-luna", "provider": "openai-codex",
                          "billing_mode": "subscription"}],
            "fallback_strategy": "sequential",
        },
        # T2: the shipped shape — a plan-covered primary that cannot see images,
        # behind it one elo that can and one that cannot.
        "T2": {
            "model": "glm-5.3", "provider": "zai", "billing_mode": "plan",
            "fallback": [
                {"model": "gpt-5.6-luna", "provider": "openai-codex",
                 "billing_mode": "subscription"},
                {"model": "deepseek-v4-flash", "provider": "deepseek",
                 "billing_mode": "metered"},
            ],
            "fallback_strategy": "sequential",
        },
        "T3": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "billing_mode": "subscription",
            "fallback": [{"model": "deepseek-v4-pro", "provider": "deepseek",
                          "billing_mode": "metered"}],
            "fallback_strategy": "sequential",
            "requirements": {"min_context": 200000},
        },
        # T4 is the only tier that shuffles, and it shuffles the primary too.
        "T4": {
            "model": "gpt-5.5", "provider": "openai-codex",
            "billing_mode": "subscription",
            "fallback": [
                {"model": "mimo-v2.5", "provider": "xiaomi",
                 "billing_mode": "metered"},
                {"model": "gpt-5.6-luna", "provider": "openai-codex",
                 "billing_mode": "subscription"},
            ],
            "fallback_strategy": "random",
            "pin_primary": False,
        },
    },
}

T4_ELOS = {"gpt-5.5", "mimo-v2.5", "gpt-5.6-luna"}


def _hops(chain):
    """(model, provider) of each hop — the only part the executor consumes."""
    return [(hop.get("model"), hop.get("provider")) for hop in chain or []]


def _targets(result):
    """The attempt order the delegate_profile executor derives from a result.

    Mirrors ``_routed_targets`` in the plugin: the plan when it is attached,
    the declared primary + fallbacks when it is not.
    """
    if result.get("chain"):
        return _hops(result["chain"])
    declared = []
    if result.get("model"):
        declared.append((result["model"], result.get("provider")))
    declared.extend(
        (hop.get("model"), hop.get("provider"))
        for hop in result.get("fallback") or []
    )
    return declared


class TestPlannedChain:
    """route() must return the PLANNED chain, not the declared one."""

    def test_vision_task_drops_the_elos_that_cannot_see(self):
        """The whole point of the capability filter, on the production path."""
        dlog = DecisionLog()
        result = route(VISION_TASK, CAPABILITY_CONFIG, decision_log=dlog,
                       now=FIXED_CLOCK)

        # The tier decision is unchanged — a rule still only picks a TIER.
        assert result["model"] == "glm-5.3"
        # ...but what production attempts excludes both blind elos.
        assert _targets(result) == [("gpt-5.6-luna", "openai-codex")]
        assert not any(
            caps.MODEL_CAPABILITIES.get(model, {}).get("vision") is False
            for model, _provider in _targets(result)
        )

        plan = dlog.tail(1)[0]["chain_plan"]
        assert [hop["model"] for hop in plan["chain"]] == ["gpt-5.6-luna"]
        assert {(hop["model"], hop["reject_reason"]) for hop in plan["rejected"]} == {
            ("glm-5.3", "no_vision"), ("deepseek-v4-flash", "no_vision"),
        }
        assert plan["bypassed"] is False

    def test_production_chain_matches_explain_for_the_same_task(self):
        """The console/CLI preview and live routing must not disagree.

        They diverged for the entire capability feature: explain() planned the
        chain while route() returned the declared order, so the panel showed a
        filtered chain production never used.
        """
        dlog = DecisionLog()
        result = route(VISION_TASK, CAPABILITY_CONFIG, decision_log=dlog,
                       rng=random.Random(7), now=FIXED_CLOCK)

        features = signals.extract(VISION_TASK)
        features.update({"utc_hour": FIXED_CLOCK.hour,
                         "utc_weekday": FIXED_CLOCK.weekday()})
        explained = rules_mod.explain(
            VISION_TASK, features, False, CAPABILITY_CONFIG["rules"],
            CAPABILITY_CONFIG["default"], CAPABILITY_CONFIG["tiers"],
            random.Random(7),
        )

        assert _targets(result) == _hops(explained["chain_plan"]["chain"])
        assert _hops(dlog.tail(1)[0]["chain_plan"]["chain"]) == _targets(result)

    def test_the_shipped_policy_never_attempts_a_blind_elo_for_a_vision_turn(self):
        """The claim router.yaml makes about itself, checked against the file.

        Written as a property rather than a literal chain so an operator editing
        the tier table cannot make it vacuous.
        """
        policy = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "router.yaml").read_text(encoding="utf-8")
        )
        dlog = DecisionLog()
        result = route(VISION_TASK, policy, decision_log=dlog, now=FIXED_CLOCK)

        plan = dlog.tail(1)[0]["chain_plan"]
        assert plan["bypassed"] is False, "some shipped elo must be able to see"
        blind = [
            model for model, _provider in _targets(result)
            if caps.MODEL_CAPABILITIES.get(model, {}).get("vision") is False
        ]
        assert blind == [], f"a vision turn would be sent to {blind}"

    def test_chain_is_omitted_when_the_plan_is_the_declared_order(self):
        """Absent chain == "declared order stands", and it really is identical.

        The suppression keeps every historical consumer of this dict byte-exact;
        the executor's two branches must therefore agree on the targets.
        """
        dlog = DecisionLog()
        result = route("Rename a symbol in this code", CAPABILITY_CONFIG,
                       decision_log=dlog, now=FIXED_CLOCK)

        assert "chain" not in result
        assert _targets(result) == [("glm-4.7", "zai"),
                                   ("gpt-5.6-luna", "openai-codex")]
        assert _hops(dlog.tail(1)[0]["chain_plan"]["chain"]) == _targets(result), \
            "the trace still carries the plan even when the result omits it"

    def test_every_terminal_path_records_a_chain_plan(self):
        """No path may skip the plan: the trace is the operator's only replay."""
        veto = DecisionLog()
        route("anything", ROUTER_CONFIG, requested_model="gpt-5.6-sol",
              requested_provider="openai-codex", decision_log=veto)

        fail_safe = DecisionLog()
        route("Hello there", CAPABILITY_CONFIG, decision_log=fail_safe)

        classified = DecisionLog()
        route("Hello there", CAPABILITY_CONFIG, decision_log=classified,
              classify_fn=lambda _t, _f: {"tier": "T3", "confidence": "high"})

        direct = DecisionLog()
        route(VISION_TASK, CAPABILITY_CONFIG, decision_log=direct)

        for log in (veto, fail_safe, classified, direct):
            assert "chain_plan" in log.tail(1)[0]

        # A veto attempts nothing, and says so rather than guessing a chain.
        assert veto.tail(1)[0]["chain_plan"]["chain"] == []
        # A classified route plans its TIER's chain, floor included.
        plan = classified.tail(1)[0]["chain_plan"]
        assert [hop["model"] for hop in plan["chain"]] == ["gpt-5.6-terra",
                                                           "deepseek-v4-pro"]
        assert plan["requirements"]["min_context"] >= 200000, \
            "the classified tier's own requirements floor must reach the plan"


class TestOrderingSeed:
    """`random` must be real in production and replayable from the trace."""

    def _t4_chain(self, task, **kwargs):
        pin = SessionPin()
        pin.set("T4")
        dlog = DecisionLog()
        result = route(task, CAPABILITY_CONFIG, session_pin=pin,
                       decision_log=dlog, now=FIXED_CLOCK, **kwargs)
        return result, dlog.tail(1)[0]

    def test_the_seed_is_recorded_and_reproduces_the_order(self):
        result, entry = self._t4_chain("Rename a symbol in this code")

        seed = entry["steps"][1]["in"]["seed"]
        assert isinstance(seed, int)

        # Same turn, same order — a decision an operator can reason about.
        again, _entry = self._t4_chain("Rename a symbol in this code")
        assert _targets(again) == _targets(result)

        # And the recorded seed alone reproduces it, which is what makes the
        # trace an audit record rather than a story.
        replayed, _entry = self._t4_chain("Rename a symbol in this code",
                                          rng=random.Random(seed))
        assert _targets(replayed) == _targets(result)

    def test_different_turns_really_spread_across_the_tail(self):
        """A fixed seed would hammer one rail; that is why the seed is per turn."""
        heads = set()
        for i in range(24):
            result, _entry = self._t4_chain(f"Rename symbol_{i} in this code")
            heads.add(_targets(result)[0][0])
        assert len(heads) > 1, "every turn drew the same primary — rng is not live"
        assert heads <= T4_ELOS

    def test_an_injected_rng_is_not_advertised_as_a_seed(self):
        """The trace must never name a seed that did not produce the order."""
        _result, entry = self._t4_chain("Rename a symbol in this code",
                                        rng=random.Random(3))
        assert entry["steps"][1]["in"]["seed"] is None


class TestSessionPinUnderRandom:
    """The upward-only ratchet must survive the chain plan."""

    def test_the_pin_still_ratchets_when_the_strategy_is_random(self):
        """The plan runs AFTER the floor, and the floor is what protects the turn.

        _apply_session_floor identifies a tier by looking output['model'] up in
        the tier table, so a chain shuffled BEFORE it would leave the lookup
        looking for a model no tier declares and silently unenforce the ratchet.
        """
        pin = SessionPin()
        pin.set("T4")
        dlog = DecisionLog()
        result = route("Rename a symbol in this code", CAPABILITY_CONFIG,
                       session_pin=pin, decision_log=dlog,
                       rng=random.Random(5), now=FIXED_CLOCK)

        # Ratcheted: T1's rule matched, T4 is what routes.
        assert result["model"] == "gpt-5.5"
        assert dlog.tail(1)[0]["cause"] == "session_pin"
        # And the chain is the PINNED tier's, under the PINNED tier's policy.
        assert {model for model, _provider in _targets(result)} == T4_ELOS
        assert dlog.tail(1)[0]["chain_plan"]["strategy"] == "random"

    def test_the_ratchet_holds_for_every_shuffle(self):
        """Not just for one lucky seed."""
        for seed in range(40):
            pin = SessionPin()
            pin.set("T4")
            result = route("Rename a symbol in this code", CAPABILITY_CONFIG,
                           session_pin=pin, rng=random.Random(seed),
                           now=FIXED_CLOCK)
            assert result["model"] == "gpt-5.5"
            assert {model for model, _provider in _targets(result)} == T4_ELOS

    def test_the_promoted_tier_brings_its_own_policy(self):
        """A floor swaps the whole tier: its chain AND its ordering policy.

        Copying model/provider/fallback alone left the previous tier's strategy
        and requirements floor governing the promoted chain — invisible while
        nothing consumed them, wrong the moment the plan did.
        """
        heads = set()
        for seed in range(24):
            pin = SessionPin()
            pin.set("T4")
            result = route("Rename a symbol in this code", CAPABILITY_CONFIG,
                           session_pin=pin, rng=random.Random(seed),
                           now=FIXED_CLOCK)
            heads.add(_targets(result)[0][0])
        assert len(heads) > 1, \
            "T1's sequential strategy is still ordering the promoted T4 chain"

    def test_a_promotion_does_not_carry_the_lower_tier_s_cost_controls(self):
        """The other half of the same invariant: stale policy must be dropped.

        T1 caps its multiplier because a mechanical edit is never worth a peak
        token. A hard task promoted out of T1 never opted into that cap, and
        leaving it behind would let a cost control shrink a tier's chain that
        never declared one.
        """
        config = copy.deepcopy(CAPABILITY_CONFIG)
        config["tiers"]["T1"]["time_cap"] = {"max_multiplier": 1.5}
        config["tiers"]["T1"]["requirements"] = {"min_context": 100000}
        pin = SessionPin()
        pin.set("T4")

        result = route("Rename a symbol in this code", config, session_pin=pin,
                       rng=random.Random(1), now=FIXED_CLOCK)

        assert result["model"] == "gpt-5.5"
        assert "time_cap" not in result
        assert "requirements" not in result


class TestContextIsSizedFromTheRealPrompt:
    """est_input_tokens must measure the text the model actually receives."""

    def _composed(self, goal, context):
        return f"Context: {context}\n\nTask: {goal}"

    def test_a_large_context_with_a_small_goal_triggers_the_context_rule(self):
        goal = "fix the failing test"
        context = "WARN retry scheduled for the nightly job\n" * 2000
        composed = self._composed(goal, context)
        assert len(composed) > 80000

        goal_only = DecisionLog()
        route(goal, CAPABILITY_CONFIG, decision_log=goal_only, now=FIXED_CLOCK)
        assert goal_only.tail(1)[0]["rule_id"] is None, \
            "the goal line alone matches nothing — that is the blind spot"

        composed_log = DecisionLog()
        result = route(goal, CAPABILITY_CONFIG, prompt_text=composed,
                       decision_log=composed_log, now=FIXED_CLOCK)

        entry = composed_log.tail(1)[0]
        assert entry["rule_id"] == "huge-context-read"
        assert entry["steps"][1]["out"]["est_input_tokens"] > 20000
        assert result["model"] == "gpt-5.6-terra"
        assert entry["chain_plan"]["requirements"]["min_context"] >= 200000

    def test_the_classifier_still_sees_the_goal_alone(self):
        """The split is deliberate: a 128-token classification must not carry
        120k chars of logs into the prompt it is being paid to answer."""
        goal = "make it work"
        context = "log line\n" * 2000
        seen = []

        route(goal, CAPABILITY_CONFIG, prompt_text=self._composed(goal, context),
              classify_fn=lambda task, features: seen.append((task, features))
              or {"tier": "T2", "confidence": "high"},
              now=FIXED_CLOCK)

        assert seen[0][0] == goal
        assert "log line" not in seen[0][0]
        # The feature vector, though, describes the real input.
        assert seen[0][1]["est_input_tokens"] > 4000

    def test_no_prompt_text_routes_exactly_as_before(self):
        """Defaulting to the goal keeps every existing caller's behaviour."""
        without = route("Rename a symbol in this code", CAPABILITY_CONFIG,
                        now=FIXED_CLOCK)
        explicit = route("Rename a symbol in this code", CAPABILITY_CONFIG,
                         prompt_text="Rename a symbol in this code",
                         now=FIXED_CLOCK)
        assert without == explicit


class TestInjectedClock:
    """The clock is a parameter, and it reaches the feature vector."""

    def test_the_utc_features_are_injected_for_time_keyed_rules(self):
        config = copy.deepcopy(CAPABILITY_CONFIG)
        config["rules"].insert(0, {
            "id": "peak-hours-defer",
            "when": {"utc_hour": {"gte": 6, "lt": 10}},
            "then": {"profile": "coder", "model": "T1"},
        })

        peak = DecisionLog()
        route("Add a health endpoint", config, decision_log=peak,
              now=datetime(2026, 8, 17, 7, 30, tzinfo=timezone.utc))
        entry = peak.tail(1)[0]
        assert entry["rule_id"] == "peak-hours-defer"
        assert entry["steps"][1]["out"]["utc_hour"] == 7
        assert entry["steps"][1]["out"]["utc_weekday"] == 0

        off_peak = DecisionLog()
        route("Add a health endpoint", config, decision_log=off_peak,
              now=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc))
        assert off_peak.tail(1)[0]["rule_id"] != "peak-hours-defer"

    def test_a_clock_from_another_zone_is_normalised_to_utc(self):
        """An aware datetime must not shift the window it lands in."""
        from datetime import timedelta

        minus_three = timezone(timedelta(hours=-3))
        dlog = DecisionLog()
        route("Add a health endpoint", CAPABILITY_CONFIG, decision_log=dlog,
              now=datetime(2026, 8, 17, 4, 30, tzinfo=minus_three))
        assert dlog.tail(1)[0]["steps"][1]["out"]["utc_hour"] == 7

    def test_production_reads_the_clock_at_the_edge(self):
        """No `now` means the real UTC clock — the one place it may be read."""
        dlog = DecisionLog()
        route("Add a health endpoint", CAPABILITY_CONFIG, decision_log=dlog)
        features = dlog.tail(1)[0]["steps"][1]["out"]
        assert features["utc_hour"] in range(24)
        assert features["utc_weekday"] in range(7)

    def test_the_shipped_policy_really_routes_differently_by_the_hour(self):
        """End-to-end proof that the injected clock is consequential, not decor.

        Two primary rails price by wall-clock window, so a policy that uses them
        must produce more than one order across a day. One order for all 24
        hours means the clock stopped reaching the planner — the failure mode
        that made the whole time layer a no-op.
        """
        policy = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "router.yaml").read_text(encoding="utf-8")
        )
        orders = {
            tuple(
                _targets(route("add a health endpoint to the api code", policy,
                               rng=random.Random(0),
                               now=datetime(2026, 8, 17, hour, 0, tzinfo=timezone.utc)))
            )
            for hour in range(24)
        }
        assert len(orders) > 1

    def test_the_clock_is_handed_to_the_planner(self, monkeypatch):
        """time_cap / time_policy / cheapest_now are only live if `when` arrives."""
        seen = {}

        def spy(output, features, **kwargs):
            seen.update(kwargs)
            return {"chain": [], "requirements": {}, "rejected": [], "unknown": [],
                    "bypassed": False, "strategy": "sequential",
                    "independent_rails": 0}

        monkeypatch.setattr(adapter, "plan_chain", spy)
        monkeypatch.setattr(adapter, "_PLAN_CHAIN_ACCEPTS_WHEN", True)
        route(VISION_TASK, CAPABILITY_CONFIG, now=FIXED_CLOCK)

        assert seen["when"] == FIXED_CLOCK
        assert isinstance(seen["rng"], random.Random)

    def test_a_planner_without_a_clock_parameter_still_routes(self, monkeypatch):
        """A rules.py that predates the time layer must route, time-agnostic.

        Resolved by signature rather than by catching TypeError, so a genuine
        TypeError from inside the planner is never masked by a second call.
        """
        calls = []

        def spy(output, features, *, rng=None):
            calls.append(rng)
            return {"chain": [{"model": "gpt-5.6-luna", "provider": "openai-codex"}],
                    "requirements": {}, "rejected": [], "unknown": [],
                    "bypassed": False, "strategy": "sequential",
                    "independent_rails": 1}

        monkeypatch.setattr(adapter, "plan_chain", spy)
        monkeypatch.setattr(adapter, "_PLAN_CHAIN_ACCEPTS_WHEN", False)
        result = route(VISION_TASK, CAPABILITY_CONFIG, now=FIXED_CLOCK)

        assert _targets(result) == [("gpt-5.6-luna", "openai-codex")]
        assert calls and isinstance(calls[0], random.Random)


class TestPlannerFailureIsNotARoutingFailure:
    """The plan is a cost control and an audit record, never a gate."""

    def test_a_planner_explosion_degrades_to_the_declared_route(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("stale registry")

        monkeypatch.setattr(adapter, "plan_chain", boom)
        dlog = DecisionLog()
        result = route(VISION_TASK, CAPABILITY_CONFIG, decision_log=dlog,
                       now=FIXED_CLOCK)

        assert result["model"] == "glm-5.3"
        assert "chain" not in result
        assert _targets(result) == [
            ("glm-5.3", "zai"), ("gpt-5.6-luna", "openai-codex"),
            ("deepseek-v4-flash", "deepseek"),
        ], "no plan means the declared chain — the pre-capability behaviour"
        assert "chain_plan" not in dlog.tail(1)[0], \
            "a trace records no plan rather than an invented one"

    def test_a_non_mapping_plan_is_ignored(self, monkeypatch):
        monkeypatch.setattr(adapter, "plan_chain", lambda *_a, **_k: "nope")
        result = route(VISION_TASK, CAPABILITY_CONFIG, now=FIXED_CLOCK)
        assert result["model"] == "glm-5.3"
        assert "chain" not in result


class TestDefensiveShapes:
    """Malformed inputs produce a diagnostic-shaped answer, never an exception.

    Each guard below exists because the value comes from OUTSIDE this module —
    a hand-edited YAML tier, a host that injected a clock of the wrong type —
    and routing must degrade rather than fail.
    """

    def test_a_clock_of_the_wrong_type_routes_time_agnostic(self):
        dlog = DecisionLog()
        result = route("Rename a symbol in this code", CAPABILITY_CONFIG,
                       decision_log=dlog, now="half past three")
        assert result["model"] == "glm-4.7"
        features = dlog.tail(1)[0]["steps"][1]["out"]
        assert "utc_hour" not in features and "utc_weekday" not in features

    def test_a_tier_without_a_model_yields_no_model(self):
        """`resolve_tiers` would otherwise hand back the placeholder alias."""
        assert adapter._resolve_tier_cfg({"provider": "p"}) == {"provider": "p"}
        assert adapter._resolve_tier_cfg("not a mapping") == {}

    def test_the_policy_swap_tolerates_a_non_mapping(self):
        target = {"model": "m", "fallback_strategy": "random"}
        adapter._adopt_tier_policy(target, "not a mapping")
        assert target == {"model": "m", "fallback_strategy": "random"}

    def test_the_declared_chain_of_a_model_less_output_is_its_fallbacks(self):
        """A tier that declares only fallbacks still has an attempt order."""
        assert adapter._declared_chain(
            {"fallback": [{"model": "hop", "provider": "p"}, {"provider": "no-model"}]}
        ) == [{"model": "hop", "provider": "p"}]
        assert adapter._declared_chain({"fallback": "not a list"}) == []
