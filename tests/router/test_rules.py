"""Unit tests for rule matching engine (router/rules.py)."""

import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from router import rules as rules_mod
from router.rules import match, lint, lint_warnings, explain, plan_chain, resolve_tiers

try:
    from router import signals as signals_mod
except ImportError:  # pragma: no cover - signals always ships with the router
    signals_mod = None


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

ROUTER_CONFIG = {
    "enabled": True,
    "rules": [
        {
            # The production shape: the blocklist decision is an INJECTED boolean
            # (adapter passes it to match()), never a `model` feature — no signal
            # produces one, so a rule keyed on `model` is a dead row lint now
            # rejects. See TestLintWhenFields.
            "id": "block-codex-stall",
            "status": "stable",
            "when": {"blocked_model": {"eq": True}},
            "then": {"deny": True},
        },
        {
            "id": "trivial-mechanical-edit",
            "status": "stable",
            "when": {
                "verb_class": {"eq": "trivial"},
                "has_code": {"eq": True},
                "size_lines": {"lte": 40},
            },
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


def _mkf(**overrides):
    """Make a feature vector with sensible defaults."""
    fv = {
        "char_len": 100,
        "has_code": False,
        "size_lines": 0,
        "num_files": 0,
        "has_stacktrace": False,
        "num_requirements": 0,
        "verb_class": "unknown",
        "lang": "",
        "keywords": [],
    }
    fv.update(overrides)
    return fv


# Fixed clocks. Every time-dependent test passes one of these; a test that read a
# real clock would be the defect the injected-clock design exists to prevent.
#
# 2026-08-17 is a Monday. 06:00-10:00 UTC is peak on BOTH primary rails at once
# (deepseek every day, zai on weekdays), which is the window unattended overnight
# work lands in, so it is the hour most of these cases use.
PEAK_MONDAY = datetime(2026, 8, 17, 7, 0, tzinfo=timezone.utc)
# Same hour, Saturday: deepseek is still peak, zai is NOT (weekdays only).
PEAK_SATURDAY = datetime(2026, 8, 15, 7, 0, tzinfo=timezone.utc)
# Monday midday: off-peak on every rail in the registry.
OFF_PEAK = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
# Inside xiaomi's 16:00-24:00 CHEAP window (0.8x), which must never read as peak.
CHEAP_WINDOW = datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# match() tests
# ---------------------------------------------------------------------------

class TestMatch:
    def test_blocklist_deny_first(self):
        """The deny row is first, so it wins over a row that also matches."""
        fv = _mkf(verb_class="trivial", has_code=True, size_lines=20)
        output, rule_id = match(fv, True, ROUTER_CONFIG["rules"],
                                ROUTER_CONFIG["default"], ROUTER_CONFIG["tiers"])
        assert rule_id == "block-codex-stall"
        assert output == {"deny": True}

    def test_blocklist_deny_direct_rule(self):
        """The injected blocked_model boolean fires the deny row."""
        rules = ROUTER_CONFIG["rules"]
        default = ROUTER_CONFIG["default"]
        tiers = ROUTER_CONFIG["tiers"]

        output, rule_id = match(_mkf(), True, rules, default, tiers)
        assert rule_id == "block-codex-stall"
        assert output["deny"] is True

    def test_blocklist_row_is_inert_when_not_blocked(self):
        """blocked_model False must not fire the deny row."""
        fv = _mkf(verb_class="trivial", has_code=True, size_lines=20)
        output, rule_id = match(fv, False, ROUTER_CONFIG["rules"],
                                ROUTER_CONFIG["default"], ROUTER_CONFIG["tiers"])
        assert rule_id == "trivial-mechanical-edit"
        assert "deny" not in output

    def test_trivial_route_free(self):
        """Trivial task with code and small size → T1, no classifier."""
        fv = _mkf(verb_class="trivial", has_code=True, size_lines=20)
        rules = ROUTER_CONFIG["rules"]
        default = ROUTER_CONFIG["default"]
        tiers = ROUTER_CONFIG["tiers"]

        output, rule_id = match(fv, False, rules, default, tiers)
        assert rule_id == "trivial-mechanical-edit"
        assert output["profile"] == "coder"
        assert output["model"] == "glm-5.2-fast"
        assert output["provider"] == "zai"

    def test_trivial_too_many_lines_falls_through(self):
        """Trivial but larger than 40 lines → falls through to classify."""
        fv = _mkf(verb_class="trivial", has_code=True, size_lines=200)
        rules = ROUTER_CONFIG["rules"]
        default = ROUTER_CONFIG["default"]
        tiers = ROUTER_CONFIG["tiers"]

        output, rule_id = match(fv, False, rules, default, tiers)
        # hard-verbs doesn't match (verb is trivial), review doesn't match
        # → default: classify
        assert rule_id is None
        assert output["action"] == "classify"

    def test_hard_verb_route_strong(self):
        """Hard verb → T4 immediately, fail toward capability."""
        fv = _mkf(verb_class="hard", has_code=True)
        rules = ROUTER_CONFIG["rules"]
        default = ROUTER_CONFIG["default"]
        tiers = ROUTER_CONFIG["tiers"]

        output, rule_id = match(fv, False, rules, default, tiers)
        assert rule_id == "hard-verbs"
        assert output["model"] == "claude-opus"
        assert output["provider"] == "anthropic"
        trace = explain("Debug a race condition", fv, False, rules, default, tiers)
        assert trace["cause"] == "hard_rule"

    def test_hard_outranks_trivial(self):
        """Hard verb with small file → still goes hard (first-match, hard fires first)."""
        fv = _mkf(verb_class="hard", has_code=True, size_lines=10)
        rules = ROUTER_CONFIG["rules"]
        default = ROUTER_CONFIG["default"]
        tiers = ROUTER_CONFIG["tiers"]

        output, rule_id = match(fv, False, rules, default, tiers)
        assert rule_id == "hard-verbs"

    def test_review_keyword_classify(self):
        """Review keyword → profile=reviewer, action=classify."""
        fv = _mkf(keywords=["review"])
        rules = ROUTER_CONFIG["rules"]
        default = ROUTER_CONFIG["default"]
        tiers = ROUTER_CONFIG["tiers"]

        output, rule_id = match(fv, False, rules, default, tiers)
        assert rule_id == "review-request"
        assert output["profile"] == "reviewer"
        assert output["action"] == "classify"

    def test_default_fallthrough(self):
        """No rules match → default action=classify."""
        fv = _mkf()
        rules = ROUTER_CONFIG["rules"]
        default = ROUTER_CONFIG["default"]
        tiers = ROUTER_CONFIG["tiers"]

        output, rule_id = match(fv, False, rules, default, tiers)
        assert rule_id is None
        assert output["action"] == "classify"

    def test_first_match_semantics(self):
        """First matching rule wins, even if a later rule would also match."""
        # Create config where two rules could match
        config = {
            "rules": [
                {
                    "id": "first",
                    "when": {"has_code": {"eq": True}},
                    "then": {"profile": "coder", "model": "T1"},
                },
                {
                    "id": "second",
                    "when": {"has_code": {"eq": True}},
                    "then": {"profile": "reviewer"},
                },
            ],
            "default": {"action": "classify"},
            "tiers": ROUTER_CONFIG["tiers"],
        }
        fv = _mkf(has_code=True)
        output, rule_id = match(fv, False, config["rules"], config["default"], config["tiers"])
        assert rule_id == "first"
        assert output["profile"] == "coder"

    def test_never_silent_no_match(self):
        """Default is always present — never a silent no-match."""
        # Even with empty rules, default fires
        fv = _mkf()
        output, rule_id = match(fv, False, [], {"action": "classify"}, ROUTER_CONFIG["tiers"])
        assert rule_id is None
        assert output["action"] == "classify"


# ---------------------------------------------------------------------------
# lint() tests
# ---------------------------------------------------------------------------

class TestLint:
    def test_valid_config(self):
        errors = lint(ROUTER_CONFIG)
        assert errors == []

    def test_missing_default(self):
        config = {"rules": [], "tiers": {}}
        errors = lint(config)
        assert any("default" in e for e in errors)

    def test_missing_tiers(self):
        config = {"rules": [], "default": {"action": "classify"}}
        errors = lint(config)
        assert any("tiers" in e for e in errors)

    def test_missing_single_tier(self):
        config = {
            "rules": [],
            "default": {"action": "classify"},
            "tiers": {"T1": {}, "T2": {}, "T3": {}},  # missing T4
        }
        errors = lint(config)
        assert any("T4" in e for e in errors)

    def test_empty_rules_ok(self):
        config = {
            "rules": [],
            "default": {"action": "classify"},
            "tiers": ROUTER_CONFIG["tiers"],
        }
        errors = lint(config)
        # Empty rules is ok — default covers everything
        assert errors == []

    def test_duplicate_rule_ids(self):
        config = dict(ROUTER_CONFIG)
        config["rules"] = [
            {"id": "same", "when": {"has_code": {"eq": True}}, "then": {"profile": "coder"}},
            {"id": "same", "when": {"verb_class": {"eq": "hard"}}, "then": {"profile": "coder"}},
        ]
        config["default"] = {"action": "classify"}
        errors = lint(config)
        assert any("duplicate" in e for e in errors)

    def test_unknown_operator(self):
        config = dict(ROUTER_CONFIG)
        config["rules"] = [
            {"id": "bad", "when": {"has_code": {"regex": ".*"}}, "then": {"profile": "coder"}},
        ]
        errors = lint(config)
        assert any("unknown operator" in e for e in errors)

    def test_matches_on_wrong_field(self):
        config = dict(ROUTER_CONFIG)
        config["rules"] = [
            {
                "id": "bad-matches",
                "when": {"has_code": {"matches": "true"}},
                "then": {"profile": "coder"},
            },
        ]
        errors = lint(config)
        assert any("matches" in e.lower() for e in errors)

    def test_invalid_output_key(self):
        config = dict(ROUTER_CONFIG)
        config["rules"] = [
            {
                "id": "bad-out",
                "when": {"has_code": {"eq": True}},
                "then": {"priority": 10},
            },
        ]
        errors = lint(config)
        assert any("closed output" in e for e in errors)

    def test_shadowed_row_detected(self):
        config = dict(ROUTER_CONFIG)
        config["rules"] = [
            {
                "id": "broad",
                "when": {"has_code": {"eq": True}},
                "then": {"profile": "coder"},
            },
            {
                "id": "narrow",
                "when": {"has_code": {"eq": True}},
                "then": {"profile": "reviewer"},  # same when → shadowed
            },
        ]
        errors = lint(config)
        assert any("shadowed" in e for e in errors)

    def test_deny_must_be_bool(self):
        config = dict(ROUTER_CONFIG)
        config["rules"] = [
            {
                "id": "bad-deny",
                "when": {"has_code": {"eq": True}},
                "then": {"deny": "yes"},
            },
        ]
        errors = lint(config)
        assert any("deny" in e.lower() for e in errors)

    def test_empty_config(self):
        errors = lint({})
        assert len(errors) > 0

    def test_rules_missing_when_then(self):
        errors = lint({
            "rules": [{"id": "bare"}],
            "default": {"action": "classify"},
            "tiers": ROUTER_CONFIG["tiers"],
        })
        assert any("when" in e.lower() for e in errors)

    def test_unknown_tier_reference(self):
        config = dict(ROUTER_CONFIG)
        config["rules"] = [
            {
                "id": "bad-tier",
                "when": {"has_code": {"eq": True}},
                "then": {"model": "T99"},
            },
        ]
        errors = lint(config)
        assert any("T99" in e for e in errors)


# ---------------------------------------------------------------------------
# explain() tests
# ---------------------------------------------------------------------------

class TestExplain:
    def test_explain_trivial_route(self):
        fv = _mkf(verb_class="trivial", has_code=True, size_lines=20)
        result = explain(
            "Rename getCwd in 3 files, 20 lines",
            fv,
            False,
            ROUTER_CONFIG["rules"],
            ROUTER_CONFIG["default"],
            ROUTER_CONFIG["tiers"],
        )
        assert result["matched_rule_id"] == "trivial-mechanical-edit"
        assert result["output"]["profile"] == "coder"
        assert result["output"]["model"] == "glm-5.2-fast"
        assert result["cause"] == "has_code_rule"

    def test_explain_default(self):
        fv = _mkf()
        result = explain(
            "Hello",
            fv,
            False,
            ROUTER_CONFIG["rules"],
            ROUTER_CONFIG["default"],
            ROUTER_CONFIG["tiers"],
        )
        assert result["matched_rule_id"] is None
        assert result["cause"] == "default_fallthrough"

    def test_explain_blocklist(self):
        fv = _mkf()
        result = explain(
            "Use gpt-5.6-sol",
            fv,
            True,
            ROUTER_CONFIG["rules"],
            ROUTER_CONFIG["default"],
            ROUTER_CONFIG["tiers"],
        )
        assert result["cause"] == "blocklist_veto"
        assert result["output"]["deny"] is True

    def test_explain_reports_the_injected_blocked_model_chip(self):
        """blocked_model is injected, not extracted: it still needs a chip.

        The console renders matched_clauses as the "because ..." chips, so a
        blocked_model-only rule that reported none explained itself with nothing.
        """
        result = explain(
            "Use gpt-5.6-sol", _mkf(), True,
            ROUTER_CONFIG["rules"], ROUTER_CONFIG["default"], ROUTER_CONFIG["tiers"],
        )
        assert result["matched_clauses"] == {"blocked_model": {"eq": True}}


# ---------------------------------------------------------------------------
# Capability / fallback-strategy fixtures
# ---------------------------------------------------------------------------

# Tiers that declare the new knobs. Providers are deliberately distinct so the
# shared-upstream warning does not fire unless a test asks for it.
CAPS_TIERS = {
    "T1": {"model": "glm-5.2-fast", "provider": "zai"},
    "T2": {
        "model": "glm-5.2",
        "provider": "zai",
        "fallback_strategy": "random",
        "pin_primary": False,
        "billing_mode": "plan",
        "requirements": {"min_context": 128000},
    },
    "T3": {"model": "claude-sonnet", "provider": "anthropic"},
    "T4": {
        "model": "text-only-elo",
        "provider": "openai-codex",
        "vision": False,
        "fallback": [
            {"model": "vision-elo", "provider": "zai", "vision": True},
            {"model": "another-text-elo", "provider": "deepseek", "vision": False},
        ],
    },
}


def _vision_features(**overrides):
    fv = _mkf(needs_vision=True)
    fv.update(overrides)
    return fv


# ---------------------------------------------------------------------------
# _resolve_tiers: new tier knobs
# ---------------------------------------------------------------------------

class TestResolveTierKnobs:
    def test_undeclared_knobs_are_not_materialised(self):
        """A tier declaring nothing new resolves exactly as before."""
        resolved = resolve_tiers({"model": "T1"}, CAPS_TIERS)
        assert resolved == {"model": "glm-5.2-fast", "provider": "zai"}

    def test_defaults_apply_at_plan_time(self):
        """Defaults are sequential / pin_primary True even with nothing declared."""
        resolved = resolve_tiers({"model": "T1"}, CAPS_TIERS)
        plan = plan_chain(resolved, _mkf())
        assert plan["strategy"] == "sequential"
        assert rules_mod._pin_primary_of(resolved) is True

    def test_declared_knobs_carried(self):
        resolved = resolve_tiers({"profile": "coder", "model": "T2"}, CAPS_TIERS)
        assert resolved["fallback_strategy"] == "random"
        assert resolved["pin_primary"] is False
        assert resolved["billing_mode"] == "plan"
        assert resolved["requirements"] == {"min_context": 128000}
        # untouched passthrough
        assert resolved["profile"] == "coder"
        assert resolved["model"] == "glm-5.2"

    def test_declared_capabilities_carried_for_primary(self):
        resolved = resolve_tiers({"model": "T4"}, CAPS_TIERS)
        assert resolved["declared_capabilities"] == {"vision": False}

    def test_requirements_filtered_to_closed_set(self):
        tiers = {"T1": {"model": "m", "provider": "p",
                        "requirements": {"min_context": 10, "gpu": True}}}
        resolved = resolve_tiers({"model": "T1"}, tiers)
        assert resolved["requirements"] == {"min_context": 10}

    def test_non_bool_pin_primary_normalised(self):
        tiers = {"T1": {"model": "m", "provider": "p", "pin_primary": "yes"}}
        assert resolve_tiers({"model": "T1"}, tiers)["pin_primary"] is True

    def test_non_string_strategy_normalised(self):
        tiers = {"T1": {"model": "m", "provider": "p", "fallback_strategy": 7}}
        assert resolve_tiers({"model": "T1"}, tiers)["fallback_strategy"] == "sequential"

    def test_never_mutates_tier_or_output(self):
        tiers = {
            "T1": {
                "model": "m", "provider": "p",
                "requirements": {"min_context": 10},
                "fallback": [{"model": "f", "provider": "q"}],
            }
        }
        snapshot = {"T1": {
            "model": "m", "provider": "p",
            "requirements": {"min_context": 10},
            "fallback": [{"model": "f", "provider": "q"}],
        }}
        output = {"model": "T1"}
        resolved = resolve_tiers(output, tiers)
        resolved["requirements"]["min_context"] = 999
        resolved["fallback"][0]["model"] = "clobbered"
        assert tiers == snapshot
        assert output == {"model": "T1"}


# ---------------------------------------------------------------------------
# plan_chain()
# ---------------------------------------------------------------------------

class TestPlanChain:
    def test_builds_declared_chain_in_declared_order(self):
        resolved = resolve_tiers({"model": "T4"}, CAPS_TIERS)
        plan = plan_chain(resolved, _mkf())
        assert [hop["model"] for hop in plan["chain"]] == [
            "text-only-elo", "vision-elo", "another-text-elo",
        ]
        assert plan["strategy"] == "sequential"
        assert plan["bypassed"] is False
        assert plan["rejected"] == []
        assert plan["independent_rails"] == 3

    def test_returns_the_full_contract_keys(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, CAPS_TIERS), _mkf())
        assert set(plan) == {
            "chain", "requirements", "rejected", "unknown", "bypassed",
            "unsatisfiable", "strategy", "strategy_declared", "strategy_degraded",
            "strategy_degraded_reason", "pin_primary", "independent_rails",
            "time_agnostic", "time_cap_bypassed", "capped", "demoted",
            "promoted", "multipliers",
        }

    def test_clock_keys_appear_only_with_a_clock(self):
        """A null hour reads as midnight in a JSON consumer, so it is omitted."""
        resolved = resolve_tiers({"model": "T1"}, CAPS_TIERS)
        blind = plan_chain(resolved, _mkf())
        assert blind["time_agnostic"] is True
        assert "utc_hour" not in blind and "utc_weekday" not in blind

        timed = plan_chain(resolved, _mkf(), when=PEAK_MONDAY)
        assert timed["time_agnostic"] is False
        assert (timed["utc_hour"], timed["utc_weekday"]) == (7, 0)

    def test_plan_carries_pin_primary(self):
        """F7: the console reads plan.pin_primary and defaults it to True."""
        assert plan_chain(resolve_tiers({"model": "T2"}, CAPS_TIERS),
                          _mkf())["pin_primary"] is False
        assert plan_chain(resolve_tiers({"model": "T1"}, CAPS_TIERS),
                          _mkf())["pin_primary"] is True

    def test_plan_is_json_serialisable(self):
        """The plan is persisted to routes.jsonl: no datetime may leak into it."""
        plan = plan_chain(resolve_tiers({"model": "T4"}, CAPS_TIERS),
                          _mkf(), when=PEAK_MONDAY)
        assert json.loads(json.dumps(plan))["utc_hour"] == 7

    def test_filters_elo_that_cannot_do_vision(self):
        """A vision task drops the hops that declare vision: false."""
        resolved = resolve_tiers({"model": "T4"}, CAPS_TIERS)
        plan = plan_chain(resolved, _vision_features())
        assert plan["requirements"]["vision"] is True
        assert [hop["model"] for hop in plan["chain"]] == ["vision-elo"]
        rejected = {hop["model"]: hop["reject_reason"] for hop in plan["rejected"]}
        assert rejected == {
            "text-only-elo": "no_vision", "another-text-elo": "no_vision",
        }
        assert plan["bypassed"] is False

    def test_context_requirement_derived_from_signals(self):
        """est_input_tokens flows into min_context — no new operator needed."""
        tiers = {"T1": {
            "model": "small-ctx", "provider": "p", "context_window": 8000,
            "fallback": [{"model": "big-ctx", "provider": "q",
                          "context_window": 1000000}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers),
                          _mkf(est_input_tokens=400000))
        assert plan["requirements"]["min_context"] == 500000
        assert [hop["model"] for hop in plan["chain"]] == ["big-ctx"]
        assert plan["rejected"][0]["reject_reason"] == "context_too_small"

    def test_tier_floor_unioned_with_signals(self):
        resolved = resolve_tiers({"model": "T2"}, CAPS_TIERS)
        plan = plan_chain(resolved, _mkf(est_input_tokens=0))
        assert plan["requirements"] == {"min_context": 128000}

    def test_bypasses_when_nothing_qualifies(self):
        """Filtering must never empty the chain and break routing.

        The per-elo reasons are RETAINED on a bypass (phase-2 contract change):
        "nothing can meet this requirement" is only actionable next to which
        requirement each elo failed, and a bypass is exactly when the operator
        needs to know whether the requirement or the tier is wrong.
        """
        tiers = {"T1": {
            "model": "text-a", "provider": "p", "vision": False,
            "fallback": [{"model": "text-b", "provider": "q", "vision": False}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _vision_features())
        assert plan["bypassed"] is True
        assert [hop["model"] for hop in plan["chain"]] == ["text-a", "text-b"]
        assert {hop["model"]: hop["reject_reason"] for hop in plan["rejected"]} == {
            "text-a": "no_vision", "text-b": "no_vision",
        }

    def test_random_ordering_reproducible_under_seeded_rng(self):
        tiers = {"T1": {
            "model": "p0", "provider": "a", "fallback_strategy": "random",
            "fallback": [
                {"model": "p1", "provider": "b"},
                {"model": "p2", "provider": "c"},
                {"model": "p3", "provider": "d"},
                {"model": "p4", "provider": "e"},
            ],
        }}
        resolved = resolve_tiers({"model": "T1"}, tiers)
        first = plan_chain(resolved, _mkf(), rng=random.Random(7))["chain"]
        second = plan_chain(resolved, _mkf(), rng=random.Random(7))["chain"]
        assert [h["model"] for h in first] == [h["model"] for h in second]
        # pin_primary defaults to True: the tier's own elo stays the first hop
        assert first[0]["model"] == "p0"
        assert sorted(h["model"] for h in first) == ["p0", "p1", "p2", "p3", "p4"]
        assert plan_chain(resolved, _mkf(), rng=random.Random(7))["strategy"] == "random"

    def test_random_without_pin_primary_may_move_the_primary(self):
        tiers = {"T1": {
            "model": "p0", "provider": "a",
            "fallback_strategy": "random", "pin_primary": False,
            "fallback": [{"model": f"p{i}", "provider": f"pr{i}"} for i in range(1, 6)],
        }}
        resolved = resolve_tiers({"model": "T1"}, tiers)
        orders = {
            tuple(h["model"] for h in plan_chain(resolved, _mkf(), rng=random.Random(s))["chain"])
            for s in range(30)
        }
        assert any(order[0] != "p0" for order in orders)
        # still reproducible per seed
        assert (
            [h["model"] for h in plan_chain(resolved, _mkf(), rng=random.Random(3))["chain"]]
            == [h["model"] for h in plan_chain(resolved, _mkf(), rng=random.Random(3))["chain"]]
        )

    def test_random_without_rng_falls_back_to_sequential(self):
        """Purity: no rng means no global randomness."""
        tiers = {"T1": {
            "model": "p0", "provider": "a", "fallback_strategy": "random",
            "pin_primary": False,
            "fallback": [{"model": f"p{i}", "provider": f"pr{i}"} for i in range(1, 6)],
        }}
        resolved = resolve_tiers({"model": "T1"}, tiers)
        plan = plan_chain(resolved, _mkf())
        assert [h["model"] for h in plan["chain"]] == ["p0", "p1", "p2", "p3", "p4", "p5"]

    def test_unknown_strategy_is_sequential(self):
        tiers = {"T1": {
            "model": "p0", "provider": "a", "fallback_strategy": "round_robin",
            "fallback": [{"model": "p1", "provider": "b"}],
        }}
        resolved = resolve_tiers({"model": "T1"}, tiers)
        plan = plan_chain(resolved, _mkf(), rng=random.Random(1))
        assert [h["model"] for h in plan["chain"]] == ["p0", "p1"]

    def test_unknown_capability_stays_eligible(self):
        tiers = {"T1": {"model": "totally-made-up-elo-xyz", "provider": "p"}}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _vision_features())
        assert [h["model"] for h in plan["chain"]] == ["totally-made-up-elo-xyz"]
        assert plan["unknown"] == ["totally-made-up-elo-xyz"]
        assert plan["rejected"] == []

    def test_no_model_output_yields_empty_chain(self):
        plan = plan_chain({"action": "classify"}, _mkf())
        assert plan["chain"] == []
        assert plan["bypassed"] is False

    def test_does_not_mutate_the_resolved_output(self):
        resolved = resolve_tiers({"model": "T4"}, CAPS_TIERS)
        before = {
            "model": resolved["model"],
            "fallback": [dict(h) for h in resolved["fallback"]],
        }
        plan_chain(resolved, _vision_features(), rng=random.Random(0))
        assert resolved["model"] == before["model"]
        assert resolved["fallback"] == before["fallback"]

    def test_degrades_when_registry_missing(self, monkeypatch):
        """Without router.capabilities the chain is declared order, unfiltered."""
        monkeypatch.setattr(rules_mod, "_caps", None)
        resolved = resolve_tiers({"model": "T4"}, CAPS_TIERS)
        plan = plan_chain(resolved, _vision_features(), rng=random.Random(0))
        assert [h["model"] for h in plan["chain"]] == [
            "text-only-elo", "vision-elo", "another-text-elo",
        ]
        assert plan["requirements"] == {}
        assert plan["bypassed"] is False
        assert plan["rejected"] == [] and plan["unknown"] == []
        assert plan["independent_rails"] == 3

    def test_degrades_when_registry_raises(self, monkeypatch):
        class _Broken:
            @staticmethod
            def derive_requirements(features, tier_requirements=None):
                raise TypeError("stale registry")

        monkeypatch.setattr(rules_mod, "_caps", _Broken)
        plan = plan_chain(resolve_tiers({"model": "T1"}, CAPS_TIERS), _mkf())
        assert [h["model"] for h in plan["chain"]] == ["glm-5.2-fast"]
        assert plan["requirements"] == {}
        assert plan["bypassed"] is False


# ---------------------------------------------------------------------------
# The plan carries `unsatisfiable` (NEW: the filter computed it and plan_chain
# dropped it, so "this request is pathological" was unreportable)
# ---------------------------------------------------------------------------

class TestUnsatisfiableIsCarried:
    """``unsatisfiable`` must reach the trace, not die one stack frame up.

    ``capabilities.filter_chain`` names the requirement keys no available model
    could ever meet, computed against MAX_REGISTERED_CONTEXT on every request.
    Without it in the plan the console shows ``bypassed: true`` plus three
    coincidental ``context_too_small`` rejections and the operator has to
    reconstruct "the floor is above every window that exists" by hand — the exact
    reconstruction the key was added to remove.
    """

    #: Both hops are real registry elos, so the floor below is above every
    #: declared context_window as well as above MAX_REGISTERED_CONTEXT.
    TIERS = {"T1": {
        "model": "glm-4.7", "provider": "zai",
        "fallback": [{"model": "deepseek-v4-pro", "provider": "deepseek"}],
    }}

    def _plan(self, **features):
        return plan_chain(resolve_tiers({"model": "T1"}, self.TIERS), _mkf(**features))

    def test_a_pathological_floor_is_named_in_the_plan(self):
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("MAX_REGISTERED_CONTEXT lives in the capability registry")
        plan = self._plan(est_input_tokens=2_000_000)
        assert plan["unsatisfiable"] == ["min_context"]
        # ... and it is a DIFFERENT fact from the per-elo rejections.
        assert plan["bypassed"] is True
        assert {h["reject_reason"] for h in plan["rejected"]} == {"context_too_small"}

    def test_the_plan_agrees_with_the_filter_it_came_from(self):
        """One answer, carried — never a second derivation that could differ."""
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("the filter lives in the capability registry")
        features = _mkf(est_input_tokens=2_000_000)
        resolved = resolve_tiers({"model": "T1"}, self.TIERS)
        requirements = rules_mod._caps.derive_requirements(features, None)
        direct = rules_mod._caps.filter_chain(
            rules_mod._build_chain(resolved), requirements,
        )
        assert plan_chain(resolved, features)["unsatisfiable"] == (
            direct["unsatisfiable"]
        )

    def test_an_ordinary_rejection_is_not_pathological(self):
        """A floor one elo can meet is not "no model could ever meet this"."""
        tiers = {"T1": {
            "model": "small-ctx", "provider": "p", "context_window": 8000,
            "fallback": [{"model": "big-ctx", "provider": "q",
                          "context_window": 1000000}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers),
                          _mkf(est_input_tokens=400000))
        assert plan["unsatisfiable"] == []
        assert [h["reject_reason"] for h in plan["rejected"]] == ["context_too_small"]

    def test_an_ordinary_request_reports_the_empty_list(self):
        assert self._plan(est_input_tokens=10)["unsatisfiable"] == []

    def test_the_key_survives_a_json_round_trip(self):
        """It has to reach routes.jsonl and the console, not just the dict."""
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("MAX_REGISTERED_CONTEXT lives in the capability registry")
        plan = plan_chain(resolve_tiers({"model": "T1"}, self.TIERS),
                          _mkf(est_input_tokens=2_000_000), when=PEAK_MONDAY)
        assert json.loads(json.dumps(plan))["unsatisfiable"] == ["min_context"]

    def test_the_degraded_shapes_carry_the_key_too(self, monkeypatch):
        """Shape stability: a consumer must not have to test for its presence."""
        assert rules_mod._empty_chain_plan()["unsatisfiable"] == []
        monkeypatch.setattr(rules_mod, "_caps", None)
        plan = plan_chain(resolve_tiers({"model": "T1"}, self.TIERS),
                          _mkf(est_input_tokens=2_000_000))
        assert plan["unsatisfiable"] == []

    def test_a_registry_that_omits_the_key_is_tolerated(self, monkeypatch):
        """An older filter_chain returning no `unsatisfiable` is not a crash."""
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("the filter lives in the capability registry")
        monkeypatch.setattr(
            rules_mod._caps, "filter_chain",
            lambda chain, requirements: {
                "eligible": list(chain), "rejected": [], "unknown": [],
                "bypassed": False,
            },
        )
        assert self._plan()["unsatisfiable"] == []


# ---------------------------------------------------------------------------
# explain() carries chain_plan
# ---------------------------------------------------------------------------

class TestExplainChainPlan:
    def test_explain_carries_chain_plan(self):
        fv = _mkf(verb_class="hard", has_code=True)
        result = explain(
            "Debug a race condition", fv, False,
            ROUTER_CONFIG["rules"], ROUTER_CONFIG["default"], ROUTER_CONFIG["tiers"],
        )
        plan = result["chain_plan"]
        assert [h["model"] for h in plan["chain"]] == ["claude-opus"]
        assert plan["strategy"] == "sequential"
        assert plan["bypassed"] is False

    def test_explain_chain_plan_shows_rejections(self):
        rules = [{
            "id": "vision-task",
            "when": {"needs_vision": {"eq": True}},
            "then": {"profile": "coder", "model": "T4"},
        }]
        result = explain(
            "Look at this screenshot", _vision_features(), False,
            rules, {"action": "classify"}, CAPS_TIERS,
        )
        plan = result["chain_plan"]
        assert [h["model"] for h in plan["chain"]] == ["vision-elo"]
        assert {h["reject_reason"] for h in plan["rejected"]} == {"no_vision"}

    def test_explain_preview_is_stable_across_calls(self):
        """Fixed default seed: the console preview must not churn on reload."""
        rules = [{
            "id": "random-tier",
            "when": {"has_code": {"eq": True}},
            "then": {"model": "T2"},
        }]
        tiers = {"T2": dict(CAPS_TIERS["T2"], fallback=[
            {"model": f"f{i}", "provider": f"pr{i}"} for i in range(1, 6)
        ])}
        fv = _mkf(has_code=True)
        first = explain("x", fv, False, rules, {"action": "classify"}, tiers)
        second = explain("x", fv, False, rules, {"action": "classify"}, tiers)
        assert (
            [h["model"] for h in first["chain_plan"]["chain"]]
            == [h["model"] for h in second["chain_plan"]["chain"]]
        )

    def test_explain_passes_rng_through(self):
        rules = [{
            "id": "random-tier",
            "when": {"has_code": {"eq": True}},
            "then": {"model": "T2"},
        }]
        tiers = {"T2": dict(CAPS_TIERS["T2"], fallback=[
            {"model": f"f{i}", "provider": f"pr{i}"} for i in range(1, 6)
        ])}
        fv = _mkf(has_code=True)
        traced = explain("x", fv, False, rules, {"action": "classify"}, tiers,
                         rng=random.Random(11))
        direct = plan_chain(resolve_tiers({"model": "T2"}, tiers), fv,
                            rng=random.Random(11))
        assert (
            [h["model"] for h in traced["chain_plan"]["chain"]]
            == [h["model"] for h in direct["chain"]]
        )

    def test_explain_survives_a_planner_that_raises(self, monkeypatch):
        """A broken planner degrades the trace, it does not break the decision."""
        def boom(*_args, **_kwargs):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(rules_mod, "plan_chain", boom)
        result = explain(
            "Debug a race condition", _mkf(verb_class="hard"), False,
            ROUTER_CONFIG["rules"], ROUTER_CONFIG["default"], ROUTER_CONFIG["tiers"],
        )
        assert result["matched_rule_id"] == "hard-verbs"
        assert result["chain_plan"] == {
            "chain": [], "requirements": {}, "rejected": [], "unknown": [],
            "bypassed": False, "unsatisfiable": [], "strategy": "sequential",
            "strategy_declared": "sequential", "strategy_degraded": False,
            "strategy_degraded_reason": "", "pin_primary": True,
            "independent_rails": 0, "time_agnostic": True,
            "time_cap_bypassed": False, "capped": [], "demoted": [],
            "promoted": [], "multipliers": {},
        }

    def test_explain_keeps_legacy_keys(self):
        result = explain(
            "Hello", _mkf(), False,
            ROUTER_CONFIG["rules"], ROUTER_CONFIG["default"], ROUTER_CONFIG["tiers"],
        )
        assert set(result) == {
            "matched_rule_id", "output", "matched_clauses", "cause", "chain_plan",
        }


# ---------------------------------------------------------------------------
# lint(): new tier hard errors
# ---------------------------------------------------------------------------

def _cfg(tiers):
    """A minimal valid config wrapping the given tier table."""
    base = {"T1": {"model": "m1", "provider": "p1"},
            "T2": {"model": "m2", "provider": "p2"},
            "T3": {"model": "m3", "provider": "p3"},
            "T4": {"model": "m4", "provider": "p4"}}
    base.update(tiers)
    return {"rules": [], "default": {"action": "classify"}, "tiers": base}


class TestLintTierKnobs:
    def test_valid_knobs_lint_clean(self):
        assert lint(_cfg({"T2": {
            "model": "m2", "provider": "p2",
            "fallback_strategy": "random",
            "pin_primary": False,
            "billing_mode": "subscription",
            "requirements": {"min_context": 200000, "vision": True},
            "fallback": [{"model": "f1", "provider": "q1"}],
        }})) == []

    def test_bad_fallback_strategy(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "fallback_strategy": "round_robin"}}))
        assert (
            "tier 'T2': 'fallback_strategy' must be one of "
            "cheapest_now, random, sequential"
        ) in errors

    def test_good_fallback_strategy_does_not_fire(self):
        for strategy in sorted(rules_mod._fallback_strategies()):
            errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                       "fallback_strategy": strategy}}))
            assert not any("fallback_strategy" in e for e in errors)

    def test_strategy_set_comes_from_the_registry(self):
        """One closed set: lint cannot reject a strategy order_chain supports."""
        if rules_mod._caps is None:
            pytest.skip("the strategy set lives in the capability registry")
        assert rules_mod._fallback_strategies() == rules_mod._caps.FALLBACK_STRATEGIES
        assert "cheapest_now" in rules_mod._fallback_strategies()

    def test_bad_pin_primary(self):
        errors = lint(_cfg({"T3": {"model": "m3", "provider": "p3",
                                   "pin_primary": "yes"}}))
        assert "tier 'T3': 'pin_primary' must be boolean" in errors

    def test_good_pin_primary_does_not_fire(self):
        errors = lint(_cfg({"T3": {"model": "m3", "provider": "p3",
                                   "pin_primary": True}}))
        assert not any("pin_primary" in e for e in errors)

    def test_bad_billing_mode(self):
        errors = lint(_cfg({"T1": {"model": "m1", "provider": "p1",
                                   "billing_mode": "gift-card"}}))
        expected = (
            f"tier 'T1': 'billing_mode' must be one of "
            f"{sorted(rules_mod._billing_modes())}"
        )
        assert expected in errors

    def test_good_billing_mode_does_not_fire(self):
        for mode in sorted(rules_mod._billing_modes()):
            errors = lint(_cfg({"T1": {"model": "m1", "provider": "p1",
                                       "billing_mode": mode}}))
            assert not any("billing_mode" in e for e in errors)

    def test_requirement_key_outside_closed_set(self):
        errors = lint(_cfg({"T4": {"model": "m4", "provider": "p4",
                                   "requirements": {"gpu": True}}}))
        assert "tier 'T4': 'requirements.gpu' not in closed requirement set" in errors

    def test_closed_requirement_keys_do_not_fire(self):
        errors = lint(_cfg({"T4": {"model": "m4", "provider": "p4", "requirements": {
            "min_context": 1000, "vision": True,
            "tool_calling": True, "structured_output": True,
        }}}))
        assert not any("requirement set" in e for e in errors)

    def test_malformed_fallback_hop(self):
        errors = lint(_cfg({"T3": {"model": "m3", "provider": "p3", "fallback": [
            {"model": "ok", "provider": "q"},
            {"model": "no-provider"},
            "not-a-mapping",
        ]}}))
        assert "tier 'T3': fallback[1] must be a mapping with 'model' and 'provider'" in errors
        assert "tier 'T3': fallback[2] must be a mapping with 'model' and 'provider'" in errors
        assert not any("fallback[0]" in e for e in errors)

    def test_unhashable_knob_values_are_reported_not_raised(self):
        """`x in frozenset` raises for an unhashable x, and YAML can produce one.

        lint() is the write gate: a list where a strategy belongs has to come back
        as a diagnostic, never as a TypeError through the operator's apply.
        """
        for value in (["random"], {"random": True}, 7, None):
            errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                       "fallback_strategy": value,
                                       "billing_mode": value}}))
            assert any("fallback_strategy" in e for e in errors)
            assert any("billing_mode" in e for e in errors)

    def test_lint_never_returns_warnings(self):
        """The shared-upstream / unknown-model findings must not block a write."""
        config = _cfg({"T2": {
            "model": "glm-5.2", "provider": "zai",
            "fallback": [{"model": "glm-5.3", "provider": "zai"}],
        }})
        assert lint(config) == []
        assert lint_warnings(config)


