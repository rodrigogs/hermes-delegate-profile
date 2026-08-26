"""Unit tests for rule matching engine (router/rules.py)."""

import inspect
import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from router import rules as rules_mod
from router.rules import match, lint, lint_findings, lint_warnings, explain, plan_chain, resolve_tiers

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
# 18:00 UTC: no rail is inside any window here. It used to be inside xiaomi's
# 0.8x night discount, which was removed on 2026-08-26 (Token-Plan-only rate on a
# pay-as-you-go install), and the name is kept because the invariant it guards is
# the same: a multiplier that is not > 1.0 must never read as "avoid this now".
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

    def test_disabled_rule_never_fires_and_yields_to_the_next(self):
        """enabled:false takes a row out of first-match without deleting it.

        The console's 'Desativar esta regra' writes exactly this field, so the
        matcher is the contract that makes the button honest: a disabled row
        must neither win nor stand in the way of the row behind it.
        """
        rows = [
            {"id": "broad", "enabled": False,
             "when": {"has_code": {"eq": True}}, "then": {"profile": "coder", "model": "T1"}},
            {"id": "narrow", "when": {"has_code": {"eq": True}},
             "then": {"profile": "reviewer", "model": "T2"}},
        ]
        fv = _mkf(has_code=True)
        output, rule_id = match(fv, False, rows, {"action": "classify"}, ROUTER_CONFIG["tiers"])
        assert rule_id == "narrow", "the disabled row must not fire"
        assert output["model"] == "glm-5.2"
        # explain() is the surface the operator reads: it must not claim the
        # disabled row matched either.
        trace = explain("some task", fv, False, rows, {"action": "classify"}, ROUTER_CONFIG["tiers"])
        assert trace["matched_rule_id"] == "narrow"

    def test_disabled_rule_skipped_even_when_it_would_have_won(self):
        """A disabled row that WOULD have matched must fall through past it."""
        rows = [
            {"id": "only", "enabled": False,
             "when": {"verb_class": {"eq": "hard"}}, "then": {"model": "T4"}},
        ]
        output, rule_id = match(_mkf(verb_class="hard"), False, rows,
                                {"action": "classify"}, ROUTER_CONFIG["tiers"])
        assert rule_id is None
        assert output["action"] == "classify"


# ---------------------------------------------------------------------------
# lint() tests
# ---------------------------------------------------------------------------

class TestLint:
    def test_valid_config(self):
        errors = lint(ROUTER_CONFIG)
        assert errors == []

    def test_non_boolean_rule_enabled_is_rejected(self):
        """enabled is a switch: a truthy string would silently mean 'on'."""
        config = _rules_cfg(
            {"id": "r1", "enabled": "no",
             "when": {"has_code": {"eq": True}}, "then": {"model": "T2"}},
        )
        errors = lint(config)
        assert any("'enabled' must be boolean" in e for e in errors)

    def test_disabled_rule_is_still_schema_validated(self):
        """Disabling is not a lint bypass: a dormant typo becomes live on re-enable.

        The write gate is fail-closed; 'just turn it off' must not be the hatch
        that ships a rule that can never pass lint. The disable button exists to
        resolve the SHADOW class, which it does — shadow findings are the one
        check a disabled rule is exempt from, because it cannot fire.
        """
        config = _rules_cfg(
            {"id": "r1", "enabled": False,
             "when": {"verb_classs": {"eq": "hard"}}, "then": {"model": "T4"}},
        )
        errors = lint(config)
        assert any("'when.verb_classs' is not a known signal" in e for e in errors)

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


class _RegistryWithout:
    """The live registry with one or more names HIDDEN — a stale sibling install.

    This plugin is deployed by copy, so "the registry is a version behind" is a
    real state: the functions that exist behave exactly as they ship (they are
    forwarded, not stubbed), and only the named attributes are missing. A stub
    registry would prove that a stub was called; this one exercises the same
    degrade decision rules.py has to make against a genuinely partial module.
    """

    def __init__(self, module, *hidden):
        self._module = module
        self._hidden = frozenset(hidden)

    def __getattr__(self, name):
        if name in self._hidden:
            raise AttributeError(name)
        return getattr(self._module, name)


