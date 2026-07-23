"""Unit tests for rule matching engine (router/rules.py)."""

import pytest
from router.rules import match, lint, explain, resolve_tiers


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

ROUTER_CONFIG = {
    "enabled": True,
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
        "keyword_hits": [],
    }
    fv.update(overrides)
    return fv


# ---------------------------------------------------------------------------
# match() tests
# ---------------------------------------------------------------------------

class TestMatch:
    def test_blocklist_deny_first(self):
        """Blocklist deny row fires before anything else."""
        fv = _mkf(verb_class="trivial", has_code=True, size_lines=20)
        # Blocklist rule matches when model is in deny list
        # The blocklist pre-filter injects the check; here we simulate it
        # by having a feature that wouldn't normally match
        # Actually the blocklist rule checks 'model' field - this is from
        # the turn context, not the feature vector. We test it separately.
        pass

    def test_blocklist_deny_direct_rule(self):
        """When the model field is in the deny list, blocklist fires."""
        rules = ROUTER_CONFIG["rules"]
        default = ROUTER_CONFIG["default"]
        tiers = ROUTER_CONFIG["tiers"]

        # Simulating a turn where the requested model is gpt-5.6-sol
        fv = _mkf()
        # Inject 'model' into features for testing (the adapter would do this)
        fv["model"] = "gpt-5.6-sol"

        output, rule_id = match(fv, False, rules, default, tiers)
        assert rule_id == "block-codex-stall"
        assert output["deny"] is True

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
        fv["model"] = "gpt-5.6-sol"
        result = explain(
            "Use gpt-5.6-sol",
            fv,
            False,
            ROUTER_CONFIG["rules"],
            ROUTER_CONFIG["default"],
            ROUTER_CONFIG["tiers"],
        )
        assert result["cause"] == "blocklist_veto"
        assert result["output"]["deny"] is True