# ---------------------------------------------------------------------------
# lint_warnings()
# ---------------------------------------------------------------------------

class TestLintWarnings:
    def test_shared_upstream_pair_warns(self):
        config = _cfg({"T4": {
            "model": "m4", "provider": "zai",
            "fallback": [{"model": "f1", "provider": "zai"},
                         {"model": "f2", "provider": "deepseek"}],
        }})
        assert (
            "tier 'T4': first two hops share upstream 'zai' — no independent fallback"
            in lint_warnings(config)
        )

    def test_reseller_upstream_pair_warns(self):
        """nous is a white-label reseller in front of openrouter — one rail."""
        if rules_mod._caps is None:
            pytest.skip("upstream aliasing lives in the capability registry")
        config = _cfg({"T4": {
            "model": "m4", "provider": "nous",
            "fallback": [{"model": "f1", "provider": "openrouter"}],
        }})
        assert any(
            "first two hops share upstream 'openrouter'" in w
            for w in lint_warnings(config)
        )

    def test_independent_pair_does_not_warn(self):
        config = _cfg({"T4": {
            "model": "m4", "provider": "openai-codex",
            "fallback": [{"model": "f1", "provider": "zai"}],
        }})
        assert not any("share upstream" in w for w in lint_warnings(config))

    def test_unknown_model_warns(self):
        if rules_mod._caps is None:
            pytest.skip("registry required to know a model is unknown")
        config = _cfg({"T1": {"model": "totally-made-up-elo-xyz", "provider": "p1"}})
        assert (
            "tier 'T1': model 'totally-made-up-elo-xyz' is unknown to the "
            "capability registry and declares no capabilities"
        ) in lint_warnings(config)

    def test_declared_capabilities_silence_the_unknown_warning(self):
        config = _cfg({"T1": {"model": "totally-made-up-elo-xyz", "provider": "p1",
                              "context_window": 200000, "vision": False}})
        assert not any(
            w.startswith("tier 'T1'") and "unknown to the capability registry" in w
            for w in lint_warnings(config)
        )

    def test_warnings_never_raise_on_garbage(self):
        assert lint_warnings(None) == []
        assert lint_warnings({}) == []
        assert lint_warnings({"tiers": "nope"}) == []
        assert lint_warnings({"tiers": {"T1": "nope"}}) == []