class _RegistryWith:
    """The live registry with one or more names REPLACED — a version-SKEWED sibling.

    Same premise as :class:`_RegistryWithout` — the plugin is deployed by copy, so
    "capabilities.py is a version behind rules.py" is a real state — for the skew
    absence cannot express: a function that still EXISTS and no longer takes the
    arguments rules.py passes it, or no longer returns the shape rules.py reads.

    Replacing the name on this PROXY rather than on the module is the point. The
    registry's own internals keep calling their own functions, exactly as a
    genuinely older module would, so a test can say "rules.py lost THIS call" and
    mean only that call.
    """

    def __init__(self, module, **replacements):
        self._module = module
        self._replacements = replacements

    def __getattr__(self, name):
        if name in self._replacements:
            return self._replacements[name]
        return getattr(self._module, name)


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

    def test_only_entries_that_name_an_elo_become_hops(self):
        """A hand-edited `fallback:` contributes hops, not everything in the list.

        An entry with no model names nothing to attempt: keeping it would put a hop
        in the chain that the runner can only fail on, and count it as a rail in
        `independent_rails`. The gate refuses both entries below — asserted, because
        silently dropping them is only acceptable while something still says so.

        The two chain builders are asserted to AGREE. `_tier_chain` is what the
        advisories are computed over and `_build_chain` is what the request is
        routed on, so an entry only one of them counts is either a finding about a
        rail that is not there or a rail nobody checked.
        """
        tier = {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "fallback": [
                {"model": "glm-5.3", "provider": "zai"},
                {"provider": "zai"},        # no elo to attempt
                "glm-4.7",                   # a name where a mapping belongs
            ],
        }
        plan = plan_chain(resolve_tiers({"model": "T1"}, {"T1": tier}), _mkf())
        assert [hop["model"] for hop in plan["chain"]] == [
            "gpt-5.6-terra", "glm-5.3",
        ]
        assert [hop["model"] for hop in rules_mod._tier_chain(tier)] == [
            hop["model"] for hop in plan["chain"]
        ]
        assert len([
            e for e in lint(_cfg({"T1": tier})) if "must be a mapping with" in e
        ]) == 2
        # Two hops, two upstreams: the entry that named no elo is not a rail.
        assert plan["independent_rails"] == 2

    def test_returns_the_full_contract_keys(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, CAPS_TIERS), _mkf())
        assert set(plan) == {
            "chain", "requirements", "rejected", "unknown", "bypassed",
            "unsatisfiable", "strategy", "strategy_declared", "strategy_degraded",
            "strategy_degraded_reason", "pin_primary", "independent_rails",
            "time_agnostic", "time_cap_bypassed", "capped", "demoted",
            # POSITION vs PRICE: `demoted` names only what actually moved,
            # `peak_priced` names everything avoid_peak matched in a dearer
            # window. Both are in the contract because one field cannot carry
            # both readings without lying about one of them.
            "promoted", "peak_priced", "multipliers",
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

    def test_a_registry_missing_a_late_stage_still_yields_a_usable_plan(
        self, monkeypatch
    ):
        """A partial registry costs the STAGES, never the route.

        ``independent_rails`` is the last thing plan_chain asks the registry for,
        so hiding it degrades a run in which the filter, the cap and the policy all
        already succeeded — the worst case for the defensive ``except``, because
        the wholesale degrade throws their results away. What must survive is a
        usable plan: every declared hop still attemptable, in declared order.
        """
        monkeypatch.setattr(
            rules_mod, "_caps", _RegistryWithout(rules_mod._caps, "independent_rails")
        )
        resolved = resolve_tiers({"model": "T1"}, TIME_TIERS)
        plan = plan_chain(resolved, _mkf(), when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        # Honest flags, not merely present ones: no stage ran, so nothing may be
        # reported as filtered, capped, demoted or repriced.
        assert plan["capped"] == [] and plan["demoted"] == []
        assert plan["multipliers"] == {} and plan["rejected"] == []
        assert plan["time_cap_bypassed"] is False and plan["bypassed"] is False
        # ... and the hour it was planned at is still the hour it was planned at.
        assert (plan["utc_hour"], plan["time_agnostic"]) == (7, False)
        assert plan["time_cap"] == {"max_multiplier": 1.5}

    def test_a_filter_returning_the_wrong_type_degrades_rather_than_raising(
        self, monkeypatch
    ):
        """`.get` on a list is an AttributeError, and this is the request path."""
        monkeypatch.setattr(
            rules_mod._caps, "filter_chain", lambda chain, requirements: list(chain),
        )
        plan = plan_chain(resolve_tiers({"model": "T4"}, CAPS_TIERS),
                          _vision_features())
        assert [h["model"] for h in plan["chain"]] == [
            "text-only-elo", "vision-elo", "another-text-elo",
        ]
        assert plan["requirements"] == {} and plan["rejected"] == []

    def test_the_degraded_plan_has_the_same_shape_as_a_healthy_one(
        self, monkeypatch
    ):
        """One shape, whatever happened: a consumer must not test for a key.

        The console, the CLI and routes.jsonl all read the same dict. A degraded
        plan that dropped a key would make every reader of that key branch on
        whether the registry happened to be healthy — which is how a diagnostic
        ends up rendered as `undefined` on the one run an operator is debugging.
        """
        resolved = resolve_tiers({"model": "T1"}, TIME_TIERS)
        healthy = set(plan_chain(resolved, _mkf(), when=PEAK_MONDAY))
        monkeypatch.setattr(
            rules_mod, "_caps", _RegistryWithout(rules_mod._caps, "independent_rails")
        )
        assert set(plan_chain(resolved, _mkf(), when=PEAK_MONDAY)) == healthy
        monkeypatch.setattr(rules_mod, "_caps", None)
        assert set(plan_chain(resolved, _mkf(), when=PEAK_MONDAY)) == healthy


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
            "promoted": [], "peak_priced": [], "multipliers": {},
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

    def test_bad_hop_billing_mode(self):
        """A FALLBACK HOP's billing_mode is checked, because it is not inert.

        The hop loop used to check only that `model` and `provider` were present.
        resolve_tiers hands every other key on a hop to
        capabilities._declared_overrides, where a declared mode OVERRIDES the
        registry's correct one, so `meterd` sends that elo to _billing_rank's
        unknown bucket and `cheapest_now` sorts it LAST. Priced demonstration:
        TestHopBillingModeIsACheapestNowKey.
        """
        errors = lint(_cfg({"T2": {
            "model": "m2", "provider": "p2",
            "fallback_strategy": "cheapest_now", "pin_primary": False,
            "fallback": [{"model": "f1", "provider": "q1",
                          "billing_mode": "meterd"}],
        }}))
        expected = (
            f"tier 'T2': fallback[0]: 'billing_mode' must be one of "
            f"{sorted(rules_mod._billing_modes())}"
        )
        assert expected in errors

    def test_good_hop_billing_mode_does_not_fire(self):
        for mode in sorted(rules_mod._billing_modes()):
            errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2", "fallback": [
                {"model": "f1", "provider": "q1", "billing_mode": mode},
            ]}}))
            assert not any("billing_mode" in e for e in errors)

    @pytest.mark.parametrize(
        "mode",
        ["metered", "plan", "meterd", "subscribtion", "", "gift-card",
         ["metered"], {"metered": True}, 7, None, True],
    )
    def test_a_hop_is_held_to_the_tiers_billing_standard(self, mode):
        """Symmetry, asserted AS symmetry — the same value, the same verdict.

        Not "a hop rejects meterd": that passes again the moment the two checks
        drift a second time, which is how this gap opened. The claim under test is
        that neither elo can declare a mode the other could not, whatever the
        closed set becomes and whichever shape YAML produces — and it holds in
        both directions, so a good mode must stay clean in both places too.
        """
        on_tier = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                    "billing_mode": mode}}))
        on_hop = lint(_cfg({"T2": {"model": "m2", "provider": "p2", "fallback": [
            {"model": "f1", "provider": "q1", "billing_mode": mode},
        ]}}))
        assert (
            any("billing_mode" in e for e in on_tier)
            == any("billing_mode" in e for e in on_hop)
        ), f"billing_mode {mode!r}: tier={on_tier} hop={on_hop}"

    def test_a_hops_shape_and_its_knob_are_reported_together(self):
        """One diagnostic must not hide another: the gate reports both defects."""
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2", "fallback": [
            {"model": "f1", "billing_mode": "meterd"},
        ]}}))
        assert (
            "tier 'T2': fallback[0] must be a mapping with 'model' and 'provider'"
            in errors
        )
        assert any("fallback[0]: 'billing_mode'" in e for e in errors)

    def test_a_non_mapping_hop_is_reported_not_raised(self):
        """`'billing_mode' in 7` raises TypeError, and lint() is the write gate."""
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2",
                                   "fallback": ["nope", 7, None]}}))
        assert len([e for e in errors if "must be a mapping with" in e]) == 3
        assert not any("billing_mode" in e for e in errors)

    @pytest.mark.parametrize("key", ["model", "provider"])
    @pytest.mark.parametrize(
        "value",
        ["glm-5.3", "", "   ", 4.7, 7, True, None, ["glm-5.3"], {"glm-5.3": True}],
    )
    def test_a_hops_identity_is_held_to_the_tiers_standard(self, key, value):
        """Symmetry, asserted AS symmetry: same value, same verdict, either elo.

        A hop's model/provider were checked for TRUTHINESS only while a tier's own
        must be a non-empty string, so ``model: 4.7`` — YAML's reading of an
        unquoted glm-4.7 — was refused on a tier and waved through on a hop. A hop
        is not the lesser declaration: the request is routed on it. What happens to
        it downstream is asserted in
        test_an_unusable_hop_identity_is_invisible_after_the_gate.

        Stated as an equality rather than "a hop rejects 4.7" for the reason the
        billing_mode symmetry above is: the verdict-by-verdict claim passes again
        the moment the two checks drift a second time.
        """
        tier = {"model": "m2", "provider": "p2", key: value}
        on_tier = [e for e in lint(_cfg({"T2": tier})) if "T2" in e]
        on_hop = [
            e for e in lint(_cfg({"T2": {
                "model": "m2", "provider": "p2",
                "fallback": [{"model": "f1", "provider": "q1", key: value}],
            }})) if "T2" in e
        ]
        assert bool(on_tier) == bool(on_hop), (
            f"{key}={value!r}: tier={on_tier} hop={on_hop}"
        )

    def test_an_unusable_hop_identity_names_the_key_and_only_it(self):
        """One defect, one diagnostic — and it says which key and what is wrong.

        The shape message ("must be a mapping with 'model' and 'provider'") is
        about a hop that does not DECLARE them; this hop declares both, and one is
        not a name. Emitting both would send the operator looking for a missing key
        they can see in the file.
        """
        errors = lint(_cfg({"T2": {"model": "m2", "provider": "p2", "fallback": [
            {"model": 4.7, "provider": "zai"},
        ]}}))
        assert "tier 'T2': fallback[0]: 'model' must be a non-empty string" in errors
        assert not any("must be a mapping with" in e for e in errors)

    def test_an_unusable_hop_identity_is_invisible_after_the_gate(self):
        """Why it has to be caught HERE: nothing downstream can say it.

        The plan keeps the hop — the capability filter reads its id as "" and lets
        it through on the fail-open unknown path, which is right, because a filter
        that removed it could empty a chain. But `unknown` is the flag that makes
        that fail-open loud, and it collects model IDS: it names the string model it
        has never heard of and cannot name this one at all. So the router would
        attempt a rail called 4.7 with every plan field reading clean.
        """
        tier = {"model": "m2", "provider": "p2",
                "fallback": [{"model": 4.7, "provider": "zai"}]}
        plan = plan_chain(resolve_tiers({"model": "T2"}, {"T2": tier}),
                          _vision_features())
        assert [hop["model"] for hop in plan["chain"]] == ["m2", 4.7]
        assert plan["unknown"] == ["m2"]
        assert plan["rejected"] == [] and plan["bypassed"] is False

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

    def test_a_tier_that_is_not_a_mapping_is_reported_once(self):
        """`T2: glm-5.3` — the mapping an operator forgot to nest under the alias.

        Every later check indexes the tier (`tier['model']`, `'pin_primary' in
        tier`), and `"glm-5.3"['model']` is a TypeError, so the write gate would
        raise through the operator's apply without the early `continue`. Exactly
        one diagnostic, because the shape IS the defect: four more errors derived
        from it (missing model, missing provider, ...) would bury the one that
        tells them what to type.
        """
        errors = lint(_cfg({"T2": "glm-5.3"}))
        assert [e for e in errors if "T2" in e] == ["tier 'T2' must be a mapping"]

    def test_requirements_that_are_not_a_mapping_are_reported(self):
        """`requirements: [min_context]` — a list where the floor belongs.

        Reported rather than iterated: `["min_context"].items()` raises, and lint()
        is the write gate. The second assertion is why it must be a HARD error and
        not silence — the planner discards a floor of this shape without a word
        (_tier_floor_of reads None), so the operator would keep a floor they
        believe they set and never had, and the failure direction of a discarded
        floor is routing to a model that cannot serve the request.
        """
        errors = lint(_cfg({"T4": {"model": "m4", "provider": "p4",
                                   "requirements": ["min_context"]}}))
        assert "tier 'T4': 'requirements' must be a mapping" in errors
        assert rules_mod._tier_floor_of({"requirements": ["min_context"]}) is None


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

    def test_a_model_named_by_three_hops_is_reported_once(self):
        """One finding per model, not one per hop.

        A tier that falls back to the same elo behind two providers names it three
        times; three copies of the same sentence is how an operator learns to skim
        the advisory channel, and the next finding in it is the one they miss.
        """
        if rules_mod._caps is None:
            pytest.skip("registry required to know a model is unknown")
        config = _cfg({"T2": {
            "model": "totally-made-up-elo-xyz", "provider": "p2",
            "fallback": [{"model": "totally-made-up-elo-xyz", "provider": "q2"},
                         {"model": "totally-made-up-elo-xyz", "provider": "r2"}],
        }})
        assert [w for w in lint_warnings(config) if "T2" in w] == [
            "tier 'T2': model 'totally-made-up-elo-xyz' is unknown to the "
            "capability registry and declares no capabilities"
        ]

    def test_a_hop_whose_model_is_not_a_string_earns_no_finding(self):
        """`model: 7` is not an unknown model — it is not a model at all.

        The registry keys on strings, so a numeric id cannot be looked up and
        cannot be reported as unverifiable either. Silence here is the conservative
        direction for an ADVISORY: the finding it could invent ("unknown to the
        registry") would point the operator at the registry, when the defect is in
        their file. NOTE: lint() does not refuse it either — a hop's model is
        checked only for truthiness, while a tier's own must be a non-empty string.
        """
        if rules_mod._caps is None:
            pytest.skip("registry required to know a model is unknown")
        config = _cfg({"T2": {"model": "glm-5.3", "provider": "zai",
                              "fallback": [{"model": 7, "provider": "deepseek"}]}})
        assert not any("model '7'" in w for w in lint_warnings(config))

    def test_a_registry_that_raises_on_capabilities_for_is_silent(self, monkeypatch):
        """An unverifiable model is unverifiable — not reported as unknown.

        `capabilities_for` raising is a broken registry, and a warning blaming the
        operator's model list for it would send them editing the wrong file. The
        rest of the report still arrives, which is the point of degrading per hop.
        """
        if rules_mod._caps is None:
            pytest.skip("registry required to know a model is unknown")
        def boom(model, declared=None):
            raise TypeError("stale registry")

        monkeypatch.setattr(rules_mod._caps, "capabilities_for", boom)
        config = _cfg({"T2": {
            "model": "totally-made-up-elo-xyz", "provider": "zai",
            "fallback": [{"model": "f1", "provider": "zai"}],
        }})
        warnings = lint_warnings(config)
        assert not any("unknown to the capability registry" in w for w in warnings)
        assert "tier 'T2': first two hops share upstream 'zai' — no independent fallback" in warnings


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
# The closed sets when the registry cannot supply them
#
# lint() validates four vocabularies it does not own: the strategy set, the
# billing modes, the requirement keys and the `when` field names. Each is READ
# from the sibling module that owns it so the two cannot drift — and each has a
# local fallback for the state where the sibling is absent or is exporting
# something unusable. This plugin is deployed by COPY, so a router next to a
# registry a version behind (or missing) is a real deployment, not a hypothetical.
#
# What these cases assert is that the fallback keeps lint FAIL-CLOSED. A gate
# that silently stops refusing typos is worse than one that refuses too much: the
# typo reads in the file as a working cost control and does nothing at all.
# ---------------------------------------------------------------------------

class TestClosedSetsWithoutTheRegistry:
    def test_the_local_mirrors_match_the_registry_they_stand_in_for(self):
        """A mirror drifts; these two must not, so the equality is asserted."""
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("the closed sets live in the capability registry")
        assert rules_mod._FALLBACK_STRATEGIES == rules_mod._caps.FALLBACK_STRATEGIES
        assert rules_mod._FALLBACK_BILLING_MODES == rules_mod._caps.BILLING_MODES
        assert rules_mod._FALLBACK_REQUIREMENT_KEYS == rules_mod._caps.REQUIREMENT_KEYS

    @pytest.mark.parametrize(
        "tier,fragment",
        [
            ({"fallback_strategy": "round_robin"}, "'fallback_strategy' must be one of"),
            ({"billing_mode": "gift-card"}, "'billing_mode' must be one of"),
            ({"requirements": {"gpu": True}}, "'requirements.gpu' not in closed"),
        ],
    )
    def test_the_gate_reaches_the_same_verdict_without_the_registry(
        self, tier, fragment, monkeypatch
    ):
        """Same config, same verdict, registry or no registry.

        Asserted as an agreement between the two states rather than against the
        error strings alone: what matters is that an operator cannot get a bad
        value past the gate by deploying next to a registry that failed to import.
        """
        config = _cfg({"T2": dict({"model": "m2", "provider": "p2"}, **tier)})
        with_registry = [e for e in lint(config) if fragment in e]
        monkeypatch.setattr(rules_mod, "_caps", None)
        assert [e for e in lint(config) if fragment in e] == with_registry
        assert len(with_registry) == 1

    @pytest.mark.parametrize(
        "attr,tier,accepted",
        [
            ("FALLBACK_STRATEGIES", {"fallback_strategy": "cheapest"}, "cheapest_now"),
            ("BILLING_MODES", {"billing_mode": "meter"}, "metered"),
            ("REQUIREMENT_KEYS", {"requirements": {"min": 5}}, "min_context"),
        ],
    )
    def test_a_registry_exporting_a_string_cannot_open_the_gate(
        self, attr, tier, accepted, monkeypatch
    ):
        """The type check is the load-bearing half of "read it from the registry".

        A closed set is consulted with ``in``, and ``in`` on a STRING is a
        substring test: were the export trusted whatever its type, a registry
        exporting ``"cheapest_now"`` instead of a one-element set would make
        ``fallback_strategy: cheapest`` — and ``chea``, and ``now`` — lint clean and
        then degrade to sequential at run time. Hence: unusable export, local
        mirror, gate still closed.
        """
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("the closed sets live in the capability registry")
        assert accepted.startswith(tier.get("fallback_strategy")
                                   or tier.get("billing_mode")
                                   or "min")  # the typo really is a substring
        monkeypatch.setattr(rules_mod._caps, attr, accepted, raising=False)
        config = _cfg({"T2": dict({"model": "m2", "provider": "p2"}, **tier)})
        assert lint(config) != []

    def test_a_signal_vocabulary_that_is_not_a_set_skips_the_field_check(
        self, monkeypatch
    ):
        """No canonical list means no guess — and no legitimate field refused.

        The failure mode this direction avoids is the expensive one: a vocabulary
        lint cannot read is not evidence that ``needs_vision`` is a typo, and
        refusing it at the write gate strands the operator outside the guarded
        path over a field that works.
        """
        if signals_mod is None:  # pragma: no cover - signals always ships
            pytest.skip("signals module required")
        config = _rules_cfg({"id": "vision", "when": {"needs_vision": {"eq": True}},
                             "then": {"model": "T2"}})
        for broken in (["needs_vision"], frozenset(), None):
            monkeypatch.setattr(signals_mod, "KNOWN_FEATURE_NAMES", broken,
                                raising=False)
            assert rules_mod._known_when_fields() is None
            assert lint(config) == []

    def test_upstream_grouping_degrades_to_provider_identity(self, monkeypatch):
        """Without the alias table the literal pair is still caught, the alias is not.

        Both halves are asserted, because the honest degrade is "less redundancy
        analysis", never "a rail pair invented from a table nobody has".
        """
        if rules_mod._caps is None:  # pragma: no cover - registry always ships
            pytest.skip("upstream aliasing lives in the capability registry")
        literal = _cfg({"T4": {"model": "m4", "provider": "zai",
                               "fallback": [{"model": "f1", "provider": "zai"}]}})
        reseller = _cfg({"T4": {"model": "m4", "provider": "nous",
                                "fallback": [{"model": "f1",
                                              "provider": "openrouter"}]}})
        assert any("share upstream" in w for w in lint_warnings(reseller))
        monkeypatch.setattr(rules_mod, "_caps", None)
        assert any("share upstream 'zai'" in w for w in lint_warnings(literal))
        assert not any("share upstream" in w for w in lint_warnings(reseller))

    def test_a_registry_that_raises_on_upstream_group_still_catches_the_pair(
        self, monkeypatch
    ):
        """The same degrade, reached by a stale table rather than a missing one."""
        def boom(provider):
            raise ValueError("stale registry")

        monkeypatch.setattr(rules_mod._caps, "upstream_group", boom)
        config = _cfg({"T4": {"model": "m4", "provider": "zai",
                              "fallback": [{"model": "f1", "provider": "zai"}]}})
        assert any("share upstream 'zai'" in w for w in lint_warnings(config))


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


