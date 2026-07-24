"""Tests for DecisionLog — the additive steps= trace param and its shape guarantees."""

from __future__ import annotations

from router.decision_log import DecisionLog, VALID_CAUSES


def test_record_without_steps_keeps_historical_shape():
    log = DecisionLog()
    log.record("hard_rule", {"profile": "coder", "model": "T4"}, matched_rule_id="r1",
               task_preview="x" * 200)
    entry = log.entries()[0]
    assert set(entry) == {"ts", "cause", "output", "rule_id", "task"}
    assert "steps" not in entry  # omitted -> no key, so persisted shape is unchanged
    assert entry["cause"] == "hard_rule"
    assert entry["rule_id"] == "r1"
    assert entry["task"] == "x" * 120  # truncated to 120


def test_record_with_steps_attaches_trace():
    log = DecisionLog()
    steps = [
        {"stage": "blocklist", "in": {"model": "m"}, "out": {"blocked": False}, "cause": None},
        {"stage": "rules", "in": {"features": {}}, "out": {"model": "T4"}, "cause": "hard_rule"},
    ]
    log.record("hard_rule", {"model": "T4"}, steps=steps)
    entry = log.entries()[0]
    assert entry["steps"] == steps
    assert entry["steps"][0]["stage"] == "blocklist"


def test_record_coerces_unknown_cause_to_fail_safe():
    log = DecisionLog()
    log.record("not-a-real-cause", {"model": "x"})
    assert log.entries()[0]["cause"] == "fail_safe_strong"
    assert "fail_safe_strong" in VALID_CAUSES


def test_tail_and_format_line_unaffected_by_steps():
    log = DecisionLog()
    log.record("classifier", {"profile": "coder", "model": "big"}, steps=[{"stage": "x"}])
    line = log.format_line(log.tail(1)[0])
    assert "cause=classifier" in line
    assert "model=big" in line