# ---------------------------------------------------------------------------
# lint_warnings() folds in the capability registry's own self-check
#
# These tests CORRUPT router.capabilities.MODEL_CAPABILITIES and assert the
# diagnostic reaches lint_warnings(). Monkeypatching registry_diagnostics to a
# stub would prove only that a stub is called — the defect being closed here is
# precisely that nothing called the real function, so nothing but the real
# registry, really corrupted, can show it now does.
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(monkeypatch):
    """The live capability registry, or a skip. Mutations are undone by pytest."""
    if rules_mod._caps is None:  # pragma: no cover - registry always ships
        pytest.skip("the registry self-check lives in the capability registry")
    return rules_mod._caps


class TestRegistryDiagnosticsAreFoldedIn:
    #: Any lintable config; these findings are about the registry, not the file.
    CONFIG = {"rules": [], "default": {"action": "classify"},
              "tiers": {"T1": {"model": "glm-4.7", "provider": "zai"},
                        "T2": {"model": "glm-4.7", "provider": "zai"},
                        "T3": {"model": "glm-4.7", "provider": "zai"},
                        "T4": {"model": "glm-4.7", "provider": "zai"}}}

    def test_the_shipped_registry_is_clean(self, registry):
        """The baseline the cases below move away from."""
        assert registry.registry_diagnostics() == []
        assert lint_warnings(self.CONFIG) == []

    def test_overlapping_registry_windows_reach_lint_warnings(
        self, registry, monkeypatch
    ):
        """Two matching windows make the multiplier an accident of list order.

        Nothing else can catch it: _lint_price_windows only sees windows the
        OPERATOR declared in YAML, so a defect shipped in MODEL_CAPABILITIES was
        invisible to every gate — while the router took the first match and the
        console took the last, pricing the same hour differently.
        """
        monkeypatch.setitem(
            registry.MODEL_CAPABILITIES["glm-4.7"], "price_windows",
            [{"hours_utc": [6, 10], "multiplier": 2.0},
             {"hours_utc": [8, 12], "multiplier": 3.0}],
        )
        assert (
            "model 'glm-4.7': price_windows entries overlap"
            in registry.registry_diagnostics()
        )
        assert (
            "model 'glm-4.7': price_windows entries overlap"
            in lint_warnings(self.CONFIG)
        )

    def test_a_missing_required_field_reaches_lint_warnings(
        self, registry, monkeypatch
    ):
        monkeypatch.setitem(
            registry.MODEL_CAPABILITIES, "broken-elo",
            {"provider": "zai", "vision": False, "tool_calling": False,
             "structured_output": False, "billing_mode": "metered"},
        )
        warnings = lint_warnings(self.CONFIG)
        assert "model 'broken-elo': missing required field 'context_window'" in warnings
        assert "model 'broken-elo': context_window must be a positive int" in warnings

    def test_the_strings_are_appended_verbatim(self, registry, monkeypatch):
        """Already shaped `model '<id>': <defect>` — no re-wrapping, no loss."""
        monkeypatch.setitem(
            registry.MODEL_CAPABILITIES["glm-4.7"], "billing_mode", "gift-card",
        )
        problems = registry.registry_diagnostics()
        assert problems
        assert set(problems) <= set(lint_warnings(self.CONFIG))

    def test_a_registry_defect_never_blocks_a_write(self, registry, monkeypatch):
        """The operator cannot fix MODEL_CAPABILITIES from YAML.

        Contrast _lint_price_windows, where the overlapping windows are the
        operator's own and a hard error is actionable.
        """
        monkeypatch.setitem(
            registry.MODEL_CAPABILITIES["glm-4.7"], "price_windows",
            [{"hours_utc": [6, 10], "multiplier": 2.0},
             {"hours_utc": [8, 12], "multiplier": 3.0}],
        )
        assert lint(self.CONFIG) == []
        assert lint_warnings(self.CONFIG)

    def test_the_fold_in_does_not_depend_on_the_config(self, registry, monkeypatch):
        """A registry defect is a property of the router, not of the file.

        lint_warnings bails early on a config with no usable `tiers`; the registry
        check runs before that, or a corrupt registry would be reported for some
        configs and hidden for others.
        """
        monkeypatch.setitem(
            registry.MODEL_CAPABILITIES["glm-4.7"], "context_window", -1,
        )
        expected = "model 'glm-4.7': context_window must be a positive int"
        assert expected in lint_warnings({})
        assert expected in lint_warnings(None)
        assert expected in lint_warnings({"tiers": "nope"})

    def test_a_self_check_that_raises_degrades_to_silence(self, monkeypatch):
        """A registry too broken to describe itself must not kill the report."""
        def boom():
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(rules_mod._caps, "registry_diagnostics", boom,
                            raising=False)
        config = _cfg({"T1": {"model": "totally-made-up-elo-xyz", "provider": "p1"}})
        warnings = lint_warnings(config)
        assert any("unknown to the capability registry" in w for w in warnings)

    def test_a_registry_without_the_check_is_tolerated(self, monkeypatch):
        """No self-check to fold in is not a defect: the rest of lint still runs.

        Uses this class's CONFIG rather than _cfg(), whose placeholder models are
        unknown to the registry and so earn the unknown-model warning on their own
        merits. That warning is a genuine operator signal — a tier naming a model
        the router cannot verify — and it is not what this case is about.
        """
        monkeypatch.delattr(rules_mod._caps, "registry_diagnostics", raising=False)
        assert lint_warnings(self.CONFIG) == []

    def test_no_registry_at_all_is_tolerated(self, monkeypatch):
        monkeypatch.setattr(rules_mod, "_caps", None)
        assert lint_warnings(_cfg({})) == []


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestLegacyConfigRegression:
    def test_legacy_config_lints_clean_and_resolves_unchanged(self):
        """A router.yaml with none of the new keys behaves exactly as before."""
        assert lint(ROUTER_CONFIG) == []
        assert resolve_tiers({"profile": "coder", "model": "T4"},
                             ROUTER_CONFIG["tiers"]) == {
            "profile": "coder", "model": "claude-opus", "provider": "anthropic",
        }
        output, rule_id = match(
            _mkf(verb_class="trivial", has_code=True, size_lines=20),
            False, ROUTER_CONFIG["rules"], ROUTER_CONFIG["default"],
            ROUTER_CONFIG["tiers"],
        )
        assert rule_id == "trivial-mechanical-edit"
        assert output == {"profile": "coder", "model": "glm-5.2-fast",
                          "provider": "zai"}

    def test_legacy_features_without_new_signals_still_route(self):
        """Adding signal keys is backward compatible; absent ones just skip."""
        plan = plan_chain(resolve_tiers({"model": "T4"}, ROUTER_CONFIG["tiers"]),
                          _mkf())
        assert [h["model"] for h in plan["chain"]] == ["claude-opus"]
        assert plan["requirements"] == {}

    def test_context_predicates_use_existing_operators_only(self):
        """est_input_tokens routes through gt — the operator set stays closed."""
        rules = [{
            "id": "huge-context",
            "when": {"est_input_tokens": {"gt": 200000}},
            "then": {"profile": "coder", "model": "T4"},
        }]
        config = {"rules": rules, "default": {"action": "classify"},
                  "tiers": ROUTER_CONFIG["tiers"]}
        assert lint(config) == []
        _out, rule_id = match(_mkf(est_input_tokens=400000), False, rules,
                              config["default"], config["tiers"])
        assert rule_id == "huge-context"
        _out, rule_id = match(_mkf(est_input_tokens=1000), False, rules,
                              config["default"], config["tiers"])
        assert rule_id is None