def _first_match(rows, **features):
    """The rule id first-match ACTUALLY lands on for this feature vector.

    Every shadow verdict is a claim about this function: "shadowed" means no vector
    can reach the later row, and silence means one can. Asserting the claim against
    the engine is the only way the two cannot drift — a lint that reports a shadow
    the matcher does not produce blocks a legitimate config at the write gate.
    """
    return match(_mkf(**features), False, rows, {"action": "classify"},
                 ROUTER_CONFIG["tiers"])[1]


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

    def test_a_disabled_row_neither_shadows_nor_is_shadowed(self):
        """A rule the operator turned off is dead by declaration, not by shadow.

        _shadowed_pairs must skip it on BOTH sides: as the later row (it cannot
        fire, so nothing is dead BECAUSE of it) and as the earlier row (it skips
        in the matcher, so it cannot kill the row behind it). Otherwise the
        console's disable button would write enabled:false and the amber finding
        would survive, telling the operator the fix did not work.
        """
        # Disabled LATER row: the pair lint used to flag must go quiet.
        config = _rules_cfg(
            {"id": "broad", "when": {"has_code": {"eq": True}}, "then": {"model": "T2"}},
            {"id": "dead", "enabled": False,
             "when": {"has_code": {"eq": True}}, "then": {"model": "T3"}},
        )
        assert lint(config) == []
        assert lint_findings(config) == []
        # Disabled EARLIER row: the row behind it becomes reachable, so it is
        # not shadowed by anything.
        config = _rules_cfg(
            {"id": "off", "enabled": False,
             "when": {"has_code": {"eq": True}}, "then": {"model": "T2"}},
            {"id": "alive", "when": {"has_code": {"eq": True}}, "then": {"model": "T3"}},
        )
        assert lint(config) == []
        assert lint_findings(config) == []
        # And the matcher agrees: the reachable row really fires.
        assert _first_match(config["rules"], has_code=True) == "alive"

    def test_disabled_rule_is_never_a_finding_even_with_identical_when(self):
        """The one case that used to be shadowed unconditionally."""
        config = _rules_cfg(
            {"id": "a", "when": {"verb_class": {"eq": "hard"}}, "then": {"model": "T4"}},
            {"id": "b", "enabled": False,
             "when": {"verb_class": {"eq": "hard"}}, "then": {"model": "T3"}},
        )
        assert lint(config) == []
        assert lint_findings(config) == []

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


class TestShadowContainmentEdges:
    """The containment cases at the edges of each operator family.

    Every case asserts BOTH halves of the same claim: what lint() reports, and what
    the engine does with a feature vector chosen to test it. A reported shadow is
    witnessed by the later row losing on its own best input; a silent pair is
    witnessed by a vector that reaches the later row, which is why refusing it
    would have been wrong.
    """

    def test_a_condition_that_only_requires_presence_shadows_the_field(self):
        """`est_input_tokens: {}` constrains nothing beyond the field being there.

        Presence is the one thing the later row also requires, so every vector the
        later row admits the earlier row admits too. The witness is the later row's
        own best case: 900k satisfies `gt: 800000` and still routes to T2.
        """
        rows = [
            {"id": "any-context", "when": {"est_input_tokens": {}},
             "then": {"model": "T2"}},
            {"id": "gigantic", "when": {"est_input_tokens": {"gt": 800000}},
             "then": {"model": "T4"}},
        ]
        assert (
            "rule 'gigantic' is shadowed by earlier rule 'any-context'"
            in lint(_rules_cfg(*rows))
        )
        assert _first_match(rows, est_input_tokens=900000) == "any-context"

    def test_a_bounded_row_never_shadows_the_presence_only_row_after_it(self):
        """The other direction: the empty condition admits what no bound does."""
        rows = [
            {"id": "gigantic", "when": {"est_input_tokens": {"gt": 800000}},
             "then": {"model": "T4"}},
            {"id": "any-context", "when": {"est_input_tokens": {}},
             "then": {"model": "T2"}},
        ]
        assert _shadow_errors(_rules_cfg(*rows)) == []
        assert _first_match(rows, est_input_tokens=10) == "any-context"

    def test_excluding_less_shadows_excluding_more(self):
        """Excluding fewer values ADMITS more: `ne: hard` covers `nin: [hard, trivial]`."""
        rows = [
            {"id": "not-hard", "when": {"verb_class": {"ne": "hard"}},
             "then": {"model": "T2"}},
            {"id": "neither", "when": {"verb_class": {"nin": ["hard", "trivial"]}},
             "then": {"model": "T3"}},
        ]
        assert (
            "rule 'neither' is shadowed by earlier rule 'not-hard'"
            in lint(_rules_cfg(*rows))
        )
        assert _first_match(rows, verb_class="moderate") == "not-hard"

    def test_excluding_more_does_not_shadow_excluding_less(self):
        """Reversed, the later row owns the values the earlier one excludes."""
        rows = [
            {"id": "neither", "when": {"verb_class": {"nin": ["hard", "trivial"]}},
             "then": {"model": "T3"}},
            {"id": "not-hard", "when": {"verb_class": {"ne": "hard"}},
             "then": {"model": "T2"}},
        ]
        assert _shadow_errors(_rules_cfg(*rows)) == []
        assert _first_match(rows, verb_class="trivial") == "not-hard"

    @pytest.mark.parametrize(
        "later_bounds",
        [
            {"gt": 200000, "gte": 500000},   # the tighter floor written second
            {"gte": 500000, "gt": 200000},   # ... and first
            {"gte": 500000, "gt": 500000},   # same number: the exclusive one wins
        ],
    )
    def test_two_lower_bounds_reduce_to_the_tighter_one(self, later_bounds):
        """A row that kept its old floor beside a new one means the tighter one.

        `gt` and `gte` on one field are ANDed at match time, so the row admits the
        SMALLER set — whichever order they appear in the file. Reading it any other
        way would let a row whose real floor is at or above an earlier row's look
        reachable, and the operator would never see it never firing.
        """
        rows = [
            {"id": "over-500k", "when": {"est_input_tokens": {"gte": 500000}},
             "then": {"model": "T3"}},
            {"id": "two-floors", "when": {"est_input_tokens": later_bounds},
             "then": {"model": "T4"}},
        ]
        assert (
            "rule 'two-floors' is shadowed by earlier rule 'over-500k'"
            in lint(_rules_cfg(*rows))
        )
        assert _first_match(rows, est_input_tokens=900000) == "over-500k"

    def test_two_lower_bounds_below_the_earlier_floor_still_fire(self):
        """The tighter of the two is 400k, and 400k..500k is the later row's own."""
        rows = [
            {"id": "over-500k", "when": {"est_input_tokens": {"gte": 500000}},
             "then": {"model": "T3"}},
            {"id": "two-floors",
             "when": {"est_input_tokens": {"gt": 200000, "gte": 400000}},
             "then": {"model": "T4"}},
        ]
        assert _shadow_errors(_rules_cfg(*rows)) == []
        assert _first_match(rows, est_input_tokens=450000) == "two-floors"

    @pytest.mark.parametrize(
        "later_bounds",
        [
            {"lt": 900000, "lte": 500000},   # the tighter ceiling written second
            {"lte": 500000, "lt": 900000},   # ... and first
            {"lte": 500000, "lt": 500000},   # same number: the exclusive one wins
        ],
    )
    def test_two_upper_bounds_reduce_to_the_tighter_one(self, later_bounds):
        """The ceiling mirror of the floor case, and ANDed the same way."""
        rows = [
            {"id": "under-500k", "when": {"est_input_tokens": {"lte": 500000}},
             "then": {"model": "T1"}},
            {"id": "two-ceilings", "when": {"est_input_tokens": later_bounds},
             "then": {"model": "T2"}},
        ]
        assert (
            "rule 'two-ceilings' is shadowed by earlier rule 'under-500k'"
            in lint(_rules_cfg(*rows))
        )
        assert _first_match(rows, est_input_tokens=1000) == "under-500k"

    def test_two_upper_bounds_above_the_earlier_ceiling_still_fire(self):
        rows = [
            {"id": "under-500k", "when": {"est_input_tokens": {"lte": 500000}},
             "then": {"model": "T1"}},
            {"id": "two-ceilings",
             "when": {"est_input_tokens": {"lte": 900000, "lt": 800000}},
             "then": {"model": "T2"}},
        ]
        assert _shadow_errors(_rules_cfg(*rows)) == []
        assert _first_match(rows, est_input_tokens=600000) == "two-ceilings"

    def test_a_window_inside_a_ceiling_only_row_is_reported(self):
        """Unbounded BELOW admits everything, so only the ceilings need comparing."""
        rows = [
            {"id": "under-900k", "when": {"est_input_tokens": {"lt": 900000}},
             "then": {"model": "T2"}},
            {"id": "500k-to-800k",
             "when": {"est_input_tokens": {"gt": 500000, "lt": 800000}},
             "then": {"model": "T3"}},
        ]
        assert (
            "rule '500k-to-800k' is shadowed by earlier rule 'under-900k'"
            in lint(_rules_cfg(*rows))
        )
        assert _first_match(rows, est_input_tokens=600000) == "under-900k"

    def test_a_ceiling_never_shadows_a_row_with_no_ceiling(self):
        """`gt: 500000` admits arbitrarily large values; `lt: 900000` does not."""
        rows = [
            {"id": "under-900k", "when": {"est_input_tokens": {"lt": 900000}},
             "then": {"model": "T2"}},
            {"id": "over-500k", "when": {"est_input_tokens": {"gt": 500000}},
             "then": {"model": "T3"}},
        ]
        assert _shadow_errors(_rules_cfg(*rows)) == []
        assert _first_match(rows, est_input_tokens=950000) == "over-500k"

    def test_a_strict_ceiling_does_not_shadow_the_inclusive_one(self):
        """`lte: 500000` admits exactly 500000; `lt: 500000` does not.

        The upper-bound mirror of the `gt`/`gte` boundary case, and the witness is
        the single value the two disagree about.
        """
        rows = [
            {"id": "strict", "when": {"est_input_tokens": {"lt": 500000}},
             "then": {"model": "T1"}},
            {"id": "inclusive", "when": {"est_input_tokens": {"lte": 500000}},
             "then": {"model": "T2"}},
        ]
        assert _shadow_errors(_rules_cfg(*rows)) == []
        assert _first_match(rows, est_input_tokens=500000) == "inclusive"

    def test_an_inclusive_ceiling_shadows_the_strict_one(self):
        rows = [
            {"id": "inclusive", "when": {"est_input_tokens": {"lte": 500000}},
             "then": {"model": "T2"}},
            {"id": "strict", "when": {"est_input_tokens": {"lt": 500000}},
             "then": {"model": "T1"}},
        ]
        assert (
            "rule 'strict' is shadowed by earlier rule 'inclusive'"
            in lint(_rules_cfg(*rows))
        )
        assert _first_match(rows, est_input_tokens=400000) == "inclusive"

    @pytest.mark.parametrize(
        "nested", [[["hard", "trivial"]], {"hard": True}],
    )
    def test_a_membership_operand_that_cannot_form_a_set_is_not_shadowed(
        self, nested
    ):
        """One indentation level too many, and containment stops being decidable.

        `in: [[hard, trivial]]` is what an extra `- ` in YAML produces, and a set
        cannot be formed from it. The answer is silence, never a shadow: lint() is
        the write gate, so a false shadow refuses a legitimate config and strands
        the operator outside the guarded path, while a missed one leaves a row that
        is visible in the file and visible in the decision log as a rule id with
        zero hits.

        The un-nested pair below is the control: the two configs differ only by that
        one level, and the decidable one IS reported. So silence here is the
        conservative direction, not an accident of the shape.
        """
        rows = [
            {"id": "either", "when": {"verb_class": {"in": ["hard", "trivial"]}},
             "then": {"model": "T3"}},
            {"id": "nested", "when": {"verb_class": {"in": nested}},
             "then": {"model": "T1"}},
        ]
        assert _shadow_errors(_rules_cfg(*rows)) == []

        decidable = [
            rows[0],
            {"id": "nested", "when": {"verb_class": {"in": ["hard"]}},
             "then": {"model": "T1"}},
        ]
        assert (
            "rule 'nested' is shadowed by earlier rule 'either'"
            in lint(_rules_cfg(*decidable))
        )

    def test_an_exclusion_operand_that_cannot_form_a_set_is_not_shadowed(self):
        """The same undecidable operand on the exclusion side, same direction.

        `nin: [[hard]]` excludes a set this module cannot build, so it cannot know
        the earlier row excludes no more than the later one — and it says nothing.
        The control differs only in the nesting and IS reported.
        """
        rows = [
            {"id": "neither", "when": {"verb_class": {"nin": [["hard"]]}},
             "then": {"model": "T3"}},
            {"id": "not-hard", "when": {"verb_class": {"ne": "hard"}},
             "then": {"model": "T2"}},
        ]
        assert _shadow_errors(_rules_cfg(*rows)) == []

        decidable = [
            {"id": "neither", "when": {"verb_class": {"nin": ["hard"]}},
             "then": {"model": "T3"}},
            rows[1],
        ]
        assert (
            "rule 'not-hard' is shadowed by earlier rule 'neither'"
            in lint(_rules_cfg(*decidable))
        )


