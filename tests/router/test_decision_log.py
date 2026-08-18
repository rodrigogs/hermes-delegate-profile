"""Tests for DecisionLog — the additive steps=/chain_plan= params, their shape
guarantees, and the attempted head that makes a trace name the elo that RAN."""

from __future__ import annotations

import json

from router.decision_log import (
    CHAIN_PLAN_KEYS,
    DecisionLog,
    VALID_CAUSES,
    attempted_head_of,
    chain_plan_of,
    empty_chain_plan,
    plan_head_of,
)
from router.durable_decision_log import DurableDecisionLog, read_entries


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


# ---------------------------------------------------------------------------
# The attempted head — the trace must name the elo that RUNS
# ---------------------------------------------------------------------------
#
# `output["model"]` is the DECLARED tier primary. The elo the executor dispatches
# FIRST is the head of the planned chain, and after a capability filter, a time
# cap, a shuffle or a blocklist veto the two differ. The module docstring and
# `attempted_head_of` both described `record()` writing that head, and `record()`
# never wrote it: measured on this tree, a vision decision persisted
# `output.model == 'glm-5.3'` (a model that cannot see and was never attempted)
# with no `attempted_model` key at all, while `chain_plan.chain[0]` was
# `gpt-5.6-luna` — what actually ran.


def _plan(chain, **extra_keys):
    """A minimal chain_plan carrying only what these tests read."""
    plan = {"chain": list(chain), "rejected": []}
    plan.update(extra_keys)
    return plan


def _persisted(entry):
    """The entry as a reader gets it back off routes.jsonl.

    Every assertion below goes through this: the defect was in what reaches the
    FILE, so asserting on the in-memory dict alone would not have caught a head
    that cannot be serialised, and asserting that the writer was called would not
    have caught a writer that was never wired up.
    """
    return json.loads(json.dumps(entry))


def test_record_persists_the_planned_head_as_the_attempted_model():
    log = DecisionLog()
    log.record(
        "keyword_match",
        {"profile": "coder", "model": "glm-5.3", "provider": "zai"},
        chain_plan=_plan([{"model": "gpt-5.6-luna", "provider": "openai-codex"}]),
    )
    entry = _persisted(log.entries()[0])

    assert entry["output"]["attempted_model"] == "gpt-5.6-luna"
    assert entry["output"]["attempted_provider"] == "openai-codex"
    # The tier identity is NOT redefined — other consumers read `model` as the tier.
    assert entry["output"]["model"] == "glm-5.3"
    assert attempted_head_of(entry) == ("gpt-5.6-luna", "openai-codex")


def test_the_attempted_head_is_read_back_off_a_file_the_writer_persisted(
    tmp_path, monkeypatch,
):
    """End to end through the durable log: writer -> routes.jsonl -> reader.

    DurableDecisionLog persists whatever the base class appended, so this is the
    only test that proves the head survives the whole path an operator's console
    actually reads.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_ROUTE_TRACE_FILE", raising=False)

    DurableDecisionLog().record(
        "size_rule",
        {"profile": "coder", "model": "glm-5.3", "provider": "zai"},
        chain_plan=_plan([{"model": "deepseek-v4-pro", "provider": "deepseek"},
                          {"model": "glm-5.3", "provider": "zai"}]),
    )

    entries = read_entries()
    assert len(entries) == 1, "nothing was persisted, so nothing was proven"
    assert attempted_head_of(entries[0]) == ("deepseek-v4-pro", "deepseek")
    assert chain_plan_of(entries[0])["chain"][0]["model"] == "deepseek-v4-pro"


def test_the_writer_and_the_reader_agree_on_which_hop_is_first():
    """One definition of "head", asserted as an agreement rather than a value.

    plan_head_of is the writer's answer and attempted_head_of is the reader's;
    two implementations of "which hop runs first" is precisely how a trace comes
    to disagree with the executor.
    """
    chains = [
        [{"model": "a", "provider": "pa"}, {"model": "b", "provider": "pb"}],
        [{"model": "solo"}],
        [{"provider": "no-model"}, {"model": "b", "provider": "pb"}],
    ]
    for chain in chains:
        log = DecisionLog()
        log.record("classifier", {"model": "declared", "provider": "pd"},
                   chain_plan=_plan(chain))
        entry = _persisted(log.entries()[0])
        head = plan_head_of(chain_plan_of(entry))
        assert head is not None
        assert attempted_head_of(entry) == head


def test_a_decision_with_no_plan_head_falls_back_to_the_declared_model():
    """The honest answer for a decision with nothing to attempt.

    Every entry written before this feature, plus a blocklist denial, plus a plan
    whose chain is empty. None of them may invent an attempted_model.
    """
    for plan in (None, _plan([]), _plan([{"provider": "no-model"}]),
                 "not a mapping"):
        log = DecisionLog()
        log.record("hard_rule", {"profile": "coder", "model": "gpt-5.5",
                                 "provider": "openai-codex"},
                   chain_plan=plan)
        entry = _persisted(log.entries()[0])
        assert "attempted_model" not in entry["output"], plan
        assert attempted_head_of(entry) == ("gpt-5.5", "openai-codex"), plan


def test_a_headless_provider_is_omitted_rather_than_persisted_as_empty():
    log = DecisionLog()
    log.record("classifier", {"model": "declared"},
               chain_plan=_plan([{"model": "railless"}]))
    entry = _persisted(log.entries()[0])

    assert entry["output"]["attempted_model"] == "railless"
    assert "attempted_provider" not in entry["output"]
    assert attempted_head_of(entry) == ("railless", "")


def test_recording_does_not_mutate_the_callers_decision():
    """The decision dict is the caller's return value; logging must not edit it."""
    decision = {"profile": "coder", "model": "glm-5.3", "provider": "zai"}
    DecisionLog().record("keyword_match", decision,
                         chain_plan=_plan([{"model": "gpt-5.6-luna"}]))
    assert decision == {"profile": "coder", "model": "glm-5.3", "provider": "zai"}