# ---------------------------------------------------------------------------
# Shadow detection reasons about conditions, not key sets (F2)
# ---------------------------------------------------------------------------

def _rules_cfg(*rules):
    """A minimal valid config wrapping the given rule rows."""
    return {
        "rules": list(rules),
        "default": {"action": "classify"},
        "tiers": ROUTER_CONFIG["tiers"],
    }


def _shadow_errors(config):
    return [e for e in lint(config) if "shadowed" in e]


class TestShadowDetection:
    def test_two_disjoint_context_thresholds_lint_clean(self):
        """The headline feature: multi-threshold context routing must ship.

        Phase 1 compared key SETS, so one field named twice was shadowed
        whatever the values were, and lint() is the write gate — these two rows
        could not be applied through plan/apply at all.
        """
        config = _rules_cfg(
            {"id": "gigantic-context-read",
             "when": {"est_input_tokens": {"gt": 800000}},
             "then": {"model": "T4"}},
            {"id": "tiny-context-fast-path",
             "when": {"est_input_tokens": {"lt": 2000}},
             "then": {"model": "T1"}},
        )
        assert lint(config) == []

    def test_disjoint_equality_values_are_not_shadowed(self):
        """The exact pair phase 1 got wrong: contradictory, so both reachable."""
        assert rules_mod._is_shadowed({"x": {"eq": 1}}, {"x": {"eq": 2}}) is False

    def test_specific_before_general_lints_clean(self):
        """(800k, inf) does not contain (400k, inf): the second row still fires."""
        config = _rules_cfg(
            {"id": "gigantic", "when": {"est_input_tokens": {"gt": 800000}},
             "then": {"model": "T4"}},
            {"id": "huge", "when": {"est_input_tokens": {"gt": 400000}},
             "then": {"model": "T3"}},
        )
        assert lint(config) == []

    def test_contained_interval_is_still_reported(self):
        """General before specific IS dead: >800k already matched >400k."""
        config = _rules_cfg(
            {"id": "huge", "when": {"est_input_tokens": {"gt": 400000}},
             "then": {"model": "T3"}},
            {"id": "gigantic", "when": {"est_input_tokens": {"gt": 800000}},
             "then": {"model": "T4"}},
        )
        assert "rule 'gigantic' is shadowed by earlier rule 'huge'" in lint(config)

    def test_the_headline_containment_pair_is_still_caught(self):
        """gt 100000 then gt 500000: every value the later admits, the earlier did."""
        assert rules_mod._is_shadowed(
            {"est_input_tokens": {"gt": 100000}},
            {"est_input_tokens": {"gt": 500000}},
        ) is True

    def test_overlapping_but_uncontained_ranges_lint_clean(self):
        config = _rules_cfg(
            {"id": "mid", "when": {"est_input_tokens": {"gt": 100000, "lt": 500000}},
             "then": {"model": "T2"}},
            {"id": "upper", "when": {"est_input_tokens": {"gt": 300000, "lt": 900000}},
             "then": {"model": "T3"}},
        )
        assert _shadow_errors(config) == []

    def test_bounded_window_inside_an_open_range_is_reported(self):
        config = _rules_cfg(
            {"id": "over-100k", "when": {"est_input_tokens": {"gte": 100000}},
             "then": {"model": "T2"}},
            {"id": "200k-to-300k",
             "when": {"est_input_tokens": {"gt": 200000, "lt": 300000}},
             "then": {"model": "T3"}},
        )
        assert "rule '200k-to-300k' is shadowed by earlier rule 'over-100k'" in lint(config)

    def test_half_open_boundary_is_not_shadowed(self):
        """gte 400000 admits exactly 400000; gt 400000 does not."""
        config = _rules_cfg(
            {"id": "strict", "when": {"est_input_tokens": {"gt": 400000}},
             "then": {"model": "T3"}},
            {"id": "inclusive", "when": {"est_input_tokens": {"gte": 400000}},
             "then": {"model": "T4"}},
        )
        assert _shadow_errors(config) == []

    def test_identical_conditions_are_still_shadowed(self):
        config = _rules_cfg(
            {"id": "broad", "when": {"has_code": {"eq": True}},
             "then": {"profile": "coder"}},
            {"id": "narrow", "when": {"has_code": {"eq": True}},
             "then": {"profile": "reviewer"}},
        )
        assert "rule 'narrow' is shadowed by earlier rule 'broad'" in lint(config)

    def test_fewer_fields_with_identical_shared_clause_is_shadowed(self):
        config = _rules_cfg(
            {"id": "any-code", "when": {"has_code": {"eq": True}},
             "then": {"model": "T2"}},
            {"id": "trivial-code",
             "when": {"has_code": {"eq": True}, "verb_class": {"eq": "trivial"}},
             "then": {"model": "T1"}},
        )
        assert "rule 'trivial-code' is shadowed by earlier rule 'any-code'" in lint(config)

    def test_more_fields_never_shadows_fewer(self):
        """The earlier row constrains a field the later row leaves free."""
        config = _rules_cfg(
            {"id": "trivial-code",
             "when": {"has_code": {"eq": True}, "verb_class": {"eq": "trivial"}},
             "then": {"model": "T1"}},
            {"id": "any-code", "when": {"has_code": {"eq": True}},
             "then": {"model": "T2"}},
        )
        assert _shadow_errors(config) == []

    def test_disjoint_equality_values_lint_clean(self):
        config = _rules_cfg(
            {"id": "hard", "when": {"verb_class": {"eq": "hard"}},
             "then": {"model": "T4"}},
            {"id": "trivial", "when": {"verb_class": {"eq": "trivial"}},
             "then": {"model": "T1"}},
        )
        assert _shadow_errors(config) == []

    def test_membership_containment_is_reported(self):
        config = _rules_cfg(
            {"id": "either", "when": {"verb_class": {"in": ["hard", "trivial"]}},
             "then": {"model": "T3"}},
            {"id": "just-trivial", "when": {"verb_class": {"eq": "trivial"}},
             "then": {"model": "T1"}},
        )
        assert "rule 'just-trivial' is shadowed by earlier rule 'either'" in lint(config)

    def test_membership_the_other_way_round_lints_clean(self):
        config = _rules_cfg(
            {"id": "just-trivial", "when": {"verb_class": {"eq": "trivial"}},
             "then": {"model": "T1"}},
            {"id": "either", "when": {"verb_class": {"in": ["hard", "trivial"]}},
             "then": {"model": "T3"}},
        )
        assert _shadow_errors(config) == []

    def test_exclusion_containing_a_membership_is_reported(self):
        """`ne: hard` admits every non-hard verb, trivial among them."""
        config = _rules_cfg(
            {"id": "not-hard", "when": {"verb_class": {"ne": "hard"}},
             "then": {"model": "T2"}},
            {"id": "trivial", "when": {"verb_class": {"eq": "trivial"}},
             "then": {"model": "T1"}},
        )
        assert "rule 'trivial' is shadowed by earlier rule 'not-hard'" in lint(config)

    def test_exclusion_of_the_same_value_lints_clean(self):
        config = _rules_cfg(
            {"id": "not-hard", "when": {"verb_class": {"ne": "hard"}},
             "then": {"model": "T2"}},
            {"id": "hard", "when": {"verb_class": {"eq": "hard"}},
             "then": {"model": "T4"}},
        )
        assert _shadow_errors(config) == []

    def test_undecidable_substring_conditions_are_silence(self):
        """Two `contains` clauses: containment is not decidable, so no error."""
        config = _rules_cfg(
            {"id": "review", "when": {"keywords": {"contains": "review"}},
             "then": {"model": "T4"}},
            {"id": "refactor", "when": {"keywords": {"contains": "refactor"}},
             "then": {"model": "T3"}},
        )
        assert _shadow_errors(config) == []

    def test_non_numeric_bound_is_undecidable_not_shadowed(self):
        config = _rules_cfg(
            {"id": "stringly", "when": {"est_input_tokens": {"gt": "200k"}},
             "then": {"model": "T3"}},
            {"id": "numeric", "when": {"est_input_tokens": {"gt": 400000}},
             "then": {"model": "T4"}},
        )
        assert _shadow_errors(config) == []

    def test_clock_thresholds_compose_with_context_thresholds(self):
        """Two fields, two rows, disjoint hours: both must ship."""
        config = _rules_cfg(
            {"id": "peak-heavy",
             "when": {"utc_hour": {"gte": 6, "lt": 10},
                      "est_input_tokens": {"gt": 200000}},
             "then": {"model": "T1"}},
            {"id": "off-peak-heavy",
             "when": {"utc_hour": {"gte": 12, "lt": 16},
                      "est_input_tokens": {"gt": 200000}},
             "then": {"model": "T3"}},
        )
        assert lint(config) == []

    def test_shipped_policy_has_no_shadowed_rows(self):
        assert _shadow_errors(ROUTER_CONFIG) == []


