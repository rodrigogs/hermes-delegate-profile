"""Edge-case contracts for router modules.

Tests here target error and fallback behaviour that is easy to miss in normal
routing flows. Each test is hermetic: no real Hermes state is read or written.
"""

from __future__ import annotations

import argparse
import json

import pytest

from router import cli
from router.adapter import _apply_session_floor, _cause_from_rule, _resolve_output, route
from router.blocklist import Blocklist
from router.cache import Cache, SessionPin
from router.decision_log import DecisionLog
from router.rules import _all_clauses_match, _determine_cause, _eval_clause, _is_shadowed, _matching_clauses, lint


def _config(**overrides):
    config = {
        "enabled": True,
        "fail_safe": {"profile": "coder", "model": "safe", "provider": "p"},
        "blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {"enabled": False}},
        "rules": [
            {
                "id": "trivial-code",
                "when": {"verb_class": {"eq": "trivial"}, "has_code": {"eq": True}},
                "then": {"profile": "coder", "model": "T1"},
            }
        ],
        "default": {"action": "classify"},
        "tiers": {
            "T1": {"model": "small", "provider": "p"},
            "T2": {"model": "medium", "provider": "p"},
            "T3": {"model": "large", "provider": "p"},
            "T4": {"model": "largest", "provider": "p"},
        },
    }
    config.update(overrides)
    return config


def test_adapter_blocklist_without_fallback_and_fallback_fallback():
    config = _config(
        blocklist={
            "manual_ban": [{"model": "blocked", "provider": "p"}],
            "fallback_chain": [],
            "auto_breaker": {"enabled": False},
        }
    )
    result = route("task", config, requested_model="blocked", requested_provider="p")
    assert result == {"deny": True}

    fallback_config = _config(
        blocklist={
            "manual_ban": [{"model": "blocked", "provider": "p"}],
            "fallback_chain": ["blocked", "next"],
            "auto_breaker": {"enabled": False},
        }
    )
    result = route("task", fallback_config, requested_model="blocked", requested_provider="p")
    assert result == {"deny": True, "fallback_model": "next"}


def test_adapter_session_pin_prevents_downgrade():
    config = _config()
    pin = SessionPin()
    pin.set("T4")
    log = DecisionLog()
    result = route("Rename a symbol in code", config, session_pin=pin, decision_log=log)
    assert result["model"] == "largest"
    assert log.tail(1)[0]["cause"] == "session_pin"


def test_adapter_session_pin_prevents_classifier_and_cache_downgrade():
    config = _config(rules=[])
    pin = SessionPin()
    cache = Cache()
    tiers = iter(["T4", "T1"])

    def classify(_task, _features):
        return {"tier": next(tiers), "confidence": "high"}

    first = route("first task", config, classify_fn=classify, session_pin=pin, cache=cache)
    assert first["model"] == "largest"
    assert pin.tier == "T4"

    second = route("second task", config, classify_fn=classify, session_pin=pin, cache=cache)
    assert second["model"] == "largest"

    cache.set("cached task", {"tier": "T1", "model": "small", "provider": "p"})
    cached = route("cached task", config, session_pin=pin, cache=cache)
    assert cached["model"] == "largest"

    unknown, pin_applied = _apply_session_floor({"model": "external-model"}, pin, config["tiers"])
    assert unknown == {"model": "external-model"}
    assert pin_applied is False
    unknown_tier, pin_applied = _apply_session_floor(
        {"model": "external-model"}, pin, config["tiers"], output_tier="T5",
    )
    assert unknown_tier == {"model": "external-model"}
    assert pin_applied is False