# ---------------------------------------------------------------------------
# lint_findings: structured jump targets beside the write gate
# ---------------------------------------------------------------------------

class TestLintFindings:
    """lint_findings() must agree with lint() pair for pair.

    The console's "Ver regra N" button is built FROM these findings. A finding
    that named a pair lint() never reported would send the operator to a rule
    that is not broken; a shadow lint() reports but findings miss would leave
    the only actionable message dead again — the defect this surface exists to
    close.
    """

    def test_finding_shape_for_the_shadowed_pair(self):
        config = _rules_cfg(
            {"id": "broad", "when": {"has_code": {"eq": True}}, "then": {"model": "T2"}},
            {"id": "narrow", "when": {"has_code": {"eq": True}}, "then": {"model": "T1"}},
        )
        assert lint_findings(config) == [{
            "code": "shadowed",
            "later_index": 1,
            "later_id": "narrow",
            "earlier_index": 0,
            "earlier_id": "broad",
            "message": "rule 'narrow' is shadowed by earlier rule 'broad'",
        }]

    def test_message_is_the_exact_write_gate_string(self):
        """The alignment contract: each finding's message IS the lint() error.

        service.py pairs the two lists by this string; if they ever disagree,
        the console's jump button points at nothing.
        """
        config = _rules_cfg(
            {"id": "huge", "when": {"est_input_tokens": {"gt": 400000}},
             "then": {"model": "T3"}},
            {"id": "gigantic", "when": {"est_input_tokens": {"gt": 800000}},
             "then": {"model": "T4"}},
        )
        errors = lint(config)
        assert errors == ["rule 'gigantic' is shadowed by earlier rule 'huge'"]
        assert [f["message"] for f in lint_findings(config)] == errors

    def test_one_finding_per_shadowed_pair_in_report_order(self):
        """Three identical rows: every later row shadows every earlier one."""
        config = _rules_cfg(
            {"id": "a", "when": {"has_code": {"eq": True}}, "then": {"model": "T2"}},
            {"id": "b", "when": {"has_code": {"eq": True}}, "then": {"model": "T1"}},
            {"id": "c", "when": {"has_code": {"eq": True}}, "then": {"model": "T4"}},
        )
        findings = lint_findings(config)
        assert [(f["later_id"], f["earlier_id"]) for f in findings] == [
            ("b", "a"), ("c", "a"), ("c", "b"),
        ]

    def test_clean_config_yields_no_findings(self):
        config = _rules_cfg(
            {"id": "gigantic", "when": {"est_input_tokens": {"gt": 800000}},
             "then": {"model": "T4"}},
            {"id": "huge", "when": {"est_input_tokens": {"gt": 400000}},
             "then": {"model": "T3"}},
        )
        assert lint(config) == []
        assert lint_findings(config) == []

    def test_shipped_policy_yields_no_findings(self):
        assert lint_findings(ROUTER_CONFIG) == []

    def test_invalid_root_shapes_yield_no_findings(self):
        assert lint_findings("just-a-string") == []
        assert lint_findings({}) == []
        assert lint_findings({"rules": 5}) == []

    def test_malformed_rows_are_skipped_not_raised(self):
        """A non-dict row is lint()'s own error; findings must skip it, not die."""
        config = _rules_cfg(
            {"id": "broad", "when": {"has_code": {"eq": True}}, "then": {"model": "T2"}},
            "not-a-rule",
            {"id": "narrow", "when": {"has_code": {"eq": True}}, "then": {"model": "T1"}},
        )
        findings = lint_findings(config)
        assert [(f["later_id"], f["earlier_id"]) for f in findings] == [("narrow", "broad")]


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

    def test_an_operator_outside_the_closed_set_is_reported_once(self):
        """One typo, one diagnostic: the bounds check skips an op lint refused.

        The value is out of range on purpose. `between` names no bound this module
        knows how to read, so a bounds error here would be lint inventing a second
        defect out of the first — and pointing the operator at the hours when what
        is wrong is the operator name.
        """
        config = _rules_cfg({"id": "peak", "when": {"utc_hour": {"between": [0, 99]}},
                            "then": {"model": "T1"}})
        assert lint(config) == [
            "rule 'peak': 'when.utc_hour' uses unknown operator 'between'"
        ]


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
    def test_cap_drops_the_dollar_rails_that_are_peaking(self):
        """A dollar cap drops a dollar rail over it, and only a dollar rail.

        Both halves matter, so both are asserted. deepseek-v4-pro (metered) and
        glm-5.3 (plan) carry the SAME 2.0 multiplier at this hour, and only the
        metered one is capped: a plan rail spends credits off an allowance
        already bought, so `max_multiplier` — a statement about dollars — has
        nothing to say about it. Capping it would evict the one rail costing no
        marginal dollars and push the request onto a metered one, which is the
        opposite of what a cost cap is for.
        """
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "glm-5.3"]
        assert plan["capped"] == [
            {"model": "deepseek-v4-pro", "multiplier": 2.0},
        ]
        # Non-vacuity: the survivor is peaking just as hard as the casualty, so
        # this cannot pass by glm-5.3 simply being cheap at this hour.
        assert plan["multipliers"]["glm-5.3"] == 2.0
        assert plan["time_cap_bypassed"] is False
        assert plan["time_cap"] == {"max_multiplier": 1.5}

    def test_off_peak_keeps_every_rail(self):
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=OFF_PEAK)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == []
        assert plan["time_cap_bypassed"] is False

    def test_the_weekend_exempts_both_vendors_now(self):
        """The weekend is off-peak for zai AND deepseek, so nothing is capped.

        This test used to be named for an asymmetry — zai exempt on Saturday,
        deepseek not — and that asymmetry ended on 2026-08-22, when deepseek
        narrowed its peak to Monday-Friday (measured 2026-08-26 on
        api-docs.deepseek.com; the vendor edited the page without a changelog
        entry). The `weekdays` mechanism itself stays covered without depending on
        a vendor calendar: test_capabilities pins zai's Saturday exemption and
        deepseek's in test_deepseek_peak_does_not_reach_the_weekend.
        """
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_SATURDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == []

    def test_bypass_restores_the_chain_and_keeps_the_diagnostics(self):
        """A cost control must never be able to cause an outage.

        Every hop here is dollar-billed and over the cap, which is what it takes
        to reach the bypass now that a plan rail is immune: with a plan hop in
        the tier the cap can never empty the chain, so it would never bypass and
        this test would silently stop exercising the invariant it names.
        """
        tiers = {"T3": {
            "model": "deepseek-v4-pro", "provider": "deepseek",
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [{"model": "deepseek-v4-flash", "provider": "deepseek"}],
        }}
        plan = plan_chain(resolve_tiers({"model": "T3"}, tiers), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["deepseek-v4-pro", "deepseek-v4-flash"]
        assert plan["time_cap_bypassed"] is True
        assert plan["capped"] == [
            {"model": "deepseek-v4-pro", "multiplier": 2.0},
            {"model": "deepseek-v4-flash", "multiplier": 2.0},
        ]

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

    def test_a_preferred_rail_is_promoted_on_its_own_merit(self):
        """`prefer` promotes; the multiplier does not have to be a discount.

        This asserted xiaomi's 0.8x until 2026-08-26, when the window was removed
        (the vendor scopes it to the prepaid Token Plan and this install bills
        pay-as-you-go). Promotion never depended on the discount — it is driven by
        the policy's `prefer` list — and that is exactly what is pinned here now.
        The "a multiplier below 1.0 is not a peak" mechanism lives in
        test_capabilities, on a declared rail instead of a vendor promotion.
        """
        plan = plan_chain(resolve_tiers({"model": "T4"}, TIME_TIERS), _mkf(),
                          when=CHEAP_WINDOW)
        assert plan["promoted"] == ["mimo-v2.5"]
        assert plan["multipliers"]["mimo-v2.5"] == 1.0

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
        assert _models(plan) == ["gpt-5.6-terra", "glm-5.3"]
        assert [c["model"] for c in plan["capped"]] == ["deepseek-v4-pro"]
        assert plan["demoted"] == []
        # The sharp proof of the ordering: deepseek-v4-pro is a `deepseek` elo in
        # a dearer window, so an avoid_peak that ran FIRST would have named it.
        # It is absent because the cap had already removed it — the policy only
        # ever sees the set that survived.
        assert plan["peak_priced"] == ["glm-5.3"]

    def test_policy_reports_the_price_even_when_it_moves_nothing(self):
        """Same tier, cap raised so the policy sees every hop.

        The matched elos are ALREADY the trailing hops, so demoting them is the
        identity permutation and `demoted` is empty — while `peak_priced` still
        names both, because they really are charging double at this hour. One
        field could not carry both readings honestly: a console rendering
        `demoted` as "moved to the end" was asserting an order that never
        changed.
        """
        tiers = {"T1": dict(TIME_TIERS["T1"],
                            time_cap={"max_multiplier": 2.0},
                            time_policy={"avoid_peak": ["deepseek", "zai"]})}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == []
        assert plan["demoted"] == []
        assert plan["peak_priced"] == ["deepseek-v4-pro", "glm-5.3"]

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
                # Text-only: the FILTER removes it, so the cap never sees it.
                {"model": "deepseek-v4-flash", "provider": "deepseek"},
                # Declared vision, so it survives the filter and reaches the cap
                # — which drops it, being dollar-billed at 2.0 this hour.
                {"model": "deepseek-v4-pro", "provider": "deepseek",
                 "vision": True},
                {"model": "glm-4.6v", "provider": "zai"},
            ],
        }}
        plan = plan_chain(resolve_tiers({"model": "T1"}, tiers),
                          _vision_features(), when=PEAK_MONDAY)
        # Two stages, two casualties, each named by the stage that removed it.
        assert [h["model"] for h in plan["rejected"]] == ["deepseek-v4-flash"]
        assert [c["model"] for c in plan["capped"]] == ["deepseek-v4-pro"]
        assert _models(plan) == ["gpt-5.6-terra", "glm-4.6v"]

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