# ---------------------------------------------------------------------------
# A tier must declare its own model + provider (F3)
# ---------------------------------------------------------------------------

class TestLintTierIdentity:
    def test_missing_model_is_an_error(self):
        errors = lint(_cfg({"T2": {"provider": "zai"}}))
        assert "tier 'T2': missing 'model'" in errors

    def test_missing_provider_is_an_error(self):
        errors = lint(_cfg({"T2": {"model": "glm-5.3"}}))
        assert "tier 'T2': missing 'provider'" in errors

    def test_empty_tier_reports_both(self):
        errors = lint(_cfg({"T2": {}}))
        assert "tier 'T2': missing 'model'" in errors
        assert "tier 'T2': missing 'provider'" in errors

    def test_blank_model_is_an_error(self):
        errors = lint(_cfg({"T2": {"model": "   ", "provider": "zai"}}))
        assert "tier 'T2': 'model' must be a non-empty string" in errors

    def test_non_string_provider_is_an_error(self):
        errors = lint(_cfg({"T2": {"model": "glm-5.3", "provider": 7}}))
        assert "tier 'T2': 'provider' must be a non-empty string" in errors

    def test_complete_tier_does_not_fire(self):
        errors = lint(_cfg({"T2": {"model": "glm-5.3", "provider": "zai"}}))
        assert not any("missing 'model'" in e or "missing 'provider'" in e
                       for e in errors)

    def test_the_defect_this_closes(self):
        """A dropped `model:` line resolved to the literal alias, past the gate."""
        tiers = dict(_cfg({})["tiers"], T2={"provider": "zai"})
        assert resolve_tiers({"model": "T2"}, tiers)["model"] == "T2"
        assert "tier 'T2': missing 'model'" in lint(
            {"rules": [], "default": {"action": "classify"}, "tiers": tiers}
        )


# ---------------------------------------------------------------------------
# when.<field> names are validated against signals (F9)
# ---------------------------------------------------------------------------

class TestLintWhenFields:
    def test_typo_in_a_signal_name_is_an_error(self):
        config = _rules_cfg({"id": "vision-required",
                             "when": {"need_vision": {"eq": True}},
                             "then": {"model": "T2"}})
        assert "rule 'vision-required': 'when.need_vision' is not a known signal" in lint(config)

    def test_the_correct_name_does_not_fire(self):
        config = _rules_cfg({"id": "vision-required",
                             "when": {"needs_vision": {"eq": True}},
                             "then": {"model": "T2"}})
        assert lint(config) == []

    def test_every_known_signal_name_lints_clean(self):
        """The list is IMPORTED from signals, so the two cannot drift."""
        if signals_mod is None:
            pytest.skip("signals module required")
        for field in sorted(signals_mod.KNOWN_FEATURE_NAMES):
            config = _rules_cfg({"id": "row", "when": {field: {"eq": 1}},
                                 "then": {"model": "T1"}})
            assert not any("known signal" in e for e in lint(config)), field

    def test_the_vocabulary_is_read_from_signals_not_mirrored(self):
        """A mirror drifts; a drifted mirror blocks a legitimate field."""
        if signals_mod is None:
            pytest.skip("signals module required")
        assert rules_mod._known_when_fields() == (
            frozenset(signals_mod.KNOWN_FEATURE_NAMES)
            | rules_mod._INJECTED_WHEN_FIELDS
        )

    def test_injected_blocked_model_is_a_known_field(self):
        config = _rules_cfg({"id": "veto", "when": {"blocked_model": {"eq": True}},
                             "then": {"deny": True}})
        assert lint(config) == []

    def test_injected_clock_fields_are_known(self):
        config = _rules_cfg({"id": "off-peak",
                             "when": {"utc_hour": {"gte": 6, "lt": 10},
                                      "utc_weekday": {"in": [0, 1, 2, 3, 4]}},
                             "then": {"model": "T1"}})
        assert lint(config) == []

    def test_check_is_skipped_without_the_signals_module(self, monkeypatch):
        """No canonical list means no guess: a mirror would drift and block."""
        monkeypatch.setattr(rules_mod, "_signals", None)
        config = _rules_cfg({"id": "row", "when": {"whatever": {"eq": 1}},
                             "then": {"model": "T1"}})
        assert not any("known signal" in e for e in lint(config))

    def test_shipped_policy_names_only_known_signals(self):
        assert not any("known signal" in e for e in lint(ROUTER_CONFIG))


class TestLintClockBounds:
    def test_hour_out_of_range_is_an_error(self):
        config = _rules_cfg({"id": "bad-hour", "when": {"utc_hour": {"eq": 25}},
                             "then": {"model": "T1"}})
        assert "rule 'bad-hour': 'when.utc_hour' must be bounded to 0..23" in lint(config)

    def test_weekday_out_of_range_is_an_error(self):
        config = _rules_cfg({"id": "bad-day", "when": {"utc_weekday": {"gte": 7}},
                             "then": {"model": "T1"}})
        assert "rule 'bad-day': 'when.utc_weekday' must be bounded to 0..6" in lint(config)

    def test_in_list_member_out_of_range_is_an_error(self):
        config = _rules_cfg({"id": "bad-list",
                             "when": {"utc_weekday": {"in": [0, 1, 9]}},
                             "then": {"model": "T1"}})
        assert "rule 'bad-list': 'when.utc_weekday' must be bounded to 0..6" in lint(config)

    def test_non_numeric_bound_is_an_error(self):
        config = _rules_cfg({"id": "stringly", "when": {"utc_hour": {"gte": "6"}},
                             "then": {"model": "T1"}})
        assert "rule 'stringly': 'when.utc_hour' must be bounded to 0..23" in lint(config)

    def test_in_range_bounds_do_not_fire(self):
        config = _rules_cfg({"id": "peak", "when": {"utc_hour": {"gte": 6, "lt": 10}},
                             "then": {"model": "T1"}})
        assert lint(config) == []

    def test_half_open_end_of_day_is_accepted(self):
        """`[16, 24)` is how the price windows read, and it is satisfiable."""
        config = _rules_cfg({"id": "evening",
                             "when": {"utc_hour": {"gte": 16, "lt": 24}},
                             "then": {"model": "T1"}})
        assert lint(config) == []


# ---------------------------------------------------------------------------
# default.model is checked against the tier table (F10)
# ---------------------------------------------------------------------------

class TestLintDefaultTier:
    def test_unknown_tier_in_default_is_an_error(self):
        config = {"rules": [], "default": {"profile": "coder", "model": "T9"},
                  "tiers": ROUTER_CONFIG["tiers"]}
        assert "default: 'model' references unknown tier 'T9'" in lint(config)

    def test_known_tier_in_default_does_not_fire(self):
        config = {"rules": [], "default": {"profile": "coder", "model": "T1"},
                  "tiers": ROUTER_CONFIG["tiers"]}
        assert lint(config) == []

    def test_classifier_default_does_not_fire(self):
        assert lint(ROUTER_CONFIG) == []

    def test_concrete_model_in_default_does_not_fire(self):
        config = {"rules": [], "default": {"model": "glm-4.7"},
                  "tiers": ROUTER_CONFIG["tiers"]}
        assert lint(config) == []

    def test_the_defect_this_closes(self):
        """Every classifier fall-through resolved to a literal model "T9"."""
        assert resolve_tiers({"model": "T9"}, ROUTER_CONFIG["tiers"]) == {"model": "T9"}


