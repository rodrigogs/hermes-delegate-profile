"""Edge-case contracts for router modules.

Tests here target error and fallback behaviour that is easy to miss in normal
routing flows. Each test is hermetic: no real Hermes state is read or written.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from router import cli, rules
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

    unsupported_rule = _config(rules=[{
        "id": "unsupported", "when": {"verb_class": {"eq": "trivial"}},
        "then": {"action": "unsupported"},
    }])
    assert route("Rename a symbol", unsupported_rule) == {
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
    monkeypatch.setattr(blocklist_mod.os, "unlink", lambda *_args: (_ for _ in ()).throw(OSError("cleanup")))
    config = {"blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {
        "enabled": True, "threshold": 1, "window_seconds": 60,
        "base_cooldown_seconds": 1, "max_cooldown_seconds": 1, "backoff_multiplier": 2,
    }}}
    bl = Blocklist(config)
    assert bl.record_failure("m", "p", "ttfb_stall") is True
    assert "Failed to save" in caplog.text


def test_blocklist_concurrent_record_failure_does_not_lose_events(tmp_path, monkeypatch):
    """Regression: concurrent record_failure calls must not drop failure events.

    ``_record_breaker_outcome`` constructs a FRESH Blocklist per delegate_profile
    call. Without serialization across the load -> mutate -> save critical
    section, N writers each load the same on-disk state, mutate their private
    copy, and clobber each other on the atomic rename — so the breaker never
    accumulates enough weight to trip even though every failure was "recorded".
    This test fails (state stays CLOSED, events < N) without the process-wide
    state lock.
    """
    import threading

    import router.blocklist as blocklist_mod

    # Each test gets a unique state path in a fresh tmp_path; flush any stale
    # lock entry inherited from a previous run so the lock matches this path.
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(blocklist_mod, "_state_path", lambda: state_file)

    n_threads = 8
    failure_kind = "idle_stall"  # weight 2; 8 threads -> weight 16, well over threshold 5
    config = {"blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {
        "enabled": True, "threshold": 5, "window_seconds": 600,
        "base_cooldown_seconds": 60, "max_cooldown_seconds": 900, "backoff_multiplier": 2.0,
    }}}
    barrier = threading.Barrier(n_threads)

    def fire():
        barrier.wait()  # maximise simultaneity
        bl = Blocklist(config)          # fresh instance, like _record_breaker_outcome
        bl.record_failure("race-model", "race-prov", failure_kind)

    threads = [threading.Thread(target=fire) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = Blocklist(config)
    entries = final.breaker_state_dict().get("entries", {})
    entry = entries.get("race-model@race-prov", {})
    events = entry.get("failure_events", [])
    state = entry.get("state", "CLOSED")
    persisted_weight = sum(e.get("weight", 0) for e in events)

    assert len(events) == n_threads, (
        f"lost-update race: only {len(events)}/{n_threads} failure events persisted"
    )
    assert persisted_weight == n_threads * 2
    assert state == "OPEN", (
        f"breaker failed to trip under concurrency: state={state}, weight={persisted_weight}"
    )


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
    assert not _all_clauses_match({"blocked_model": {"eq": True}}, {}, False)
    assert _matching_clauses({"x": {"eq": 1}, "missing": {"eq": 2}}, {"x": 1}) == {"x": {"eq": 1}}
    # NOT shadowed, and that is the correct answer. `x eq 1` and `x eq 2` are
    # CONTRADICTORY: no feature vector matches both, so the later row is genuinely
    # reachable and must be allowed to ship. The old assertion expected True and
    # came from the key-set-only shadow check, which treated "same field names"
    # as "same condition"; _is_shadowed now decides containment per operator
    # family. Do NOT invert this back: lint() is the fail-closed write gate, so a
    # false shadow here refuses a legitimate config and strands the operator
    # outside the guarded path.
    assert _is_shadowed({"x": {"eq": 1}}, {"x": {"eq": 2}}) is False
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


def test_breaker_malformed_state_entries_are_ignored():
    from router.breaker import BreakerState, _Entry, _Event

    config = {"threshold": 1}
    assert BreakerState.from_dict({"version": 1, "entries": []}, config).to_dict() == {
        "version": 1, "entries": {},
    }
    assert _Event.from_dict({"kind": "x", "ts": "not-a-number"}) is None
    entry = _Entry.from_dict({"state": "unknown", "failure_events": [{"kind": "", "ts": 1}]})
    assert entry is not None
    assert entry.state == "CLOSED"
    assert entry.events == []

    breaker = BreakerState(config)
    breaker.record("m@p", "ttfb_stall", 1)
    breaker.record("m@p", "ttfb_stall", 2)
    assert breaker.is_blocked("m@p", 2)
    assert breaker.record("m@p", "crash", 3) is False
    breaker._entries["m@p"].state = "HALF_OPEN"
    breaker._entries["m@p"].probe_allowed = True
    assert breaker.is_blocked("m@p", 3) is False
    breaker.record_success("m@p", 3)
    assert breaker.blocked_entries(3) == []
    restored = BreakerState.from_dict({"version": 1, "entries": {"bad": None}}, config)
    assert restored.to_dict() == {"version": 1, "entries": {}}


def test_cli_blocklist_enabled_without_cooldowns(monkeypatch, capsys):
    class EmptyBlocklist:
        def __init__(self, _config):
            pass

        def manual_bans(self):
            return []

        def breaker_enabled(self):
            return True

        def breaker_status(self):
            return []

        def fallback_chain(self):
            return []

    monkeypatch.setattr(cli, "Blocklist", EmptyBlocklist)
    monkeypatch.setattr(cli, "load_config", lambda _path: {})
    cli.cmd_blocklist(argparse.Namespace(config="ignored"))
    assert "no active cooldowns" in capsys.readouterr().out


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
        {"default": {}, "tiers": {"T1": {}, "T2": {}, "T3": {}, "T4": {}}, "rules": [
            None,
            {"id": "valid", "when": {"x": {"eq": 1}}, "then": {"model": "T1"}},
        ]},
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
    # An id in no cause row is default_fallthrough, not classifier. The two
    # producers of this label — rules._determine_cause and
    # adapter._cause_from_rule — now read ONE table, and for four of the eight
    # shipped rules they used to disagree: the surface that DISPLAYS a decision
    # called it "classifier" while the path that RUNS it called it
    # keyword_match or size_rule. For an unmatched id, "it fell through to the
    # default" is what the running path always reported, and it is honest in a
    # way that claiming a classifier decided is not. Do not revert: a green
    # assertion here was the drift.
    assert _determine_cause("misc", {}) == "default_fallthrough"

    errors = lint({
        "default": {}, "tiers": {"T1": {}, "T2": {}, "T3": {}, "T4": {}},
        "rules": [
            {"when": {"x": {"eq": 1}}, "then": {"model": "literal"}},
            {"id": "missing-then", "when": {"x": {"eq": 1}}},
        ],
    })
    assert any("missing 'id'" in error for error in errors)
    assert any("missing or invalid 'then'" in error for error in errors)
    # Happy path: a fully valid config lints CLEAN. The branch under test is
    # 'then.model' naming a LITERAL model rather than a tier alias — the
    # dangling-tier-alias check must stay silent for it. Two inputs had to be
    # updated to keep exercising that branch: every tier now has to declare its
    # own model+provider (a bare `{}` tier is genuinely invalid), and 'when'
    # field names are checked against the known signal set (the old invented
    # 'x' field was a dead row lint is now right to reject).
    assert lint({
        "default": {},
        "tiers": {
            "T1": {"model": "small", "provider": "p"},
            "T2": {"model": "medium", "provider": "p"},
            "T3": {"model": "large", "provider": "p"},
            "T4": {"model": "largest", "provider": "p"},
        },
        "rules": [{
            "id": "literal",
            "when": {"has_code": {"eq": True}},
            "then": {"model": "literal-model"},
        }],
    }) == []
    assert _is_shadowed({"x": {"eq": 1}}, {"x": {"eq": 1}, "y": {"eq": 2}})
    assert not _is_shadowed(
        {"x": {"eq": 1}, "y": {"eq": 1}},
        {"x": {"eq": 1}, "y": {"eq": 2}, "z": {"eq": 3}},
    )


def test_explain_handles_rule_id_missing_from_rows(monkeypatch):
    import router.rules as rules_mod

    monkeypatch.setattr(rules_mod, "match", lambda *_args: ({"model": "m"}, "orphan"))
    result = rules_mod.explain("task", {}, False, [], {}, {})
    assert result["matched_rule_id"] == "orphan"
    # "orphan" is in no cause row, so the unified table reports the fall-through
    # rather than inventing a classifier that never saw this decision. The cause
    # set stays CLOSED for a reason: decision_log.record() coerces an unknown
    # cause to fail_safe_strong, so a new string here would relabel healthy
    # routes as fail-safe.
    assert result["cause"] == "default_fallthrough"


# ---------------------------------------------------------------------------
# A malformed blocklist must not unenforce every ban in it
# ---------------------------------------------------------------------------

class TestAMalformedBlocklistFailsClosed:
    """The blocklist is the component whose whole job is to refuse.

    `config.get("blocklist", {}).get(...)` raised AttributeError for
    ``blocklist:`` with nothing under it, ``blocklist: off``, or a list. Measured
    on the shipped policy with ``blocklist: off`` appended: ``rules.lint``
    returned ``[]``, so ``/status`` said ``valid: True`` AND the write gate
    accepted it — the operator's own console would have PERSISTED it — after which
    ``adapter.route`` raised, ``_route_task`` swallowed it at DEBUG, every
    delegation answered ``bad_args``, and EVERY MANUAL BAN WAS UNENFORCED.

    Both halves are asserted here: the gate now refuses the shape, and a file
    already on disk still routes.
    """

    @staticmethod
    def _shipped():
        import yaml
        return yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "router.example.yaml")
            .read_text(encoding="utf-8")
        )

    @pytest.mark.parametrize("bad", ["off", [], ["glm-5.3"], 7, 1.5, True])
    def test_a_non_mapping_blocklist_is_refused_by_the_write_gate(self, bad):
        cfg = dict(self._shipped())
        cfg["blocklist"] = bad
        errors = [e for e in rules.lint(cfg) if "blocklist" in e]
        assert errors, f"the write gate accepted blocklist={bad!r}"
        assert "blocks nothing" in errors[0], errors

    @pytest.mark.parametrize("bad", ["off", [], 7, None])
    def test_routing_survives_a_malformed_blocklist_already_on_disk(
        self, bad, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg = dict(self._shipped())
        cfg["blocklist"] = bad
        # Constructing it is what used to raise, and route() calls it.
        assert Blocklist(cfg).breaker_enabled() is False
        out = route("rename a variable in utils.py", cfg)
        assert out.get("model"), out

    @pytest.mark.parametrize("key,bad", [
        ("manual_ban", "nope"), ("manual_ban", 7),
        ("fallback_chain", "nope"), ("fallback_chain", {}),
        ("auto_breaker", 7), ("auto_breaker", "on"),
    ])
    def test_a_malformed_section_list_is_refused_by_name(self, key, bad):
        cfg = dict(self._shipped())
        cfg["blocklist"] = {**cfg["blocklist"], key: bad}
        assert any(
            f"blocklist.{key}" in e for e in rules.lint(cfg)
        ), rules.lint(cfg)

    def test_a_malformed_ban_ROW_warns_and_the_other_rows_still_fire(
        self, tmp_path, monkeypatch,
    ):
        """Per-row, per-row only: the rest of the list must keep working.

        The whole list used to be unenforced by one bad row, which is the worst
        possible reading of a safety list. A row shape is a WARNING, not an error:
        a row naming no model is a documented shippable input (it bans every
        model), pinned by test_plugin_status_drops_a_ban_row_it_cannot_name.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        cfg = dict(self._shipped())
        cfg["blocklist"] = {
            "manual_ban": [
                "glm-5.3-flash",                       # a bare string
                {"model": 7},                          # non-string model
                {"model": "gpt-5.6-luna", "provider": "openai-codex"},  # valid
            ],
            "fallback_chain": cfg["blocklist"]["fallback_chain"],
            "auto_breaker": {"enabled": False},
        }

        assert [e for e in rules.lint(cfg) if "blocklist" in e] == [], (
            "a row shape must not block the write"
        )
        warnings = [w for w in rules.lint_warnings(cfg) if "manual_ban" in w]
        assert len(warnings) == 2, warnings
        assert "bans nothing" in warnings[0]

        bl = Blocklist(cfg)
        # The well-formed row still fires...
        assert bl.is_blocked("gpt-5.6-luna", "openai-codex") is True
        assert bl.would_block("gpt-5.6-luna", "openai-codex") is True
        # ...and the unusable rows ban nothing rather than everything.
        assert bl.is_blocked("glm-5.3-flash", "zai") is False

    def test_a_non_string_entry_in_the_fallback_chain_is_dropped(self):
        cfg = dict(self._shipped())
        cfg["blocklist"] = {
            "manual_ban": [], "auto_breaker": {"enabled": False},
            "fallback_chain": ["a", 7, None, "b"],
        }
        assert Blocklist(cfg).fallback_chain() == ["a", "b"]

    def test_the_read_surface_degrades_instead_of_dropping_the_connection(
        self, tmp_path, monkeypatch,
    ):
        """``SidecarApp.dispatch`` has no catch-all, so a raise here killed the socket."""
        import yaml as _yaml

        from router.service import RouterService
        import router.service as service_mod

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        path = tmp_path / "router.yaml"
        path.write_text(_yaml.safe_dump(self._shipped()), encoding="utf-8")

        class _Exploding:
            def __init__(self, config):
                raise RuntimeError("state file is a directory")

        monkeypatch.setattr(service_mod, "Blocklist", _Exploding)
        answer = RouterService(path).blocklist()
        assert answer["manual_bans"] == []
        assert answer["breaker_enabled"] is False
        assert "state file is a directory" in answer["error"]