class TestHopBillingModeIsACheapestNowKey(_NeedsRegistry):
    """A hop's declared billing_mode is the OUTER `cheapest_now` sort key.

    Which is why lint() has to validate it, and why validating it only on the
    tier's own elo was a money defect rather than a tidiness one. The tier below
    is the reproduction: `cheapest_now`, `pin_primary: false`, primary gpt-5.5
    (subscription, $30.00/1M out) and one hop glm-4.7-flashx (metered, $0.40/1M
    out) — 75x cheaper on output, and both flat-priced, so nothing here depends
    on the hour.
    """

    HOP = {"model": "glm-4.7-flashx", "provider": "zai"}
    TIER = {
        "model": "gpt-5.5", "provider": "openai-codex",
        "fallback_strategy": "cheapest_now", "pin_primary": False,
    }

    def _tiers(self, mode):
        return {"T1": dict(self.TIER,
                           fallback=[dict(self.HOP, billing_mode=mode)])}

    def _order(self, mode):
        return _models(plan_chain(
            resolve_tiers({"model": "T1"}, self._tiers(mode)), _mkf(),
            rng=random.Random(0), when=OFF_PEAK,
        ))

    def test_the_declared_mode_puts_the_cheap_rail_first(self):
        assert self._order("metered") == ["glm-4.7-flashx", "gpt-5.5"]

    def test_a_typo_demotes_the_cheap_rail_behind_the_expensive_one(self):
        """The behaviour the gate now refuses to let ship, pinned as behaviour.

        plan_chain is right to do this: an elo whose billing mode nothing can
        describe cannot be claimed to be the cheapest. The defect was never here —
        it was lint() letting `meterd` reach it, so this case exists to show the
        cost of that and to fail loudly if the ranking itself is ever "fixed"
        instead.
        """
        assert self._order("meterd") == ["gpt-5.5", "glm-4.7-flashx"]

    def test_the_gate_refuses_the_typo_and_passes_the_real_mode(self):
        assert any("fallback[0]: 'billing_mode'" in e
                   for e in lint(_cfg(self._tiers("meterd"))))
        assert lint(_cfg(self._tiers("metered"))) == []


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
        assert _models(plan) == ["gpt-5.6-terra", "glm-5.3"]

    def test_an_aware_datetime_is_converted(self):
        """The operator's UTC-03 03:00 is the 06:00 UTC peak."""
        local = datetime(2026, 8, 17, 3, 0, tzinfo=timezone(timedelta(hours=-3)))
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=local)
        assert plan["utc_hour"] == 6
        assert [c["model"] for c in plan["capped"]] == ["deepseek-v4-pro"]

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

    def test_a_datetime_with_no_utc_reading_at_all_is_no_clock(self):
        """`datetime.min` in a positive offset cannot be converted to UTC.

        utctimetuple() subtracts the offset, walks off the bottom of the calendar
        and raises OverflowError — from an ordinary datetime, no stub involved
        (asserted, so this stays a real provocation). The planner's answer is the
        no-clock case, and the hour keys are ABSENT rather than null: `Number(null)`
        is 0 in a JSON consumer, so a null hour renders as midnight and this plan
        would claim an hour it could not read.
        """
        sentinel = datetime.min.replace(tzinfo=timezone(timedelta(hours=1)))
        with pytest.raises(OverflowError):
            sentinel.utctimetuple()

        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=sentinel)
        assert plan["time_agnostic"] is True
        assert "utc_hour" not in plan and "utc_weekday" not in plan
        assert plan["capped"] == [] and plan["multipliers"] == {}
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]


class _DuckClock:
    """The least a caller can hand `when` — because `when` is duck-typed.

    rules.py answers "which hour is this?" WITHOUT importing datetime (that is
    what makes "this module cannot read a clock" a property of the file rather
    than a promise in its docstring), so what it accepts is anything that answers
    `utctimetuple()` and `weekday()`. A trace decoder or a sibling deployment's
    shim is such an object, and unlike a datetime it can answer nonsense.
    """

    class _Parts:
        def __init__(self, tm_hour, tm_wday):
            self.tm_hour = tm_hour
            self.tm_wday = tm_wday

    def __init__(self, hour, tm_hour=None, tm_wday=0):
        self.hour = hour
        self._tm_hour = hour if tm_hour is None else tm_hour
        self._tm_wday = tm_wday

    def weekday(self):
        return self._tm_wday

    def utctimetuple(self):
        return self._Parts(self._tm_hour, self._tm_wday)