class TestTierAliasPattern:
    def test_capital_t_model_id_is_not_a_tier_reference(self):
        config = _rules_cfg({"id": "concrete", "when": {"has_code": {"eq": True}},
                             "then": {"model": "Titan-70B"}})
        assert lint(config) == []

    def test_tier_shaped_name_is_still_checked(self):
        config = _rules_cfg({"id": "bad-tier", "when": {"has_code": {"eq": True}},
                             "then": {"model": "T99"}})
        assert "rule 'bad-tier': 'then.model' references unknown tier 'T99'" in lint(config)

    def test_custom_tier_name_present_in_the_table_resolves(self):
        tiers = dict(ROUTER_CONFIG["tiers"], fast={"model": "m", "provider": "p"})
        config = _rules_cfg({"id": "custom", "when": {"has_code": {"eq": True}},
                             "then": {"model": "fast"}})
        config["tiers"] = tiers
        assert lint(config) == []


# ---------------------------------------------------------------------------
# requirements: values are validated, and the floor only tightens
# ---------------------------------------------------------------------------

class TestRequirementValues:
    def test_non_integer_min_context_is_an_error(self):
        errors = lint(_cfg({"T4": {"model": "m4", "provider": "p4",
                                   "requirements": {"min_context": "lots"}}}))
        assert "tier 'T4': 'requirements.min_context' must be a positive integer" in errors

    def test_boolean_min_context_is_an_error(self):
        errors = lint(_cfg({"T4": {"model": "m4", "provider": "p4",
                                   "requirements": {"min_context": True}}}))
        assert "tier 'T4': 'requirements.min_context' must be a positive integer" in errors

    def test_zero_min_context_is_an_error(self):
        errors = lint(_cfg({"T4": {"model": "m4", "provider": "p4",
                                   "requirements": {"min_context": 0}}}))
        assert "tier 'T4': 'requirements.min_context' must be a positive integer" in errors

    def test_non_boolean_capability_requirement_is_an_error(self):
        errors = lint(_cfg({"T4": {"model": "m4", "provider": "p4",
                                   "requirements": {"vision": "yes"}}}))
        assert "tier 'T4': 'requirements.vision' must be a boolean" in errors

    def test_well_typed_values_do_not_fire(self):
        errors = lint(_cfg({"T4": {"model": "m4", "provider": "p4", "requirements": {
            "min_context": 200000, "vision": True,
            "tool_calling": True, "structured_output": False,
        }}}))
        assert errors == []

    def test_the_defect_this_closes(self):
        """A string min_context is DISCARDED at plan time, floor silently gone."""
        tiers = {"T1": {"model": "small-ctx", "provider": "p",
                        "context_window": 8000,
                        "requirements": {"min_context": "lots"}}}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf())
        assert plan["requirements"] == {}

    def test_a_quoted_number_is_refused_at_the_gate_though_it_would_work(self):
        """Deliberate strictness, matching `pin_primary: "yes"`.

        The registry coerces a numeric string, so runtime stays permissive and a
        stale file still routes; lint is stricter so the file says what it means.
        """
        tiers = {"T1": {"model": "m", "provider": "p",
                        "requirements": {"min_context": "200000"}}}
        assert plan_chain(resolve_tiers({"model": "T1"}, tiers),
                          _mkf())["requirements"] == {"min_context": 200000}
        assert (
            "tier 'T1': 'requirements.min_context' must be a positive integer"
            in lint(_cfg(tiers))
        )


class TestRequirementFloorOnlyTightens:
    def test_false_boolean_cannot_lower_a_signal_requirement(self):
        """A floor raises a requirement; it must never relax one."""
        tiers = {"T1": {
            "model": "text-only", "provider": "p", "vision": False,
            "requirements": {"vision": False},
            "fallback": [{"model": "sees", "provider": "q", "vision": True}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _vision_features())
        assert plan["requirements"]["vision"] is True
        assert [h["model"] for h in plan["chain"]] == ["sees"]

    def test_true_boolean_still_tightens(self):
        tiers = {"T1": {
            "model": "text-only", "provider": "p", "vision": False,
            "requirements": {"vision": True},
            "fallback": [{"model": "sees", "provider": "q", "vision": True}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf())
        assert plan["requirements"] == {"vision": True}
        assert [h["model"] for h in plan["chain"]] == ["sees"]

    def test_min_context_floor_takes_the_maximum(self):
        tiers = {"T1": {"model": "m", "provider": "p",
                        "requirements": {"min_context": 128000}}}
        resolved = resolve_tiers({"model": "T1"}, tiers)
        assert plan_chain(resolved, _mkf(est_input_tokens=400000))["requirements"] == {
            "min_context": 500000
        }
        assert plan_chain(resolved, _mkf(est_input_tokens=8))["requirements"] == {
            "min_context": 128000
        }


# ---------------------------------------------------------------------------
# The plan reports the strategy that actually ran (F15)
# ---------------------------------------------------------------------------

RANDOM_TIER = {"T1": {
    "model": "p0", "provider": "a", "fallback_strategy": "random",
    "pin_primary": False,
    "fallback": [{"model": f"p{i}", "provider": f"pr{i}"} for i in range(1, 4)],
}}

CHEAPEST_TIER = {"T1": {
    "model": "gpt-5.6-terra", "provider": "openai-codex",
    "fallback_strategy": "cheapest_now", "pin_primary": False,
    "fallback": [
        {"model": "deepseek-v4-flash", "provider": "deepseek"},
        {"model": "gpt-5.6-luna", "provider": "openai-codex"},
    ],
}}


class TestStrategyDegraded:
    def test_random_without_rng_is_reported_as_sequential(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, RANDOM_TIER), _mkf())
        assert plan["strategy"] == "sequential"
        assert plan["strategy_declared"] == "random"
        assert plan["strategy_degraded"] is True
        assert "rng" in plan["strategy_degraded_reason"]

    def test_random_with_rng_is_not_degraded(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, RANDOM_TIER), _mkf(),
                          rng=random.Random(7))
        assert plan["strategy"] == "random"
        assert plan["strategy_degraded"] is False
        assert plan["strategy_degraded_reason"] == ""

    def test_cheapest_now_without_a_clock_is_reported_as_sequential(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, CHEAPEST_TIER), _mkf())
        assert plan["strategy"] == "sequential"
        assert plan["strategy_declared"] == "cheapest_now"
        assert plan["strategy_degraded"] is True
        assert "clock" in plan["strategy_degraded_reason"]
        assert [h["model"] for h in plan["chain"]] == [
            "gpt-5.6-terra", "deepseek-v4-flash", "gpt-5.6-luna",
        ]

    def test_cheapest_now_with_a_clock_is_not_degraded(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, CHEAPEST_TIER), _mkf(),
                          when=OFF_PEAK)
        assert plan["strategy"] == "cheapest_now"
        assert plan["strategy_degraded"] is False

    def test_unknown_strategy_is_reported_as_degraded(self):
        tiers = {"T1": {"model": "p0", "provider": "a",
                        "fallback_strategy": "round_robin",
                        "fallback": [{"model": "p1", "provider": "b"}]}}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          rng=random.Random(1))
        assert plan["strategy"] == "sequential"
        assert plan["strategy_declared"] == "round_robin"
        assert plan["strategy_degraded"] is True
        assert "round_robin" in plan["strategy_degraded_reason"]

    def test_sequential_is_never_reported_as_degraded(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, CAPS_TIERS), _mkf())
        assert plan["strategy"] == "sequential"
        assert plan["strategy_degraded"] is False

    def test_registry_absence_degrades_and_says_so(self, monkeypatch):
        monkeypatch.setattr(rules_mod, "_caps", None)
        plan = plan_chain(resolve_tiers({"model": "T1"}, RANDOM_TIER), _mkf(),
                          rng=random.Random(7))
        assert plan["strategy"] == "sequential"
        assert plan["strategy_declared"] == "random"
        assert plan["strategy_degraded"] is True
        assert "registry" in plan["strategy_degraded_reason"]
        assert plan["pin_primary"] is False

    def test_registry_absence_still_reports_the_hour_and_the_cap(self, monkeypatch):
        """The degraded plan is shape-stable: no stage ran, and it says so."""
        monkeypatch.setattr(rules_mod, "_caps", None)
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert plan["utc_hour"] == 7 and plan["time_agnostic"] is False
        assert plan["time_cap"] == {"max_multiplier": 1.5}
        assert plan["capped"] == [] and plan["time_cap_bypassed"] is False
        assert plan["multipliers"] == {}
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]


# ---------------------------------------------------------------------------
# Time layer: cap, policy, ordering — one fixed order of operations
# ---------------------------------------------------------------------------

# gpt-5.6-terra never has a window; deepseek-v4-pro is 2.0x at 01-04 and 06-10
# every day; glm-5.3 is 2.0x at 06-10 on WEEKDAYS only.
TIME_TIERS = {
    "T1": {
        "model": "gpt-5.6-terra", "provider": "openai-codex",
        "billing_mode": "subscription",
        "time_cap": {"max_multiplier": 1.5},
        "fallback": [
            {"model": "deepseek-v4-pro", "provider": "deepseek",
             "billing_mode": "metered"},
            {"model": "glm-5.3", "provider": "zai", "billing_mode": "plan"},
        ],
    },
    "T2": {
        "model": "gpt-5.6-terra", "provider": "openai-codex",
        "billing_mode": "subscription",
        "time_policy": {"avoid_peak": ["deepseek", "zai"]},
        "fallback": [
            {"model": "deepseek-v4-pro", "provider": "deepseek",
             "billing_mode": "metered"},
            {"model": "mimo-v2.5", "provider": "xiaomi", "billing_mode": "metered"},
        ],
    },
    "T3": {  # every elo peaks at 06:00-10:00 on a weekday: the cap must bypass
        "model": "deepseek-v4-pro", "provider": "deepseek",
        "billing_mode": "metered",
        "time_cap": {"max_multiplier": 1.5},
        "fallback": [{"model": "glm-5.3", "provider": "zai",
                      "billing_mode": "plan"}],
    },
    "T4": {
        "model": "gpt-5.6-terra", "provider": "openai-codex",
        "billing_mode": "subscription",
        "time_policy": {"prefer": ["mimo-v2.5"]},
        "fallback": [
            {"model": "deepseek-v4-pro", "provider": "deepseek",
             "billing_mode": "metered"},
            {"model": "mimo-v2.5", "provider": "xiaomi", "billing_mode": "metered"},
        ],
    },
}


def _models(plan):
    return [hop["model"] for hop in plan["chain"]]


class _NeedsRegistry:
    """Mixin for the cases whose facts (prices, windows) live in the registry."""

    @pytest.fixture(autouse=True)
    def _registry_required(self):
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("the time layer lives in the capability registry")