def test_adapter_classifier_failure_and_session_floor_edges():
    config = _config(rules=[])

    def classifier_failure(_task, _features):
        raise RuntimeError("provider unavailable")

    assert route("task", config, classify_fn=classifier_failure) == {
        "profile": "coder", "model": "safe", "provider": "p",
    }

    pin = SessionPin()
    pin.set("T2")
    no_provider_tiers = {
        "T1": {"model": "small"},
        "T2": {"model": "medium"},
        "T3": {"model": "large"},
        "T4": {"model": "largest"},
    }
    raised, applied = _apply_session_floor({"model": "small"}, pin, no_provider_tiers)
    assert raised == {"model": "medium"}
    assert applied is True
    unchanged, applied = _apply_session_floor({"model": "medium"}, pin, no_provider_tiers)
    assert unchanged == {"model": "medium"}
    assert applied is False

    direct_log = DecisionLog()
    direct_pin = SessionPin()
    direct_pin.set("T1")
    direct = route("Rename a symbol in code", _config(), session_pin=direct_pin, decision_log=direct_log)
    assert direct["model"] == "small"
    assert direct_log.tail(1)[0]["cause"] == "has_code_rule"

    profile_config = _config(rules=[{
        "id": "review", "when": {"keywords": {"contains": "review"}},
        "then": {"profile": "reviewer", "action": "classify"},
    }])
    classified = route(
        "review this task", profile_config,
        classify_fn=lambda _task, _features: {"tier": "T2", "confidence": "high"},
    )
    assert classified["profile"] == "reviewer"

    assert route("task", _config(default={"action": "unsupported"})) == {
        "profile": "coder", "model": "safe", "provider": "p",
    }
    assert _cause_from_rule("rule", {"deny": True}) == "blocklist_veto"


def test_adapter_bottom_failsafe_and_output_helpers():
    result = route("unclassified", _config(default={}))
    assert result == {"profile": "coder", "model": "safe", "provider": "p"}
    assert _cause_from_rule("review-task", {}) == "keyword_match"
    assert _cause_from_rule("size-threshold", {}) == "size_rule"
    assert _cause_from_rule("other", {}) == "default_fallthrough"
    assert _resolve_output({"model": "m", "provider": "p"}, {"profile": "reviewer", "action": "classify"}, {}) == {
        "profile": "reviewer", "model": "m", "provider": "p"
    }
    assert _resolve_output({}, {}, {}) == {"profile": "coder"}


def test_adapter_cache_hit_preserves_rule_profile():
    config = _config(
        rules=[{
            "id": "review", "when": {"keywords": {"contains": "review"}},
            "then": {"profile": "reviewer", "action": "classify"},
        }]
    )
    cache = Cache()
    cache.set("review task", {"model": "cached", "provider": "p"})
    result = route("review task", config, cache=cache)
    assert result == {"profile": "reviewer", "model": "cached", "provider": "p"}