class TestADuckTypedClockIsCheckedNotTrusted(_NeedsRegistry):
    """Every case here asserts the two readings AGREE about what this clock is.

    rules._clock_parts mirrors capabilities._utc_parts on purpose: the hour the
    plan REPORTS and the hour the multipliers were taken AT must be the same
    reading, or the trace explains an order that was decided at another hour. For
    a datetime the two cannot diverge; for a duck-typed clock they can, so the
    agreement is asserted rather than assumed.
    """

    def test_a_usable_shim_is_used_exactly_like_the_datetime(self):
        duck = _DuckClock(hour=7, tm_wday=0)
        assert rules_mod._clock_parts(duck) == rules_mod._caps._utc_parts(duck)
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=duck)
        real = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert (plan["utc_hour"], plan["utc_weekday"]) == (7, 0)
        assert _models(plan) == _models(real)
        assert plan["capped"] == real["capped"]
        assert plan["multipliers"] == real["multipliers"]

    def test_a_clock_that_cannot_name_an_hour_is_no_clock(self):
        """No hour, no plan hour — never hour 0.

        A stdlib `date` never reaches this guard (it has no `utctimetuple` at all),
        so what it holds off is a shim that answers both calls and still has no
        hour to give. Defaulting it to 0 would price every such plan at midnight.
        """
        duck = _DuckClock(hour=None, tm_hour=7)
        assert rules_mod._clock_parts(duck) is None
        assert rules_mod._caps._utc_parts(duck) is None
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=duck)
        assert plan["time_agnostic"] is True
        assert "utc_hour" not in plan
        assert plan["capped"] == [] and plan["multipliers"] == {}

    @pytest.mark.parametrize(
        "tm_hour,tm_wday", [(24, 0), (-1, 0), (7, 7), (7, -1)],
    )
    def test_a_reading_outside_the_real_bounds_is_refused(self, tm_hour, tm_wday):
        """An hour of 24 is not an hour, and the console would render it as one.

        The bounds are not defensive decoration: utc_hour/utc_weekday leave this
        module as plan fields that a dashboard prints and a price window is matched
        against, and a window is [0, 24) on days 0..6. A reading outside that is
        rejected as "no clock" rather than passed on as a number.
        """
        duck = _DuckClock(hour=tm_hour, tm_hour=tm_hour, tm_wday=tm_wday)
        assert rules_mod._clock_parts(duck) is None
        assert rules_mod._caps._utc_parts(duck) is None
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=duck)
        assert plan["time_agnostic"] is True
        assert "utc_hour" not in plan and "utc_weekday" not in plan
        assert plan["multipliers"] == {}


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

    def test_a_cap_stage_that_returns_the_pre_diagnostics_shape_is_discarded(
        self, monkeypatch
    ):
        """A stage that answers with a bare chain is not a stage this reads.

        The older shape returned the surviving chain and nothing else. Trusting it
        would mean reading `.get` off a list — a TypeError in the request path over
        a cost control — so the result is discarded whole: no cap ran, and the plan
        says so. The prices are still reported, which is what lets an operator see
        the 2.0x they wanted capped.
        """
        monkeypatch.setattr(
            rules_mod._caps, "apply_time_cap",
            lambda chain, cap, when=None: list(chain),
        )
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["capped"] == [] and plan["time_cap_bypassed"] is False
        assert plan["multipliers"]["deepseek-v4-pro"] == 2.0

    def test_a_broken_policy_stage_costs_only_the_policy(self, monkeypatch):
        """Measured against what the live stage does at this hour, not a constant.

        T2 declares `avoid_peak: [deepseek, zai]` and both are peaking at 07:00Z,
        so the live stage moves something here — asserted first, so the degraded
        half cannot pass by describing an hour where nothing would have moved
        anyway.
        """
        live = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert live["demoted"] == ["deepseek-v4-pro"]

        def boom(*_args, **_kwargs):
            raise KeyError("stale registry")

        monkeypatch.setattr(rules_mod._caps, "apply_time_policy", boom)
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "mimo-v2.5"]
        assert plan["demoted"] == [] and plan["promoted"] == []
        assert plan["peak_priced"] == []
        # Position was lost; membership and price were not.
        assert plan["multipliers"]["deepseek-v4-pro"] == 2.0

    def test_a_policy_stage_that_returns_the_pre_diagnostics_shape_is_discarded(
        self, monkeypatch
    ):
        """The older shape returned the reordered chain alone: not this contract.

        A bare list carries no demoted/promoted/peak_priced, and inventing empty
        ones beside a chain that DID move would report an order nobody can explain.
        Discarded in favour of the input, so the reported order and the reported
        movements describe the same call.
        """
        monkeypatch.setattr(
            rules_mod._caps, "apply_time_policy",
            lambda chain, policy, when=None: list(reversed(chain)),
        )
        plan = plan_chain(resolve_tiers({"model": "T2"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(plan) == ["gpt-5.6-terra", "deepseek-v4-pro", "mimo-v2.5"]
        assert plan["demoted"] == [] and plan["peak_priced"] == []


# ---------------------------------------------------------------------------
# capabilities.py a version BEHIND rules.py — the state a copy deploy makes
# ---------------------------------------------------------------------------

# cheapest_now over a plan rail and a subscription rail, with a dollar cap that
# drops the metered one at 07:00Z. The ORDER of the two survivors is the part that
# needs the clock: glm-5.3 leads only when prices are compared AT the hour.
STALE_ORDER_TIER = {"T1": {
    "model": "gpt-5.6-terra", "provider": "openai-codex",
    "billing_mode": "subscription",
    "fallback_strategy": "cheapest_now", "pin_primary": False,
    "time_cap": {"max_multiplier": 1.5},
    "fallback": [
        {"model": "deepseek-v4-pro", "provider": "deepseek",
         "billing_mode": "metered"},
        {"model": "glm-5.3", "provider": "zai", "billing_mode": "plan"},
    ],
}}


class TestAVersionSkewedRegistryCostsOneStage(_NeedsRegistry):
    """A registry that still has the name and no longer has the signature.

    This plugin is deployed by copy, so "capabilities.py is a version behind
    rules.py" is a real state — and every case here must cost the plan ONE thing.
    The alternative is plan_chain's defensive `except`, which degrades every stage
    at once, capability filtering included, and a filter that stops running routes
    a request to a model that cannot serve it.
    """

    def test_the_import_time_clock_probe_agrees_with_the_installed_registry(self):
        """One question, one answer: does THIS order_chain take a clock?

        The probe runs once, at import, and the call site branches on it. If the
        two ever disagreed the mismatch would surface as a TypeError inside
        plan_chain — i.e. as the whole-plan degrade this flag exists to avoid — so
        the flag is asserted against the installed signature rather than against
        True.
        """
        assert rules_mod._ORDER_CHAIN_ACCEPTS_WHEN is (
            "when" in inspect.signature(rules_mod._caps.order_chain).parameters
        )

    def test_an_orderer_that_predates_the_clock_loses_only_the_order(
        self, monkeypatch
    ):
        """The stale-registry case the import-time probe exists for.

        The legacy orderer IS the shipped one minus the clock, which is what a
        version-behind module actually is. So cheapest_now still runs and still
        ranks — on base rates instead of this hour's — and everything that is not
        ordering (the cap, the clock reading, the price report) is unchanged.
        """
        live = plan_chain(resolve_tiers({"model": "T1"}, STALE_ORDER_TIER), _mkf(),
                          when=PEAK_MONDAY)
        assert _models(live) == ["glm-5.3", "gpt-5.6-terra"]

        real_order_chain = rules_mod._caps.order_chain
        seen = []

        def legacy_order_chain(chain, strategy="sequential", pin_primary=True,
                               rng=None):
            seen.append(strategy)
            return real_order_chain(chain, strategy=strategy,
                                    pin_primary=pin_primary, rng=rng)

        accepts_when = "when" in inspect.signature(legacy_order_chain).parameters
        assert accepts_when is False
        monkeypatch.setattr(rules_mod._caps, "order_chain", legacy_order_chain)
        monkeypatch.setattr(rules_mod, "_ORDER_CHAIN_ACCEPTS_WHEN", accepts_when)

        plan = plan_chain(resolve_tiers({"model": "T1"}, STALE_ORDER_TIER), _mkf(),
                          when=PEAK_MONDAY)
        # The strategy still reaches the orderer; only the hour does not.
        assert seen == ["cheapest_now"]
        assert _models(plan) == ["gpt-5.6-terra", "glm-5.3"]
        assert plan["capped"] == [{"model": "deepseek-v4-pro", "multiplier": 2.0}]
        assert plan["utc_hour"] == 7 and plan["time_agnostic"] is False
        assert plan["multipliers"]["glm-5.3"] == 2.0

    def test_a_registry_without_price_multiplier_reports_no_prices(
        self, monkeypatch
    ):
        """No price function, no price claims — and the cap still runs.

        Hidden on the PROXY, not on the module: the registry's own cap stage calls
        its own price_multiplier, exactly as an older module would, so this is the
        one call rules.py makes going missing rather than the registry losing the
        ability to price. 1.0 for everything would be a claim about prices nobody
        checked; {} says "no reading", and `capped` still explains the 2.0x drop.
        """
        monkeypatch.setattr(
            rules_mod, "_caps",
            _RegistryWithout(rules_mod._caps, "price_multiplier"),
        )
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert plan["multipliers"] == {}
        assert plan["capped"] == [{"model": "deepseek-v4-pro", "multiplier": 2.0}]
        assert _models(plan) == ["gpt-5.6-terra", "glm-5.3"]

    def test_a_price_lookup_that_predates_declared_overrides_reports_no_prices(
        self, monkeypatch
    ):
        """rules.py passes the hop's declarations; the older signature has no slot.

        The skew is a TypeError on every call, so the multiplier REPORT is what is
        lost — never the cap that already ran, and never the plan.
        """
        real = rules_mod._caps.price_multiplier

        def legacy_price_multiplier(model, when=None):
            return real(model, when)

        monkeypatch.setattr(
            rules_mod, "_caps",
            _RegistryWith(rules_mod._caps,
                          price_multiplier=legacy_price_multiplier),
        )
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert plan["multipliers"] == {"deepseek-v4-pro": 2.0}
        assert plan["capped"] == [{"model": "deepseek-v4-pro", "multiplier": 2.0}]
        assert _models(plan) == ["gpt-5.6-terra", "glm-5.3"]

    def test_an_unpriceable_model_is_omitted_never_reported_as_null(
        self, monkeypatch
    ):
        """A None multiplier is a missing key, because null renders as 0.

        The older shape answered Optional[float] — None for "no window
        information" — where today's answers the flat 1.0. Carrying that None into
        the plan would put `Number(null) === 0` in front of the console: a rail
        described as costing nothing, which is the same class of silent wrongness
        `time_agnostic` exists to prevent. The other rails still report, so the
        omission is per elo rather than a blanket {}.
        """
        real = rules_mod._caps.price_multiplier

        def older_price_multiplier(model, when=None, declared=None):
            if model == "gpt-5.6-terra":
                return None
            return real(model, when, declared)

        monkeypatch.setattr(
            rules_mod, "_caps",
            _RegistryWith(rules_mod._caps,
                          price_multiplier=older_price_multiplier),
        )
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert "gpt-5.6-terra" in _models(plan)
        assert "gpt-5.6-terra" not in plan["multipliers"]
        assert plan["multipliers"] == {"deepseek-v4-pro": 2.0, "glm-5.3": 2.0}

    @pytest.mark.parametrize(
        "capped_entry",
        [
            {"model": "deepseek-v4-pro"},                       # names no number
            {"model": "deepseek-v4-pro", "multiplier": "2.0x"},  # formatted, not a number
            {"multiplier": 2.0},                                # names no elo
        ],
    )
    def test_a_capped_entry_without_a_usable_number_adds_none(
        self, monkeypatch, capped_entry
    ):
        """The cap's own number wins — and there is no second-best guess.

        `multipliers` exists to say what the plan was made on. An entry that does
        not name both an elo and a number says nothing, so it contributes nothing:
        no invented 1.0, and no string where every consumer reads a float.
        """
        real = rules_mod._caps.apply_time_cap

        def older_cap(chain, cap, when=None):
            result = dict(real(chain, cap, when=when))
            result["capped"] = [dict(capped_entry)]
            return result

        monkeypatch.setattr(rules_mod._caps, "apply_time_cap", older_cap)
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert plan["capped"] == [capped_entry]
        assert "deepseek-v4-pro" not in plan["multipliers"]
        assert plan["multipliers"] == {"glm-5.3": 2.0, "gpt-5.6-terra": 1.0}
        assert all(isinstance(v, float) for v in plan["multipliers"].values())

    def test_an_orderer_that_returns_names_instead_of_hops_still_prices(
        self, monkeypatch
    ):
        """A chain entry that is not a mapping is skipped, not indexed.

        Older rails were lists of model NAMES (the blocklist's fallback_chain still
        is). An orderer that answers in that shape must not turn the price report
        into an AttributeError, because that exception is caught one level up as
        "the registry is broken" and costs every stage, capability filter included.
        A non-empty `capped` is the proof it was not: the whole-plan degrade reports
        `capped: []` by construction, so the cap can only be named here if the
        stages really ran.
        """
        monkeypatch.setattr(
            rules_mod._caps, "order_chain",
            lambda chain, **_kwargs: [hop.get("model") for hop in chain],
        )
        plan = plan_chain(resolve_tiers({"model": "T1"}, TIME_TIERS), _mkf(),
                          when=PEAK_MONDAY)
        assert plan["chain"] == ["gpt-5.6-terra", "glm-5.3"]
        assert plan["capped"] == [{"model": "deepseek-v4-pro", "multiplier": 2.0}]
        # Only the elo the cap named could be read; the bare names carry no hop.
        assert plan["multipliers"] == {"deepseek-v4-pro": 2.0}


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

    def test_a_hop_that_is_not_an_elo_is_not_counted_as_expensive(self):
        """"Every elo peaks at some hour" must not be said about a non-elo.

        `model: 4.7` is what YAML makes of an unquoted glm-4.7. The advisory claims
        something about EVERY elo in the tier, so one it cannot read is one it
        cannot claim — silence, and the operator is pointed at the real defect by
        the gate instead. Both halves are asserted: the write is blocked, and the
        advisory does not invent a second finding on top.
        """
        config = _cfg({"T3": {
            "model": "deepseek-v4-pro", "provider": "deepseek",
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [{"model": 4.7, "provider": "zai"}],
        }})
        assert (
            "tier 'T3': fallback[0]: 'model' must be a non-empty string" in lint(config)
        )
        assert not any("time_cap will bypass" in w for w in lint_warnings(config))

    def test_a_model_the_registry_never_heard_of_is_not_counted_as_expensive(self):
        """An unknown elo has no windows to read, so it is not "in" one.

        Two advisories, one config, and each says only what it knows: the unknown
        model IS reported as unverifiable, and the cap is NOT reported as doomed —
        the elo might well be flat-priced, and a bypass warning would send the
        operator hunting for a window that does not exist.
        """
        config = _cfg({"T3": {
            "model": "totally-made-up-elo-xyz", "provider": "zai",
            "time_cap": {"max_multiplier": 1.5},
        }})
        warnings = lint_warnings(config)
        assert any("unknown to the capability registry" in w for w in warnings)
        assert not any("time_cap will bypass" in w for w in warnings)

    def test_a_malformed_window_entry_does_not_hide_a_real_one(self):
        """One bad entry in a declared list is skipped, not fatal to the scan.

        The list is the operator's own override, so it can be half-typed. The
        advisory reads past the entry it cannot use and still finds the 2.0x window
        the cap of 1.5 can never admit — while the gate names the malformed entry,
        so neither finding depends on the other.
        """
        config = _cfg({"T3": {
            "model": "gpt-5.6-terra", "provider": "openai-codex",
            "time_cap": {"max_multiplier": 1.5},
            "price_windows": ["nope", {"hours_utc": [6, 10], "multiplier": 2.0}],
        }})
        assert (
            "model 'gpt-5.6-terra': price_windows entry 0 is not a mapping"
            in lint(config)
        )
        assert any("time_cap will bypass" in w for w in lint_warnings(config))

    def test_a_hop_that_is_not_an_elo_does_not_count_as_priced(self):
        """"No priced elo" is about elos, and 4.7 is not one.

        glm-5.3 bills in plan credits and publishes no dollar price, so the
        advisory is correct here — and a hop whose id is a float must not be what
        silences it, because nothing about it can be compared in dollars either.
        """
        config = _cfg({"T2": {
            "model": "glm-5.3", "provider": "zai",
            "fallback_strategy": "cheapest_now",
            "fallback": [{"model": 4.7, "provider": "deepseek"}],
        }})
        assert (
            "tier 'T2': fallback[0]: 'model' must be a non-empty string" in lint(config)
        )
        assert (
            "tier 'T2': 'cheapest_now' with no priced elo degrades to "
            "billing_mode rank only"
        ) in lint_warnings(config)


class TestTimeWarningsUnderAVersionSkewedRegistry(_NeedsRegistry):
    """An advisory must never raise out of the lint path, whatever it asked.

    Both findings below are computed by calling the registry with the arguments
    THIS rules.py passes. A registry a version behind still has the name and no
    longer has the parameter, so the call is a TypeError — and the report the
    operator asked for has to arrive anyway.
    """

    #: Two deepseek rails, both 2.0x at 06:00-10:00: the live registry has
    #: something to say about the cap AND about the shared upstream.
    CAP_CONFIG = _cfg({"T3": {
        "model": "deepseek-v4-pro", "provider": "deepseek",
        "time_cap": {"max_multiplier": 1.5},
        "fallback": [{"model": "deepseek-v4-flash", "provider": "deepseek"}],
    }})

    def test_a_capability_lookup_that_predates_declared_overrides_is_silent(
        self, monkeypatch
    ):
        """The cap finding is lost; the findings that did not need prices are not.

        Asserted against the live report first, so "no bypass warning" means the
        skew silenced a finding that was really there rather than describing a
        config that never had one.
        """
        assert any(
            "time_cap will bypass" in w for w in lint_warnings(self.CAP_CONFIG)
        )
        real = rules_mod._caps.capabilities_for
        monkeypatch.setattr(
            rules_mod, "_caps",
            _RegistryWith(rules_mod._caps,
                          capabilities_for=lambda model: real(model)),
        )
        warnings = lint_warnings(self.CAP_CONFIG)
        assert not any("time_cap will bypass" in w for w in warnings)
        assert (
            "tier 'T3': first two hops share upstream 'deepseek' "
            "— no independent fallback"
        ) in warnings

    def test_a_price_lookup_that_predates_declared_overrides_cannot_block(
        self, monkeypatch
    ):
        """An unreadable price makes this advisory over-fire, and that is survivable.

        "Priced" is asked through effective_price so it means exactly what
        cheapest_now means by it. When the call itself is impossible the answer is
        "not priced", which over-reports — acceptable only because this channel
        cannot block a write, and asserted as such: lint() stays clean while the
        advisory appears.
        """
        config = _cfg({"T2": {
            "model": "deepseek-v4-pro", "provider": "deepseek",
            "fallback_strategy": "cheapest_now",
            "fallback": [{"model": "deepseek-v4-flash", "provider": "deepseek"}],
        }})
        assert not any("billing_mode rank only" in w for w in lint_warnings(config))
        real = rules_mod._caps.effective_price
        monkeypatch.setattr(
            rules_mod, "_caps",
            _RegistryWith(rules_mod._caps,
                          effective_price=lambda model, when=None: real(model, when)),
        )
        assert lint(config) == []
        assert any("billing_mode rank only" in w for w in lint_warnings(config))

    def test_a_window_check_that_predates_the_model_argument_is_skipped(
        self, monkeypatch
    ):
        """The gate loses the window check and nothing else — never the report.

        `price_window_diagnostics` is delegated to precisely so the gate and the
        registry self-check cannot disagree about a window; a signature the caller
        cannot satisfy is that delegation failing. Degrading to "unchecked" is the
        same direction the absent-function branch already takes, and lint() must
        still return every other diagnostic it had.
        """
        config = _cfg({"T2": {
            "model": "glm-5.3", "provider": "zai", "billing_mode": "meterd",
            "price_windows": [
                {"hours_utc": [6, 10], "multiplier": 2.0},
                {"hours_utc": [8, 12], "multiplier": 1.5},
            ],
        }})
        errors = lint(config)
        assert "model 'glm-5.3': price_windows entries overlap" in errors
        assert any("billing_mode" in e for e in errors)

        real = rules_mod._caps.price_window_diagnostics
        monkeypatch.setattr(
            rules_mod, "_caps",
            _RegistryWith(rules_mod._caps,
                          price_window_diagnostics=lambda windows: real("", windows)),
        )
        skewed = lint(config)
        assert not any("price_windows" in e for e in skewed)
        assert any("billing_mode" in e for e in skewed)


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
        assert [h["model"] for h in plan["chain"]] == ["gpt-5.6-terra", "glm-5.3"]

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
    "condition",
    [
        {"eq": True}, {"eq": False}, {"ne": True}, {"ne": False}, {"nin": [True]},
        # Not op maps at all: lint refuses these ("must be an op map"), and the
        # engine and the chips have to agree about them too — see
        # TestAClauseLintRefusesIsDeadNotFatal.
        True, "yes", 5, ["eq"],
    ],
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


class TestAClauseLintRefusesIsDeadNotFatal:
    """`when: {has_code: true}` — the op map an operator forgot to nest.

    lint() calls it invalid: "'when.has_code' must be an op map". The engine used
    to call it an AttributeError — `condition.items()` on a bool, raised straight
    out of match() into the request path — so the gate and the runner disagreed
    about a config the gate had already rejected, and the disagreement surfaced as
    a traceback on a hand-edited router.yaml rather than as a routing decision.

    They agree now, on the only reading that costs nothing: a condition that names
    no operator holds for nothing, so the row is dead — which is exactly what lint
    reports it as. Reading the bare value as an implied `eq` is the one answer
    worse than both, because it would route real traffic on a row the write gate
    refuses to accept.
    """

    @pytest.mark.parametrize("condition", [True, False, "yes", 5, ["eq"], None])
    def test_the_row_is_skipped_and_the_next_one_decides(self, condition):
        rows = [
            {"id": "bare-value", "when": {"has_code": condition},
             "then": {"model": "T4"}},
            {"id": "proper-op-map", "when": {"has_code": {"eq": True}},
             "then": {"model": "T1"}},
        ]
        assert (
            "rule 'bare-value': 'when.has_code' must be an op map"
            in lint(_rules_cfg(*rows))
        )
        output, rule_id = match(_mkf(has_code=True), False, rows,
                                {"action": "classify"}, ROUTER_CONFIG["tiers"])
        assert rule_id == "proper-op-map"
        assert output["model"] == "glm-5.2-fast"

    @pytest.mark.parametrize("condition", [True, "yes", 5, ["eq"]])
    def test_no_chip_is_offered_for_a_clause_that_decided_nothing(self, condition):
        """The unreadable clause explains nothing; the readable one still does.

        _matching_clauses reports per clause, so it drops only the clause the
        engine could not evaluate — and the engine rejects the whole row, so
        /explain never renders these chips for a match. What must never happen is
        the reverse of the pair: a chip that says a clause held when the engine
        found it unreadable.
        """
        when = {"has_code": condition, "verb_class": {"eq": "trivial"}}
        feats = _mkf(has_code=True, verb_class="trivial")
        assert rules_mod._all_clauses_match(when, feats, False) is False
        assert rules_mod._matching_clauses(when, feats, False) == {
            "verb_class": {"eq": "trivial"}
        }


# ---------------------------------------------------------------------------
# ONE cause table: the path that RUNS a decision and the surfaces that DISPLAY
# it must label it the same
# ---------------------------------------------------------------------------


def _live_policy():
    """The shipped router.yaml — the rule ids an operator actually sees labelled."""
    import yaml
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "router.yaml").read_text(encoding="utf-8"))


class _NoBans:
    """A blocklist that bans nothing, injected so these cases measure the CAUSE.

    With the real Blocklist a ban the operator happens to be carrying on a T2
    rail would turn the recorded cause into blocklist_veto, and the agreement
    assertion would fail for a reason that has nothing to do with the label.
    """

    def is_blocked(self, model="", provider=""):
        return False

    def fallback_for(self, model):
        return None


class TestOneCauseTable:
    """`rules._determine_cause` DISPLAYS a decision, `adapter._cause_from_rule` RUNS it.

    Both label the same rule match, on two surfaces an operator compares side by
    side: the trace and /routes come from the running path, /explain and the
    console from this module. Each used to own a copy of the rule-id table, and
    the copies drifted — measured on the shipped policy, four of eight rows
    (`vision-required`, `huge-context-read`, `cross-file-or-protocol`,
    `standard-implementation`) ran under one cause and explained themselves with
    another.

    Every case below asserts the two AGREE rather than asserting one side's
    value. A label both surfaces show is a label an operator can act on; two
    plausible labels that disagree is the defect this codebase has shipped four
    times.
    """

    def test_both_producers_label_every_shipped_rule_id_the_same(self):
        from router.adapter import _cause_from_rule
        from router.decision_log import VALID_CAUSES

        for rule in _live_policy()["rules"]:
            rid = rule["id"]
            display = rules_mod._determine_cause(rid, {})
            running = _cause_from_rule(rid, {})
            assert display == running, (
                f"rule {rid!r}: /explain says {display!r}, "
                f"the running path says {running!r}"
            )
            # The set is CLOSED, and that matters twice over: decision_log.record
            # coerces an unknown cause to fail_safe_strong, so a cause string
            # invented on either side would relabel healthy routes as fail-safe.
            assert display in VALID_CAUSES

    def test_both_producers_agree_on_the_fully_resolved_output(self):
        """The same comparison against what match() actually returns.

        The case above feeds an empty output; this one feeds the resolved `then` —
        a real model, provider and tier policy through resolve_tiers — because
        that is the pair the adapter labels at its rules stage and hands to
        record(). A branch keyed on the output rather than the id would show up
        here and nowhere else.
        """
        from router.adapter import _cause_from_rule

        policy = _live_policy()
        for rule in policy["rules"]:
            rid = rule["id"]
            output = resolve_tiers(rule.get("then", {}), policy["tiers"])
            if output.get("action") == "classify":
                continue  # a delegating row: see the dedicated case below
            assert (
                rules_mod._determine_cause(rid, output)
                == _cause_from_rule(rid, output)
            ), f"rule {rid!r} disagrees on its resolved output"

    def test_no_shipped_row_is_labelled_nothing_matched(self):
        """A row that DID match must not be labelled `default_fallthrough`.

        That is exactly what both producers said about `vision-required` and
        `huge-context-read` before the table existed: a live vision route recorded
        as "nothing matched", and an operator counting hits per cause seeing the
        two conditional-routing rows never fire.
        """
        for rule in _live_policy()["rules"]:
            rid = rule["id"]
            assert rules_mod._determine_cause(rid, {}) != "default_fallthrough", (
                f"rule {rid!r} carries no cause: add it to adapter._RULE_ID_CAUSES "
                f"rather than relying on the substring heuristic"
            )

    def test_the_vision_route_reports_one_cause_on_both_surfaces(self):
        """The verified symptom, end to end: recorded cause == /explain cause.

        Asserted as an equality between the two surfaces rather than against the
        string "keyword_match", so that if the honest label for a capability-keyed
        row is ever reargued (the closed set has no capability member, which is why
        keyword_match is the one it maps onto) this case keeps holding while
        test_both_producers_label_every_shipped_rule_id_the_same pins the value.
        """
        from router.adapter import route
        from router.decision_log import DecisionLog
        from router.signals import extract

        policy = _live_policy()
        task = ("Look at this screenshot of the dashboard and tell me what the "
                "chart image gets wrong")
        features = extract(task)
        assert features.get("needs_vision") is True

        dlog = DecisionLog()
        route(task, policy, blocklist=_NoBans(), decision_log=dlog, now=OFF_PEAK,
              classify_fn=lambda _task, _features: {"tier": "T2",
                                                    "confidence": "high"})
        recorded = dlog.tail(1)[0]

        traced = explain(task, features, False, policy["rules"],
                         policy.get("default", {}), policy["tiers"], when=OFF_PEAK)
        assert traced["matched_rule_id"] == "vision-required"
        assert traced["cause"] == recorded["cause"]

    def test_a_delegating_row_is_labelled_the_way_the_running_path_records_it(self):
        """The one row where the two functions differ, and why they have to.

        `review-request` fixes the ROLE and hands the MODEL choice to Stage 1. The
        rules STEP of the trace labels the hand-off from the rule id (keyword_match
        — the keyword is what got us here), but the DECISION the running path
        records for that route is `classifier`, because the classifier is what
        picked the model. _determine_cause reports the decision, so /explain and
        the log agree on the route; labelling it from the rule id instead would put
        the console at odds with the recorded cause of every review route.
        """
        from router.adapter import route
        from router.decision_log import DecisionLog
        from router.signals import extract

        policy = _live_policy()
        task = "Please review this PR for correctness"
        features = extract(task)

        dlog = DecisionLog()
        route(task, policy, blocklist=_NoBans(), decision_log=dlog, now=OFF_PEAK,
              classify_fn=lambda _task, _features: {"tier": "T2",
                                                    "confidence": "high"})
        recorded = dlog.tail(1)[0]

        traced = explain(task, features, False, policy["rules"],
                         policy.get("default", {}), policy["tiers"], when=OFF_PEAK)
        assert traced["matched_rule_id"] == "review-request"
        assert traced["cause"] == recorded["cause"] == "classifier"

    def test_there_is_exactly_one_rule_id_table(self):
        """Structural: this module consults the adapter's table, never a copy.

        The copy is what drifted, so its absence is asserted rather than assumed,
        and the delegation is asserted by IDENTITY — a future "small local
        shortcut" inside _determine_cause fails here instead of six months later
        on one shipped row.
        """
        from router import adapter

        assert not hasattr(rules_mod, "_RULE_ID_CAUSES")
        assert rules_mod._cause_labeller() is adapter._cause_from_rule

    def test_the_labeller_is_not_bound_at_module_scope(self):
        """The cycle is real: adapter imports this module at ITS module scope.

        So the labeller is fetched at call time. Asserted because the tempting
        cleanup — hoisting that import to the top of rules.py — builds whichever
        of the two modules is imported second against a half-initialised first.
        """
        assert not hasattr(rules_mod, "adapter")
        assert not hasattr(rules_mod, "_cause_from_rule")

    def test_no_labeller_degrades_to_a_closed_set_member(self, monkeypatch):
        """An unreachable adapter must not raise through /explain, or invent a cause.

        Without the labeller the rule id cannot be labelled at all, so the answer
        is `default_fallthrough`, the closed-set member for "no rule-keyed cause".
        Nothing disagrees in that state — the module that records the other half of
        the pair is the module that failed to import. The two OUTPUT-keyed labels
        still answer, because they are decided before the delegation.
        """
        import sys

        monkeypatch.setitem(sys.modules, "router.adapter", None)

        assert rules_mod._cause_labeller() is None
        assert rules_mod._determine_cause("hard-verbs", {"model": "m"}) == (
            "default_fallthrough"
        )
        assert rules_mod._determine_cause("hard-verbs", {"deny": True}) == (
            "blocklist_veto"
        )
        assert rules_mod._determine_cause("anything", {"action": "classify"}) == (
            "classifier"
        )

    def test_a_numbered_rule_id_is_labelled_not_raised(self, monkeypatch):
        """YAML yields an int for `id: 7`; .lower() on that used to raise.

        service.explain catches ValueError, not AttributeError, so /explain died
        uncaught inside the code whose only job is to explain a route. The
        coercion lives in the labeller now, which is the point: one owner, so the
        two surfaces cannot disagree about a numbered row either.
        """
        from router.adapter import _cause_from_rule

        for rid in (7, None, 1.5, ("t",)):
            assert (
                rules_mod._determine_cause(rid, {"model": "m"})
                == _cause_from_rule(rid, {"model": "m"})
            )


# ---------------------------------------------------------------------------
# Loading shape: the capability layer must be LIVE under the PACKAGE name
# ---------------------------------------------------------------------------
#
# Every other test in this file imports ``router.*`` — the FLAT shape, where
# ``router`` is a top-level package on sys.path. Production is not that shape:
# Hermes loads this plugin as ``hermes_plugins.<slug>.router.rules`` (see the
# ``_LOADED_AS_PACKAGE`` switch in the plugin's ``__init__``). rules.py used to
# resolve its registry by the ABSOLUTE name only, so under the production shape
# both defensive imports raised ImportError, ``_caps``/``_signals`` fell to None,
# and plan_chain degraded to ``_unfiltered_plan``: the declared chain, with no
# capability filter, no time_cap, no time_policy and no cheapest_now — and lint
# quietly stopped validating ``when`` field names at the one gate that is
# supposed to be fail-closed. A green suite could not see it, because the suite
# only ever loaded the other shape.
#
# The guard runs the import in a CHILD interpreter whose sys.path cannot reach
# this repo, so the absolute name is genuinely unresolvable — the production
# condition itself, not a simulation of it. In-process this would prove nothing:
# ``router.capabilities`` and ``router.signals`` are already in this
# interpreter's sys.modules from the imports at the top of this file, so the
# absolute fallback would find the cached top-level copies and
# ``_caps is not None`` would hold with the defect fully restored.
#
# What it asserts is the AGREEMENT, not one side: the plan the production shape
# computes must EQUAL the plan this file's flat-shape import computes, for the
# same tiers, features and clock. Asserting only that the child filters would
# still pass if both shapes were inert in the same way, and asserting only that
# the flat shape filters is precisely the mistake that hid this for six weeks.

_PLUGIN_SLUG = "delegate_profile"

#: The cases both shapes are asked to plan, defined ONCE and handed to the child
#: as data — a second copy on the child side is a mirror, and a drifted mirror
#: would make the two shapes look like they disagreed when only the fixture did.
_SHAPE_CASES = (
    # A vision requirement must actually REJECT the text-only elos. Under the
    # defect this came back as all three declared hops in declared order.
    ({"model": "T4"}, CAPS_TIERS, _vision_features(), None),
    # The time layer rides the same import: a dollar cap at a peak hour.
    ({"model": "T1"}, TIME_TIERS, _mkf(), PEAK_MONDAY),
)

#: Rule ids whose cause label is decided by the adapter's table, not by the
#: output shape — i.e. the ones that need ``_cause_labeller`` to have resolved.
#: All three are non-default labels, so "both sides say default_fallthrough"
#: cannot pass for agreement.
_SHAPE_CAUSE_IDS = ("vision-required", "huge-context-read", "hard-verbs")

#: Runs in the child. Reports which modules the relative import actually bound,
#: not merely that something was bound. ``__SLUG__`` is substituted rather than
#: str.format()-ed so the dict literals below need no brace doubling.
_SHAPE_PROBE = '''
import json
import sys
from datetime import datetime

payload = json.load(sys.stdin)

from hermes_plugins.__SLUG__.router import rules
from hermes_plugins.__SLUG__.router.adapter import _cause_from_rule

out = {
    "module": rules.__name__,
    "caps": None if rules._caps is None else rules._caps.__name__,
    "signals": None if rules._signals is None else rules._signals.__name__,
    "labeller_reachable": rules._cause_labeller() is not None,
    "plans": [],
}

# Non-vacuity, checked from inside: if the top-level name were reachable here the
# absolute fallback could satisfy every assertion in the parent with the defect
# restored, and this probe would be testing nothing.
try:
    import router  # noqa: F401
except ImportError:
    out["absolute_name_reachable"] = False
else:
    out["absolute_name_reachable"] = True

for case in payload["cases"]:
    when = datetime.fromisoformat(case["when"]) if case["when"] else None
    resolved = rules.resolve_tiers(case["selector"], case["tiers"])
    out["plans"].append(rules.plan_chain(resolved, case["features"], when=when))

# Both halves of the cause pair, read in the SAME interpreter: the surface that
# displays a decision and the path that runs it.
out["causes"] = {
    rid: [
        rules._determine_cause(rid, {"model": "m"}),
        _cause_from_rule(rid, {"model": "m"}),
    ]
    for rid in payload["cause_rule_ids"]
}

json.dump(out, sys.stdout)
'''


def _build_plugin_package(root):
    """Lay out ``hermes_plugins.<slug>.router`` under ``root``.

    COPIED, never symlinked: the child must not be able to reach this repo by any
    path other than the one under test.
    """
    import shutil
    from pathlib import Path

    pkg = root / "hermes_plugins" / _PLUGIN_SLUG
    pkg.mkdir(parents=True)
    shutil.copytree(
        Path(rules_mod.__file__).resolve().parent,
        pkg / "router",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / "hermes_plugins" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    return root


def _probe_package_shape(root, cases, cause_rule_ids):
    """Plan ``cases`` in a child interpreter that can only see the package shape."""
    import os
    import subprocess
    import sys

    _build_plugin_package(root)
    script = root / "shape_probe.py"
    script.write_text(_SHAPE_PROBE.replace("__SLUG__", _PLUGIN_SLUG))

    payload = json.dumps({
        "cases": [
            {
                "selector": selector,
                "tiers": tiers,
                "features": features,
                "when": None if when is None else when.isoformat(),
            }
            for selector, tiers, features, when in cases
        ],
        "cause_rule_ids": list(cause_rule_ids),
    })
    # cwd AND PYTHONPATH are the package root and nothing else. Python prepends
    # cwd to sys.path, so leaving it at the repo root would put `router` back
    # within reach and hand the absolute fallback a way to succeed.
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestCapabilityLayerIsLiveUnderHermesPluginPackageShape:
    """The shape production loads must get the same routing engine tests do."""

    @pytest.fixture(scope="class")
    def probe(self, tmp_path_factory):
        # Class-scoped: one child interpreter answers every case below.
        return _probe_package_shape(
            tmp_path_factory.mktemp("plugin_shape"), _SHAPE_CASES, _SHAPE_CAUSE_IDS
        )

    def test_registry_and_signals_bind_the_sibling_copies(self, probe):
        """``_caps``/``_signals`` are live, and are the modules NEXT TO rules.py.

        Identity, not just non-None: binding some other ``router.capabilities``
        that happened to be importable would be the double-import bug rather than
        this one, and both surfaces would still be describing different state.
        """
        assert probe["module"] == f"hermes_plugins.{_PLUGIN_SLUG}.router.rules"
        assert probe["absolute_name_reachable"] is False
        assert probe["caps"] == f"hermes_plugins.{_PLUGIN_SLUG}.router.capabilities"
        assert probe["signals"] == f"hermes_plugins.{_PLUGIN_SLUG}.router.signals"

    def test_plan_chain_agrees_with_the_flat_shape_case_for_case(self, probe):
        """The two loading shapes must not be able to route differently."""
        assert len(probe["plans"]) == len(_SHAPE_CASES)
        for (selector, tiers, features, when), produced in zip(
            _SHAPE_CASES, probe["plans"]
        ):
            expected = plan_chain(
                resolve_tiers(selector, tiers), features, when=when
            )
            assert produced == expected, selector

    def test_the_package_shape_plan_actually_filtered(self, probe):
        """Agreement alone would also hold if BOTH shapes were inert.

        So this pins the side that has to be non-trivial: the declared chain is
        three hops, a vision requirement leaves exactly one, and the dollar cap
        drops exactly the metered rail that is over it.
        """
        vision_plan, capped_plan = probe["plans"]

        assert [hop["model"] for hop in vision_plan["chain"]] == ["vision-elo"]
        assert vision_plan["requirements"] == {"vision": True}
        assert [r["model"] for r in vision_plan["rejected"]] == [
            "text-only-elo", "another-text-elo",
        ]
        assert {r["reject_reason"] for r in vision_plan["rejected"]} == {"no_vision"}
        assert vision_plan["bypassed"] is False

        assert [c["model"] for c in capped_plan["capped"]] == ["deepseek-v4-pro"]
        assert capped_plan["time_cap"] == {"max_multiplier": 1.5}
        assert capped_plan["time_agnostic"] is False

    def test_the_cause_label_pair_agrees_under_the_package_shape(self, probe):
        """``_cause_labeller`` resolves the adapter, so both halves say the same.

        ``_determine_cause`` is the label the DISPLAY surfaces show and
        ``adapter._cause_from_rule`` is the one the RUNNING path records; the
        delegation exists so there is one answer. Resolving the adapter by the
        absolute name only made "no labeller reachable" the normal state here, so
        every rule-keyed cause displayed as ``default_fallthrough`` while the
        adapter kept recording the real one — the pair silently disagreeing on the
        shape production runs, with the flat-shape tests all green.
        """
        assert probe["labeller_reachable"] is True
        assert set(probe["causes"]) == set(_SHAPE_CAUSE_IDS)
        for rid, (displayed, ran) in probe["causes"].items():
            assert displayed == ran, rid
            # Non-vacuity: agreeing on the no-labeller degrade is not agreement.
            assert displayed != "default_fallthrough", rid


# ---------------------------------------------------------------------------
# Purity — the engine must stay deterministic and IO-free
# ---------------------------------------------------------------------------

def test_rules_module_never_reads_the_wall_clock():
    """Load-bearing: reading the clock here would make every routing test flaky.

    Same AST guard as
    ``test_capabilities.test_capabilities_module_never_reads_the_wall_clock``
    (and test_signals' twin), asserted over the AST rather than the text so the
    module can still DISCUSS ``now()`` while never calling it. The ``datetime``
    import is the TYPE_CHECKING-only annotation import; the relative
    ``from . import capabilities`` (module name is None in the AST) and its
    flat-layout fallback ``from router import capabilities`` are the sibling
    registry, not IO.
    """
    import ast

    tree = ast.parse(inspect.getsource(rules_mod))
    called: set = set()
    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called.add(node.func.id)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert not called & {
        "now", "utcnow", "today", "monotonic", "time", "fromtimestamp", "open",
    }
    # No IO, no state, no network: the import list is the proof. ``adapter`` is
    # the function-local _cause_labeller delegation (sibling module, same
    # relative/absolute pair as the capabilities registry), not a data source.
    assert imported <= {
        "", "__future__", "adapter", "datetime", "inspect", "random", "re",
        "router", "typing",
    }

def test_the_lint_knows_the_injected_role_field():
    """`when.assignee` must not be reported as an unknown signal at the write gate.

    The authority is signals.KNOWN_FEATURE_NAMES, imported and never mirrored.
    """
    from router import rules as _rules
    fields = _rules._known_when_fields()
    assert fields is not None
    assert "assignee" in fields


def test_a_role_scoped_row_matches_only_when_the_role_is_present():
    from router.rules import match
    rows = [{"id": "reviewer-only", "status": "stable",
             "when": {"assignee": {"eq": "reviewer"}},
             "then": {"profile": "reviewer", "model": "T4"}}]
    tiers = {"T4": {"model": "gpt-5.6-terra", "provider": "openai-codex"}}

    _out, rid = match({"has_code": True}, False, rows, {}, tiers)
    assert rid is None

    _out, rid = match({"has_code": True, "assignee": "reviewer"}, False, rows, {}, tiers)
    assert rid == "reviewer-only"