class TestTimeCap(_NeedsRegistry):
    def test_cap_drops_the_rails_that_are_peaking(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra"]
        assert plan["capped"] == [
            {"model": "deepseek-v4-pro", "multiplier": 2.0},
            {"model": "glm-5.3", "multiplier": 2.0},
        ]
        assert plan["time_cap_bypassed"] is False
        assert plan["time_cap"] == {"max_multiplier": 1.5}

    def test_off_peak_keeps_every_rail(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=OFF_PEAK)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == []
        assert plan["time_cap_bypassed"] is False

    def test_the_weekend_exempts_zai_but_not_deepseek(self):
        """The `weekdays` key is the whole point: same hour, different answer."""
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_SATURDAY)
        assert _models(plan) == ["gpt-5.6-terra", "glm-5.3"]
        assert [c["model"] for c in plan["capped"]] == ["deepseek-v4-pro"]

    def test_bypass_restores_the_chain_and_keeps_the_diagnostics(self):
        """A cost control must never be able to cause an outage."""
        plan = plan_chain(resolve_tiers({"model": "T3"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["deepseek-v4-pro", "glm-5.3"]
        assert plan["time_cap_bypassed"] is True
        assert plan["capped"] == [
            {"model": "deepseek-v4-pro", "multiplier": 2.0},
            {"model": "glm-5.3", "multiplier": 2.0},
        ]
        assert plan["multipliers"] == {"deepseek-v4-pro": 2.0, "glm-5.3": 2.0}

    def test_cap_is_a_ceiling_not_a_strict_bound(self):
        tiers = {"T1": dict(TIME_TIERS["T1"], time_cap={"max_multiplier": 2.0})}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == []

    def test_no_clock_means_no_cap(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf())
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == []
        assert plan["time_cap_bypassed"] is False

    def test_undeclared_cap_omits_the_key(self):
        """A null cap reads as a ceiling of 0x in a JSON consumer."""
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert "time_cap" not in plan

    def test_a_bare_number_cap_is_honoured_at_plan_time(self):
        """Runtime is permissive so a stale file still routes; lint is the gate."""
        output = dict(resolve_tiers({"model": "T2"}, TIME_TIERS), time_cap=1.5)
        plan = plan_chain(output, _mkf(), when=PEAK_MONDAY)
        assert [c["model"] for c in plan["capped"]] == ["deepseek-v4-pro"]
        assert plan["time_cap"] == {"max_multiplier": 1.5}
        assert "tier 'T2': 'time_cap' must be a mapping" in lint(
            _cfg({"T2": {"model": "m2", "provider": "p2", "time_cap": 1.5}})
        )

    def test_no_tier_can_reach_the_bare_number_branch(self):
        """_time_cap_of tolerates one; the file cannot spell it. Both fail closed.

        The docstring used to promise a bare number was a real spelling, which is
        two layers from true: _resolve_tiers drops a non-mapping time_cap and lint
        refuses it outright, so a tier declaring `time_cap: 1.5` gets NO cap.
        """
        tiers = {"T1": {"model": "gpt-5.6-terra", "provider": "openai-codex",
                        "time_cap": 1.5,
                        "fallback": [{"model": "deepseek-v4-pro",
                                      "provider": "deepseek"}]}}
        resolved = resolve_tiers({"model": "T1"}, tiers)
        assert "time_cap" not in resolved
        plan = plan_chain(resolved, _mkf(), when=PEAK_MONDAY)
        assert plan["capped"] == [] and "time_cap" not in plan
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro"]

    def test_an_unusable_cap_is_no_cap_never_a_cap_of_zero(self):
        output = dict(resolve_tiers({"model": "T2"}, TIME_TIERS),
                      time_cap={"max_multiplier": "1.5"})
        plan = plan_chain(output, _mkf(), when=PEAK_MONDAY)
        assert plan["capped"] == []
        assert "time_cap" not in plan


class TestTimePolicy(_NeedsRegistry):
    def test_avoid_peak_demotes_without_removing(self):
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "mimo-v2.5", "deepseek-v4-pro"]
        assert plan["demoted"] == ["deepseek-v4-pro"]
        assert plan["promoted"] == []

    def test_nothing_moves_off_peak(self):
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                          when=OFF_PEAK)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "mimo-v2.5"]
        assert plan["demoted"] == []

    def test_prefer_promotes_an_off_peak_model(self):
        plan = plan_chain(resolve_tiers({"model": "T4"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan)[0] == "mimo-v2.5"
        assert plan["promoted"] == ["mimo-v2.5"]

    def test_a_cheap_window_is_not_a_peak(self):
        """xiaomi's 0.8x window must never read as "avoid this now"."""
        plan = plan_chain(resolve_tiers({"model": "T4"}, TIME_TIERS), _mkf(),
                          when=CHEAP_WINDOW)
        assert plan["promoted"] == ["mimo-v2.5"]
        assert plan["multipliers"]["mimo-v2.5"] == 0.8

    def test_no_clock_means_no_policy(self):
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf())
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "mimo-v2.5"]
        assert plan["demoted"] == [] and plan["promoted"] == []

    def test_policy_is_always_a_permutation(self):
        for when in (PEAK_MONDAY, PEAK_SATURDAY, OFF_PEAK, CHEAP_WINDOW, None):
            plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                              when=when)
            assert sorted(_models(plan)) == [
                "deepseek-v4-pro", "gpt-5.6-terra", "mimo-v2.5",
            ]


class TestOrderOfOperations(_NeedsRegistry):
    def test_cap_runs_before_policy(self):
        """An elo the cap removed cannot be reported as demoted by the policy."""
        tiers = {"T1": dict(TIME_TIERS["T1"],
                            time_policy={"avoid_peak": ["deepseek", "zai"]})}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra"]
        assert [c["model"] for c in plan["capped"]] == [
            "deepseek-v4-pro", "glm-5.3",
        ]
        assert plan["demoted"] == []

    def test_policy_still_demotes_what_the_cap_allowed(self):
        """Same tier, cap raised: now the policy is the stage that moves them."""
        tiers = {"T1": dict(TIME_TIERS["T1"],
                            time_cap={"max_multiplier": 2.0},
                            time_policy={"avoid_peak": ["deepseek", "zai"]})}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == []
        assert plan["demoted"] == ["deepseek-v4-pro", "glm-5.3"]

    def test_capability_filter_runs_before_the_policy(self):
        """A promotion of an elo the filter removed never took effect."""
        tiers = {"T1": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "time_policy": {"prefer": ["deepseek-v4-pro"]},
            "fallback": [{"model": "deepseek-v4-pro", "provider": "deepseek"}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers),
                          _vision_features(), when=OFF_PEAK)
        assert _models(plan) == ["gpt-5.6-terra"]
        assert plan["promoted"] == []
        assert [h["model"] for h in plan["rejected"]] == ["deepseek-v4-pro"]

    def test_capability_filter_runs_before_the_cap(self):
        """The cap only ever reports elos the filter kept."""
        tiers = {"T1": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [
                {"model": "deepseek-v4-pro", "provider": "deepseek"},
                {"model": "glm-4.6v", "provider": "zai"},
            ],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers),
                          _vision_features(), when=PEAK_MONDAY)
        assert [h["model"] for h in plan["rejected"]] == ["deepseek-v4-pro"]
        assert [c["model"] for c in plan["capped"]] == ["glm-4.6v"]
        assert _models(plan) == ["gpt-5.6-terra"]

    def test_ordering_runs_last_over_the_surviving_set(self):
        """cheapest_now orders what the cap and the policy left behind."""
        tiers = {"T1": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "fallback_strategy": "cheapest_now", "pin_primary": False,
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [
                {"model": "deepseek-v4-pro", "provider": "deepseek"},
                {"model": "gpt-5.6-luna", "provider": "openai-codex"},
                {"model": "mimo-v2.5", "provider": "xiaomi"},
            ],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          when=PEAK_MONDAY)
        # deepseek is capped; the rest sort by effective output price.
        assert _models(plan) == ["mimo-v2.5", "gpt-5.6-luna", "gpt-5.6-terra"]
        assert plan["strategy"] == "cheapest_now"


class TestCheapestNowThroughPlanChain(_NeedsRegistry):
    def test_order_is_relative_to_the_hour(self):
        """deepseek-v4-flash undercuts luna off-peak and loses to it at 2x."""
        resolved = resolve_tiers({"model": "T1"}, CHEAPEST_TIER)
        assert _models(plan_chain(resolved, _mkf(), when=OFF_PEAK)) == [
            "deepseek-v4-flash", "gpt-5.6-luna", "gpt-5.6-terra",
        ]
        assert _models(plan_chain(resolved, _mkf(), when=PEAK_MONDAY)) == [
            "gpt-5.6-luna", "deepseek-v4-flash", "gpt-5.6-terra",
        ]

    def test_an_unpriced_plan_model_is_never_treated_as_free(self):
        """glm-5.3 sorts where a plan model belongs, not where 0.0 would."""
        tiers = {"T1": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "fallback_strategy": "cheapest_now", "pin_primary": False,
            "fallback": [
                {"model": "glm-5.3", "provider": "zai", "billing_mode": "plan"},
                {"model": "mimo-v2.5", "provider": "xiaomi",
                 "billing_mode": "metered"},
            ],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          when=OFF_PEAK)
        assert _models(plan) == ["glm-5.3", "mimo-v2.5", "gpt-5.6-terra"]
        assert rules_mod._caps.effective_price("glm-5.3", OFF_PEAK) is None

    def test_pin_primary_applies_cheapest_now_to_the_tail_only(self):
        tiers = {"T1": dict(CHEAPEST_TIER["T1"], pin_primary=True)}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          when=OFF_PEAK)
        assert _models(plan) == [
            "gpt-5.6-terra", "deepseek-v4-flash", "gpt-5.6-luna",
        ]
        assert plan["pin_primary"] is True


class TestMultipliersReported(_NeedsRegistry):
    def test_every_chain_model_is_priced_at_the_injected_hour(self):
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert plan["multipliers"] == {
            "gpt-5.6-terra": 1.0, "mimo-v2.5": 1.0, "deepseek-v4-pro": 2.0,
        }

    def test_no_clock_reports_no_multipliers(self):
        """1.0 for everything would be a price claim nobody checked."""
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf())
        assert plan["multipliers"] == {}

    def test_a_declared_window_overrides_the_registry(self):
        """price_windows is commercial metadata, overridable per elo in YAML."""
        tiers = {"T1": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "price_windows": [{"hours_utc": [6, 10], "multiplier": 3.0}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          when=PEAK_MONDAY)
        assert plan["multipliers"] == {"gpt-5.6-terra": 3.0}


class TestClockTolerance(_NeedsRegistry):
    def test_a_naive_datetime_is_read_as_utc(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=datetime(2026, 8, 17, 7, 0))
        assert (plan["utc_hour"], plan["utc_weekday"]) == (7, 0)
        assert _models(plan) == ["gpt-5.6-terra"]

    def test_an_aware_datetime_is_converted(self):
        """The operator's UTC-03 03:00 is the 06:00 UTC peak."""
        local = datetime(2026, 8, 17, 3, 0, tzinfo=timezone(timedelta(hours=-3)))
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=local)
        assert plan["utc_hour"] == 6
        assert [c["model"] for c in plan["capped"]] == [
            "deepseek-v4-pro", "glm-5.3",
        ]

    def test_junk_is_treated_as_no_clock_not_an_exception(self):
        for junk in ("2026-08-17T07:00:00Z", 7, object()):
            plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                              when=junk)
            assert plan["time_agnostic"] is True
            assert plan["capped"] == []
            assert _models(plan) == [
                "gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3",
            ]

    def test_the_reported_hour_matches_the_registry_reading(self):
        """One clock reading: the trace cannot explain another hour's order."""
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert plan["utc_hour"] == PEAK_MONDAY.hour
        assert plan["multipliers"]["deepseek-v4-pro"] == (
            rules_mod._caps.price_multiplier("deepseek-v4-pro", PEAK_MONDAY)
        )


class TestTimeStagesDegradeAlone(_NeedsRegistry):
    def test_a_broken_cap_stage_costs_only_the_cap(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise ValueError("stale registry")

        monkeypatch.setattr(rules_mod._caps, "apply_time_cap", boom)
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == []
        assert plan["multipliers"]["deepseek-v4-pro"] == 2.0

    def test_a_policy_stage_that_loses_an_elo_is_discarded(self, monkeypatch):
        monkeypatch.setattr(
            rules_mod._caps, "apply_time_policy",
            lambda chain, policy, when=None: {
                "chain": [], "demoted": ["deepseek-v4-pro"], "promoted": [],
            },
        )
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "mimo-v2.5"]
        assert plan["demoted"] == []

    def test_a_cap_stage_that_empties_the_chain_is_overridden(self, monkeypatch):
        monkeypatch.setattr(
            rules_mod._caps, "apply_time_cap",
            lambda chain, cap, when=None: {
                "chain": [], "capped": [], "bypassed": False,
            },
        )
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]


# ---------------------------------------------------------------------------
# lint(): time knobs
# ---------------------------------------------------------------------------