def test_attempted_head_of_never_raises_on_a_malformed_entry():
    for entry in (None, "", [], {}, {"output": None}, {"output": "x"},
                  {"output": {}}, {"output": {"attempted_model": None}}):
        assert attempted_head_of(entry) == ("", "")


def test_the_greppable_line_names_the_attempted_head_when_it_differs():
    """A surface that DISPLAYS a decision must not name a model that never ran."""
    log = DecisionLog()
    log.record("keyword_match", {"profile": "coder", "model": "glm-5.3",
                                 "provider": "zai"},
               chain_plan=_plan([{"model": "gpt-5.6-luna",
                                  "provider": "openai-codex"}]))
    line = log.format_line(log.tail(1)[0])

    assert "model=glm-5.3" in line, "the declared tier keeps its historical field"
    assert "attempted=gpt-5.6-luna@openai-codex" in line

    # ...and it stays quiet when the two agree, so its presence is the signal.
    agreed = DecisionLog()
    agreed.record("hard_rule", {"profile": "coder", "model": "gpt-5.5",
                                "provider": "openai-codex"},
                  chain_plan=_plan([{"model": "gpt-5.5",
                                     "provider": "openai-codex"}]))
    assert "attempted=" not in agreed.format_line(agreed.tail(1)[0])


# ---------------------------------------------------------------------------
# The blocklist veto's plan keys survive persistence
# ---------------------------------------------------------------------------

def test_the_vetos_diagnostics_are_read_back_and_not_silently_dropped():
    """A key this module does not whitelist is invisible on replay.

    That is worse than a missing key because it looks like data — the phase-2
    defect the read-back table exists to prevent — so the veto's three keys are
    asserted through a real persist/read round trip.
    """
    log = DecisionLog()
    log.record("keyword_match", {"model": "glm-5.3", "provider": "zai"},
               chain_plan=_plan(
                   [{"model": "glm-5.3", "provider": "zai"}],
                   blocked=[{"model": "gpt-5.6-luna", "provider": "openai-codex",
                             "reject_reason": "blocked"}],
                   blocklist_widened=True,
                   blocklist_bypassed=False,
               ))
    plan = chain_plan_of(_persisted(log.entries()[0]))

    assert plan["blocked"] == [{"model": "gpt-5.6-luna",
                                "provider": "openai-codex",
                                "reject_reason": "blocked"}]
    assert plan["blocklist_widened"] is True
    assert plan["blocklist_bypassed"] is False
    assert set(CHAIN_PLAN_KEYS) >= {"blocked", "blocklist_widened",
                                    "blocklist_bypassed"}


def test_the_vetos_keys_stay_ABSENT_when_the_veto_did_nothing():
    """Absent, not defaulted: the veto is a no-op on nearly every turn.

    Defaulting them would rewrite the shape of every historical entry and every
    clean trace, which is exactly why the clock keys are absent too.
    """
    log = DecisionLog()
    log.record("hard_rule", {"model": "gpt-5.5"},
               chain_plan=_plan([{"model": "gpt-5.5"}]))
    plan = chain_plan_of(_persisted(log.entries()[0]))

    default = empty_chain_plan()
    for key in ("blocked", "blocklist_widened", "blocklist_bypassed"):
        assert key not in plan, key
        assert key not in default, key
    # A corrupt value is skipped, never coerced.
    corrupt = chain_plan_of({"chain_plan": {"blocked": "nope",
                                            "blocklist_widened": "yes"}})
    assert "blocked" not in corrupt and "blocklist_widened" not in corrupt