def test_blocklist_handles_malformed_config_and_state(monkeypatch, tmp_path, caplog):
    import router.blocklist as blocklist_mod

    monkeypatch.setattr(blocklist_mod, "_state_path", lambda: tmp_path / "breaker.json")
    (tmp_path / "breaker.json").write_text("not json", encoding="utf-8")
    bl = Blocklist({"blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": "bad"}})
    assert bl.breaker_enabled() is False

    enabled = Blocklist({"blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {"enabled": True}}})
    assert enabled.breaker_state_dict() == {"version": 1, "entries": {}}
    assert "corrupt" in caplog.text


def test_blocklist_save_failure_is_nonfatal(monkeypatch, tmp_path, caplog):
    import router.blocklist as blocklist_mod

    monkeypatch.setattr(blocklist_mod, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(blocklist_mod.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    config = {"blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {
        "enabled": True, "threshold": 1, "window_seconds": 60,
        "base_cooldown_seconds": 1, "max_cooldown_seconds": 1, "backoff_multiplier": 2,
    }}}
    bl = Blocklist(config)
    assert bl.record_failure("m", "p", "ttfb_stall") is True
    assert "Failed to save" in caplog.text


def test_blocklist_disabled_success_and_match_semantics():
    bl = Blocklist({"blocklist": {"manual_ban": [{"model": "M"}], "fallback_chain": [], "auto_breaker": {"enabled": False}}})
    bl.record_success("M", "p")
    assert bl.is_blocked("m", "other")
    assert Blocklist._match("m", "p", "m", "")
    assert not Blocklist._match("m", "p", "different", "p")


@pytest.mark.parametrize(
    ("op", "actual", "target", "expected"),
    [
        ("ne", 1, 2, True), ("in", "a", ["a"], True), ("in", "a", "a", True),
        ("nin", "a", ["b"], True), ("nin", "a", "b", True),
        ("gt", 2, 1, True), ("gte", 2, 2, True), ("lt", 1, 2, True), ("lte", 2, 2, True),
        ("contains", ["Alpha"], "alpha", True), ("contains", "Alpha", "alpha", True),
        ("starts_with", "Alpha", "al", True), ("ends_with", "Alpha", "HA", True),
        ("matches", "hard", "^h", True), ("unknown", "x", "x", False), ("gt", "bad", 1, False),
    ],
)
def test_all_rule_operators(op, actual, target, expected):
    assert _eval_clause(op, actual, target) is expected


def test_rule_helper_edges_and_lint_errors():
    assert not _all_clauses_match({}, {}, False)
    assert not _all_clauses_match({"missing": {"eq": 1}}, {}, False)
    assert _all_clauses_match({"blocked_model": {"eq": True}}, {}, True)
    assert _matching_clauses({"x": {"eq": 1}, "missing": {"eq": 2}}, {"x": 1}) == {"x": {"eq": 1}}
    assert _is_shadowed({"x": {"eq": 1}}, {"x": {"eq": 2}})
    assert not _is_shadowed({}, {"x": {"eq": 1}})
    errors = lint({
        "default": {}, "tiers": {"T1": {}},
        "rules": [
            {"id": "x", "when": {"foo": "bad", "verb_class": {"wat": 1}, "x": {"matches": "x"}}, "then": {"wat": 1, "deny": "yes", "model": "T9"}},
            {"id": "x", "when": {}, "then": {}},
        ],
    })
    assert len(errors) >= 8


def test_cli_missing_config_log_and_main(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.load_config(str(tmp_path / "missing.yaml"))

    log = tmp_path / "router.log"
    log.write_text("a\nb\nc\n", encoding="utf-8")
    cli.cmd_log(argparse.Namespace(tail=2, file=str(log), follow=True))
    assert "b\nc" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.cmd_log(argparse.Namespace(tail=1, file=str(tmp_path / "missing.log"), follow=False))

    called = []
    monkeypatch.setattr(cli, "build_parser", lambda: _Parser(called))
    cli.main(["lint"])
    assert called == ["called"]


def test_blocklist_loads_valid_state_and_survives_read_cleanup_errors(monkeypatch, tmp_path, caplog):
    import router.blocklist as blocklist_mod
    from router.breaker import BreakerState

    path = tmp_path / "state.json"
    monkeypatch.setattr(blocklist_mod, "_state_path", lambda: path)
    state = BreakerState({"threshold": 1})
    state.record("m@p", "ttfb_stall", 1.0)
    path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    bl = Blocklist({"blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {"enabled": True}}})
    assert bl.breaker_state_dict()["entries"]

    monkeypatch.setattr(blocklist_mod.Path, "read_text", lambda _self, **_kwargs: (_ for _ in ()).throw(OSError("read")))
    Blocklist({"blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {"enabled": True}}})
    assert "Failed to load" in caplog.text


def test_cli_blocklist_all_output_branches_and_no_log_file(monkeypatch, capsys):
    class FakeBlocklist:
        def __init__(self, _config):
            pass

        def manual_bans(self):
            return [{"model": "m", "provider": "p", "reason": "r"}]

        def breaker_enabled(self):
            return True

        def breaker_status(self):
            return [
                {"model_key": "long", "state": "OPEN", "cooldown_remaining_s": 121, "backoff_seconds": 60, "last_failure_kind": "x"},
                {"model_key": "short", "state": "OPEN", "cooldown_remaining_s": 2, "backoff_seconds": 1, "last_failure_kind": "x"},
                {"model_key": "now", "state": "OPEN", "cooldown_remaining_s": 0, "backoff_seconds": 1, "last_failure_kind": "x"},
            ]

        def fallback_chain(self):
            return ["m", "next"]

    monkeypatch.setattr(cli, "Blocklist", FakeBlocklist)
    monkeypatch.setattr(cli, "load_config", lambda _path: {})
    cli.cmd_blocklist(argparse.Namespace(config="ignored"))
    out = capsys.readouterr().out
    assert "2m remaining" in out and "2s remaining" in out and "expiring now" in out
    cli.cmd_log(argparse.Namespace(tail=1, file=None, follow=False))
    assert "no log file" in capsys.readouterr().out


class _Parser:
    def __init__(self, called):
        self.called = called

    def parse_args(self, _argv):
        return argparse.Namespace(func=lambda _args: self.called.append("called"))


@pytest.mark.parametrize(
    "config",
    [
        "not-a-mapping",
        {"default": {}, "tiers": {"T1": {}, "T2": {}, "T3": {}, "T4": {}}, "rules": "not-a-list"},
        {"default": {}, "tiers": {"T1": {}, "T2": {}, "T3": {}, "T4": {}}, "rules": [None]},
        {"default": {}, "tiers": None, "rules": [{"id": "rule", "when": {"x": {"eq": 1}}, "then": {"model": "T1"}}]},
        {
            "default": {}, "tiers": {"T1": {}, "T2": {}, "T3": {}, "T4": {}},
            "rules": [
                {"id": "broken", "when": "not-a-mapping", "then": {"model": "T1"}},
                {"id": "valid", "when": {"x": {"eq": 1}}, "then": {"model": "T1"}},
            ],
        },
    ],
)
def test_lint_rejects_malformed_yaml_topology_without_raising(config):
    errors = lint(config)
    assert errors


def test_rules_remaining_pure_branches():
    from router.rules import explain, match, resolve_tiers

    tiers = {"T1": {"model": "m"}, "T2": {"model": "n", "provider": "p"}}
    output, rule_id = match(
        {"x": 1}, False,
        [
            {"id": "empty", "when": {"x": {"eq": 1}}, "then": {}},
            {"id": "concrete", "when": {"x": {"eq": 1}}, "then": {"model": "T2"}},
        ],
        {"model": "T1"}, tiers,
    )
    assert (output, rule_id) == ({"model": "n", "provider": "p"}, "concrete")
    assert resolve_tiers({"model": "T1"}, tiers) == {"model": "m"}
    assert resolve_tiers({"model": "literal"}, tiers) == {"model": "literal"}
    assert _matching_clauses({"x": {"eq": 2}}, {"x": 1}) == {}
    assert _is_shadowed({"x": {"eq": 1}}, {"x": {"eq": 2}, "y": {"eq": 3}}) is False

    traced = explain("task", {"x": 1}, False, [{"id": "classifier", "when": {"x": {"eq": 1}}, "then": {"action": "classify"}}], {}, tiers)
    assert traced["cause"] == "classifier"
    assert _determine_cause("keyword-search", {}) == "keyword_match"
    assert _determine_cause("size-limit", {}) == "size_rule"
    assert _determine_cause("misc", {}) == "classifier"

    errors = lint({
        "default": {}, "tiers": {"T1": {}, "T2": {}, "T3": {}, "T4": {}},
        "rules": [
            {"when": {"x": {"eq": 1}}, "then": {"model": "literal"}},
            {"id": "missing-then", "when": {"x": {"eq": 1}}},
        ],
    })
    assert any("missing 'id'" in error for error in errors)
    assert any("missing or invalid 'then'" in error for error in errors)