class TestLintTimeKnobs(_NeedsRegistry):
    def test_valid_time_knobs_lint_clean(self):
        assert lint(_cfg({"T2": {
            "model": "glm-5.3", "provider": "zai",
            "fallback_strategy": "cheapest_now",
            "time_cap": {"max_multiplier": 1.5},
            "time_policy": {"avoid_peak": ["zai"], "prefer": ["mimo-v2.5"]},
        }})) == []

    def test_cap_below_one_is_an_error(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_cap": {"max_multiplier": 0.5}}}))
        assert "tier 'T2': 'time_cap.max_multiplier' must be a number >= 1.0" in errors

    def test_non_numeric_cap_is_an_error(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_cap": {"max_multiplier": "1.5"}}}))
        assert "tier 'T2': 'time_cap.max_multiplier' must be a number >= 1.0" in errors

    def test_boolean_cap_is_an_error(self):
        """True is an int in Python: a cap of 1.0 the operator never wrote."""
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_cap": {"max_multiplier": True}}}))
        assert "tier 'T2': 'time_cap.max_multiplier' must be a number >= 1.0" in errors

    def test_cap_of_exactly_one_is_accepted(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_cap": {"max_multiplier": 1.0}}}))
        assert not any("time_cap" in e for e in errors)

    def test_non_mapping_time_cap_is_an_error(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_cap": 1.5}}))
        assert "tier 'T2': 'time_cap' must be a mapping" in errors

    def test_avoid_peak_must_be_a_list(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_policy": {"avoid_peak": "deepseek"}}}))
        assert (
            "tier 'T2': 'time_policy.avoid_peak' must be a list of provider names"
        ) in errors

    def test_prefer_must_be_a_list_of_names(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_policy": {"prefer": [7]}}}))
        assert (
            "tier 'T2': 'time_policy.prefer' must be a list of model names"
        ) in errors

    def test_non_mapping_time_policy_is_an_error(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_policy": ["deepseek"]}}))
        assert "tier 'T2': 'time_policy' must be a mapping" in errors

    def test_good_time_policy_does_not_fire(self):
        errors = lint(_cfg({"T2": {
            "model": "m2", "provider": "p2",
            "time_policy": {"avoid_peak": ["deepseek", "zai"]},
        }}))
        assert not any("time_policy" in e for e in errors)

    def test_overlapping_declared_windows_are_an_error(self):
        errors = lint(_cfg({"T2": {
            "model": "glm-5.3", "provider": "zai",
            "price_windows": [
                {"hours_utc": [6, 10], "multiplier": 2.0},
                {"hours_utc": [8, 12], "multiplier": 1.5},
            ],
        }}))
        assert "model 'glm-5.3': price_windows entries overlap" in errors

    def test_non_overlapping_declared_windows_lint_clean(self):
        assert lint(_cfg({"T2": {
            "model": "glm-5.3", "provider": "zai",
            "price_windows": [
                {"hours_utc": [1, 4], "multiplier": 2.0},
                {"hours_utc": [6, 10], "multiplier": 2.0},
            ],
        }})) == []

    def test_a_window_crossing_midnight_must_be_two_entries(self):
        errors = lint(_cfg({"T2": {
            "model": "glm-5.3", "provider": "zai",
            "price_windows": [{"hours_utc": [22, 2], "multiplier": 0.8}],
        }}))
        assert any("hours_utc" in e for e in errors)

    def test_a_fallback_hop_window_is_checked_too(self):
        errors = lint(_cfg({"T2": {
            "model": "m2", "provider": "p2",
            "fallback": [{
                "model": "deepseek-v4-pro", "provider": "deepseek",
                "price_windows": [
                    {"hours_utc": [1, 4], "multiplier": 2.0},
                    {"hours_utc": [3, 6], "multiplier": 2.0},
                ],
            }],
        }}))
        assert "model 'deepseek-v4-pro': price_windows entries overlap" in errors


# ---------------------------------------------------------------------------
# lint(): the time knobs' KEYS are a closed set too
#
# A closed strategy SET exists so a typo is refused at the write gate rather
# than degrading at runtime. The same argument applies with more force to these
# two knobs' KEYS: `avoid_peek` and `max_multipler` are one character from a
# working cost control, read in the file as if they were one, and do nothing.
# ---------------------------------------------------------------------------

class TestLintTimeKnobKeys:
    def test_a_typo_in_a_time_policy_key_is_a_hard_error(self):
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_policy": {"avoid_peek": ["deepseek"]}}}))
        assert (
            "tier 'T2': 'time_policy.avoid_peek' not in closed time_policy key set"
        ) in errors

    def test_a_typo_in_a_time_cap_key_is_a_hard_error(self):
        errors = lint(_cfg({"T1": {"model": "m1", "provider": "p1",
                                   "time_cap": {"max_multipler": 1.5}}}))
        assert (
            "tier 'T1': 'time_cap.max_multipler' not in closed time_cap key set"
        ) in errors

    def test_the_error_names_the_offending_key(self):
        """An operator has to be able to find the typo in their file."""
        for key in ("avoid_peaks", "preferred", "prefer_when"):
            errors = lint(_cfg({"T3": {"model": "m3", "provider": "p3",
                                       "time_policy": {key: ["zai"]}}}))
            assert any(f"'time_policy.{key}'" in e for e in errors)

    def test_the_known_keys_do_not_fire(self):
        errors = lint(_cfg({"T2": {
            "model": "m2", "provider": "p2",
            "time_cap": {"max_multiplier": 1.5},
            "time_policy": {"avoid_peak": ["zai"], "prefer": ["mimo-v2.5"]},
        }}))
        assert not any("closed time" in e for e in errors)

    def test_the_closed_sets_are_exactly_the_shipped_knobs(self):
        assert rules_mod._TIME_CAP_KEYS == frozenset({"max_multiplier"})
        assert rules_mod._TIME_POLICY_KEYS == frozenset({"avoid_peak", "prefer"})

    def test_the_defect_this_closes(self):
        """The typo passed the fail-closed gate and then did nothing at all.

        Same treatment `fallback_strategy: 'cheapest-now'` already got — that one
        is refused because FALLBACK_STRATEGIES is a closed set.
        """
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("the time layer lives in the capability registry")
        typo = {"T1": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "time_policy": {"avoid_peek": ["deepseek"]},
            "time_cap": {"max_multipler": 1.5},
            "fallback": [{"model": "deepseek-v4-pro", "provider": "deepseek"}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, typo), _mkf(),
                          when=PEAK_MONDAY)
        # The knobs are inert — which is exactly why lint has to refuse them.
        assert plan["demoted"] == [] and plan["capped"] == []
        assert "time_cap" not in plan
        assert len([e for e in lint(_cfg(typo)) if "closed time" in e]) == 2
        # A hyphen in a strategy is already refused; these now are too.
        assert any("fallback_strategy" in e for e in lint(_cfg(
            {"T2": {"model": "m2", "provider": "p2",
                    "fallback_strategy": "cheapest-now"}}
        )))

    def test_an_unknown_key_does_not_suppress_the_value_checks(self):
        """Both findings surface: the typo AND the bad value beside it."""
        errors = lint(_cfg({"T2": {
            "model": "m2", "provider": "p2",
            "time_cap": {"max_multipler": 1.5, "max_multiplier": 0.5},
        }}))
        assert any("'time_cap.max_multipler'" in e for e in errors)
        assert "tier 'T2': 'time_cap.max_multiplier' must be a number >= 1.0" in errors

    def test_a_non_string_key_is_reported_not_raised(self):
        """YAML can produce `7: x`; lint is the write gate, so it reports it."""
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "time_policy": {7: ["zai"]}}}))
        assert any("time_policy.7" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_warnings(): time advisories
# ---------------------------------------------------------------------------

class TestTimeWarnings(_NeedsRegistry):
    def test_a_cap_every_elo_can_exceed_warns(self):
        config = _cfg({"T3": {
            "model": "deepseek-v4-pro", "provider": "deepseek",
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [{"model": "glm-5.3", "provider": "zai"}],
        }})
        assert (
            "tier 'T3': every elo is in an expensive window at some hour "
            "— time_cap will bypass"
        ) in lint_warnings(config)

    def test_a_flat_priced_rail_silences_it(self):
        config = _cfg({"T3": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [{"model": "glm-5.3", "provider": "zai"}],
        }})
        assert not any("time_cap will bypass" in w for w in lint_warnings(config))

    def test_no_cap_means_no_bypass_warning(self):
        config = _cfg({"T3": {
            "model": "deepseek-v4-pro", "provider": "deepseek",
            "fallback": [{"model": "glm-5.3", "provider": "zai"}],
        }})
        assert not any("time_cap will bypass" in w for w in lint_warnings(config))

    def test_a_cap_above_every_window_means_no_warning(self):
        config = _cfg({"T3": {
            "model": "deepseek-v4-pro", "provider": "deepseek",
            "time_cap": {"max_multiplier": 2.0},
            "fallback": [{"model": "glm-5.3", "provider": "zai"}],
        }})
        assert not any("time_cap will bypass" in w for w in lint_warnings(config))

    def test_avoid_peak_naming_an_absent_provider_warns(self):
        config = _cfg({"T3": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "time_policy": {"avoid_peak": ["deepseek"]},
            "fallback": [{"model": "glm-5.3", "provider": "zai"}],
        }})
        assert (
            "tier 'T3': 'time_policy.avoid_peak' names provider 'deepseek', "
            "absent from this tier"
        ) in lint_warnings(config)

    def test_avoid_peak_naming_a_present_provider_is_quiet(self):
        config = _cfg({"T3": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "time_policy": {"avoid_peak": ["ZAI"]},
            "fallback": [{"model": "glm-5.3", "provider": "zai"}],
        }})
        assert not any("absent from this tier" in w for w in lint_warnings(config))

    def test_cheapest_now_with_no_priced_elo_warns(self):
        config = _cfg({"T2": {
            "model": "glm-5.3", "provider": "zai",
            "fallback_strategy": "cheapest_now",
        }})
        assert (
            "tier 'T2': 'cheapest_now' with no priced elo degrades to "
            "billing_mode rank only"
        ) in lint_warnings(config)

    def test_cheapest_now_with_one_priced_elo_is_quiet(self):
        config = _cfg({"T2": {
            "model": "glm-5.3", "provider": "zai",
            "fallback_strategy": "cheapest_now",
            "fallback": [{"model": "gpt-5.6-luna", "provider": "openai-codex"}],
        }})
        assert not any("billing_mode rank only" in w for w in lint_warnings(config))

    def test_time_warnings_never_block_a_write(self):
        config = _cfg({"T3": {
            "model": "deepseek-v4-pro", "provider": "deepseek",
            "time_cap": {"max_multiplier": 1.5},
            "time_policy": {"avoid_peak": ["xiaomi"]},
            "fallback": [{"model": "glm-5.3", "provider": "zai"}],
        }})
        assert lint(config) == []
        assert len(lint_warnings(config)) >= 2


# ---------------------------------------------------------------------------
# explain() threads the clock; dead constants are gone
# ---------------------------------------------------------------------------

class TestExplainClock(_NeedsRegistry):
    def test_explain_passes_the_clock_through(self):
        rules = [{"id": "any-code", "when": {"has_code": {"eq": True}},
                  "then": {"model": "T1"}}]
        traced = explain("x", _mkf(has_code=True), False, rules,
                         {"action": "classify"}, TIME_TIERS, when=PEAK_MONDAY)
        plan = traced["chain_plan"]
        assert plan["utc_hour"] == 7
        assert [h["model"] for h in plan["chain"]] == ["gpt-5.6-terra"]

    def test_explain_without_a_clock_is_time_agnostic(self):
        rules = [{"id": "any-code", "when": {"has_code": {"eq": True}},
                  "then": {"model": "T1"}}]
        traced = explain("x", _mkf(has_code=True), False, rules,
                         {"action": "classify"}, TIME_TIERS)
        assert traced["chain_plan"]["time_agnostic"] is True
        assert traced["chain_plan"]["capped"] == []


class TestRulesModuleHygiene:
    def test_dead_operator_constants_are_gone(self):
        assert not hasattr(rules_mod, "_UNARY_OPS")
        assert not hasattr(rules_mod, "_ALLOWED_OPS")

    def test_the_module_cannot_read_a_clock(self):
        """Purity, structurally: no datetime import means no wall-clock read."""
        assert not hasattr(rules_mod, "datetime")
        assert not hasattr(rules_mod, "time")

    def test_time_knobs_are_routing_not_capability_declarations(self):
        assert {"time_cap", "time_policy"} <= rules_mod._NON_CAPABILITY_KEYS
        resolved = resolve_tiers({"model": "T1"}, TIME_TIERS)
        assert "time_cap" not in resolved.get("declared_capabilities", {})
        assert "time_policy" not in resolved.get("declared_capabilities", {})

    def test_resolve_copies_the_time_knobs(self):
        tiers = {"T1": {"model": "m", "provider": "p",
                        "time_cap": {"max_multiplier": 1.5},
                        "time_policy": {"avoid_peak": ["zai"]}}}
        resolved = resolve_tiers({"model": "T1"}, tiers)
        resolved["time_cap"]["max_multiplier"] = 9.0
        resolved["time_policy"]["avoid_peak"].append("deepseek")
        assert tiers["T1"]["time_cap"] == {"max_multiplier": 1.5}
        assert tiers["T1"]["time_policy"] == {"avoid_peak": ["zai"]}


# ---------------------------------------------------------------------------
# blocked_model is an injected boolean, and the clause means what it says
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "condition,blocked,expected",
    [
        ({"eq": True}, True, True),    ({"eq": True}, False, False),
        ({"eq": False}, True, False),  ({"eq": False}, False, True),
        ({"ne": True}, True, False),   ({"ne": True}, False, True),
        ({"ne": False}, True, True),   ({"ne": False}, False, False),
        ({"nin": [True]}, True, False),({"nin": [True]}, False, True),
    ],
)
def test_blocked_model_honours_the_authors_operator(condition, blocked, expected):
    """A blocked_model clause must mean what it says.

    The matcher used to re-read the value under condition["eq"], discarding the
    written operator and defaulting the target to True. `{ne: true}` - the natural
    "only when NOT blocked" guard - therefore evaluated as `eq true`: False exactly
    when the model was healthy, making the rule dead on the live path. lint()
    accepted it and _matching_clauses reported a chip for it, so /explain claimed a
    clause matched that the engine had rejected.
    """
    from router.rules import _all_clauses_match

    feats = {"verb_class": "trivial", "has_code": True, "size_lines": 10}
    assert _all_clauses_match({"blocked_model": condition}, feats, blocked) is expected


@pytest.mark.parametrize(
    "condition", [{"eq": True}, {"eq": False}, {"ne": True}, {"ne": False}, {"nin": [True]}]
)
@pytest.mark.parametrize("blocked", [True, False])
def test_the_matcher_and_the_chips_never_disagree(condition, blocked):
    """The explanation surface must not claim a clause the engine rejected."""
    from router.rules import _all_clauses_match, _matching_clauses

    feats = {"verb_class": "trivial", "has_code": True, "size_lines": 10}
    when = {"blocked_model": condition}
    matched = _all_clauses_match(when, feats, blocked)
    chips = bool(_matching_clauses(when, feats, blocked))
    assert chips == matched, f"{condition} at blocked={blocked}: engine={matched} chips={chips}"
