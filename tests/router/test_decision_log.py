"""Tests for DecisionLog — the additive steps=/chain_plan= params, their shape
guarantees, and the attempted head that makes a trace name the elo that RAN."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import router.decision_log as decision_log_mod
import router.rules as rules_mod
from router.decision_log import (
    CHAIN_PLAN_KEYS,
    DecisionLog,
    VALID_CAUSES,
    attempted_head_of,
    attempts_of,
    chain_plan_of,
    empty_chain_plan,
    plan_head_of,
)
from router.durable_decision_log import DurableDecisionLog, read_entries


def test_attempts_of_distinguishes_old_trace_from_instrumented_attempts():
    assert attempts_of({"output": {"model": "head"}}) is None
    attempts = attempts_of({
        "attempts": [
            {"n": 2, "model": "served", "provider": "p2", "started_at": 2.5,
             "duration_ms": 500, "outcome": "served"},
            {"n": 1, "model": "failed", "provider": "p1", "started_at": 1,
             "duration_ms": 250, "outcome": "failed",
             "error": {"code": "rate_limit", "message": "quota"}},
            {"n": 3, "model": "bad", "provider": "p3", "started_at": 3,
             "duration_ms": 1, "outcome": "failed"},
        ]
    })
    assert attempts == [
        {"n": 1, "model": "failed", "provider": "p1", "started_at": 1.0,
         "duration_ms": 250, "outcome": "failed",
         "error": {"code": "rate_limit", "message": "quota"}},
        {"n": 2, "model": "served", "provider": "p2", "started_at": 2.5,
         "duration_ms": 500, "outcome": "served"},
    ]


def test_attempts_of_rejects_corrupt_container_rows_and_error_contracts():
    assert attempts_of({"attempts": "old-shape"}) is None
    assert attempts_of({"attempts": [
        None,
        {"n": 0, "model": "bad", "provider": "p", "started_at": 0,
         "duration_ms": 0, "outcome": "skipped"},
        {"n": 1, "model": "served", "provider": "p", "started_at": 0,
         "duration_ms": 0, "outcome": "served", "error": {"code": "x", "message": "wrong"}},
    ]}) == []


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


def test_record_labels_unknown_cause_as_unknown_not_fail_safe():
    """An inventing caller must not be painted as the router's worst outcome."""
    log = DecisionLog()
    log.record("not-a-real-cause", {"model": "x"})
    assert log.entries()[0]["cause"] == "unknown_cause"
    assert "unknown_cause" in VALID_CAUSES
    assert "profile_ignored" in VALID_CAUSES


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


# ---------------------------------------------------------------------------
# A corrupt FIELD costs that field, never the decision
# ---------------------------------------------------------------------------
#
# Both functions below are on the write path of a trace entry, which is never
# worth raising over: a plan is produced by another module and reaches here
# whatever shape that module was in, so each field is bounded and type-checked on
# its own. The property that matters is that one bad field does not take the
# entry — an entry silently reduced to nothing is worse than a missing field,
# because a reader cannot tell it from a decision that had nothing to say.


def test_a_corrupt_rejected_list_costs_that_field_and_not_the_rest_of_the_plan():
    """``rejected`` of the wrong type degrades to empty; the head still lands.

    ``bound_chain_plan`` truncates ``rejected`` because routes.jsonl is
    size-bounded, so it has to have an answer for a value that is not a list at
    all. Dropping the plan there would lose the chain — and with it the
    ``attempted_model`` this whole module exists to record.
    """
    log = DecisionLog()
    log.record(
        "keyword_match",
        {"profile": "coder", "model": "glm-5.3", "provider": "zai"},
        chain_plan={"chain": [{"model": "gpt-5.6-luna", "provider": "openai-codex"}],
                    "rejected": "not-a-list", "strategy": "cheapest_now"},
    )
    entry = _persisted(log.entries()[0])

    assert entry["chain_plan"]["rejected"] == []
    # 0, not absent: consumers never branch on the key's presence.
    assert entry["chain_plan"]["rejected_truncated"] == 0
    # The rest of the plan survived, including the field the executor is judged on.
    assert entry["chain_plan"]["strategy"] == "cheapest_now"
    assert attempted_head_of(entry) == ("gpt-5.6-luna", "openai-codex")
    assert chain_plan_of(entry)["rejected"] == []


def test_a_corrupt_chain_is_not_a_head_on_either_side_of_the_pair():
    """A chain that is not a list yields no head — to the writer AND the reader.

    ``plan_head_of`` answers None rather than ``("", "")`` precisely so the writer
    can tell "no head to record" from "a head with a blank provider", and the
    reader then falls back to the declared tier primary. Asserting the two
    together is the point: a writer that recorded ``attempted_model: ""`` here
    would make the reader answer "" for a decision that really did attempt the
    declared model.
    """
    for broken in ({"chain": "gpt-5.6-luna"}, {"chain": 3}, {"chain": None},
                   {"rejected": []}, "not a mapping", None):
        assert plan_head_of(broken) is None, broken

        log = DecisionLog()
        log.record("hard_rule", {"model": "gpt-5.5", "provider": "openai-codex"},
                   chain_plan=broken)
        entry = _persisted(log.entries()[0])
        assert "attempted_model" not in entry["output"], broken
        assert attempted_head_of(entry) == ("gpt-5.5", "openai-codex"), broken


def test_the_logs_chain_plan_method_and_the_module_accessor_never_disagree():
    """``DecisionLog.chain_plan`` is the same answer as ``chain_plan_of``.

    The method exists so a consumer holding the log object does not have to reach
    for the module function — which means the two must not be able to differ. A
    second reading of "what plan does this entry carry" is the defect this file's
    other half is entirely about, and it would be no better here.
    """
    log = DecisionLog()
    log.record("keyword_match", {"model": "glm-5.3"},
               chain_plan=_plan([{"model": "gpt-5.6-luna", "provider": "openai-codex"}],
                                strategy="random", independent_rails=2))
    log.record("hard_rule", {"model": "gpt-5.5"})  # no plan at all — an old entry

    for entry in log.entries():
        assert log.chain_plan(entry) == chain_plan_of(entry)
    # Non-vacuity: the first entry's plan is real, the second's is the default.
    planned, planless = (log.chain_plan(e) for e in log.entries())
    assert planned["chain"][0]["model"] == "gpt-5.6-luna"
    assert (planned["strategy"], planned["independent_rails"]) == ("random", 2)
    assert planless == empty_chain_plan()
    # And every shape a corrupt trace file can hand either of them is the default,
    # from both, without raising.
    for junk in (None, "", [], 7, {"chain_plan": "nope"}, {"chain_plan": [1]},
                 {"chain_plan": None}):
        assert log.chain_plan(junk) == chain_plan_of(junk) == empty_chain_plan(), junk


# ---------------------------------------------------------------------------
# The PLANNER and this module's read-back are a pair
# ---------------------------------------------------------------------------
#
# ``bound_chain_plan`` persists whatever ``rules.plan_chain`` produced, and
# ``chain_plan_of`` type-checks it back one field at a time. So a key the planner
# emits that the read-back table does not list is an error NOWHERE: it is silently
# dropped, and the console renders a phase-1 plan for a phase-2 decision — which
# is worse than a missing key because it looks like data. That is the defect the
# read-back table exists to prevent, and the only way to assert it is prevented is
# to compare the two SIDES: a plan the shipped planner really produced, at a real
# peak hour, read back through a real persist/read round trip. A literal plan
# written out here would be a third copy of the shape that drifts with neither
# side, and it is exactly what a passing test looked like while the phase-2 keys
# were being dropped.

#: 2026-08-17 is a Monday and 07:00 UTC is peak on zai (weekdays) and deepseek
#: (every day) at once, so a plan taken at this hour carries real multipliers, a
#: real cap verdict and both clock keys instead of the time-agnostic defaults.
_PEAK_MONDAY = datetime(2026, 8, 17, 7, 0, tzinfo=timezone.utc)

#: Tiers declaring the phase-2 knobs over elos the shipped capability registry
#: really prices, in the shapes ``lint`` accepts (asserted below — a fixture the
#: write gate would refuse is a plan production could never be holding):
#:   * T3 — a cap plus an avoid_peak policy. glm-5.3 bills in PLAN credits, so the
#:     1.5x DOLLAR ceiling cannot speak to it even at 2.0x; deepseek-v4-pro is
#:     metered and 2.0x at this hour, so the same cap does catch it; mimo-v2.5-pro
#:     is metered and flat, so something survives and no stage empties the chain.
#:   * T2 — ``random`` with no rng injected, which is the strategy degrade: the one
#:     plan shape that carries a NON-EMPTY ``strategy_degraded_reason``.
_PHASE_TWO_TIERS = {
    "T1": {"model": "glm-5.2-fast", "provider": "zai"},
    "T2": {
        "model": "glm-4.7",
        "provider": "zai",
        "fallback_strategy": "random",
        "pin_primary": False,
        "fallback": [
            {"model": "deepseek-v4-pro", "provider": "deepseek"},
            {"model": "mimo-v2.5-pro", "provider": "xiaomi"},
        ],
    },
    "T3": {
        "model": "glm-5.3",
        "provider": "zai",
        "fallback_strategy": "cheapest_now",
        "time_cap": {"max_multiplier": 1.5},
        "time_policy": {"avoid_peak": ["zai"]},
        "requirements": {"min_context": 128000},
        "fallback": [
            {"model": "deepseek-v4-pro", "provider": "deepseek"},
            {"model": "mimo-v2.5-pro", "provider": "xiaomi"},
        ],
    },
    "T4": {"model": "claude-opus", "provider": "anthropic"},
}

#: The module's own read-back tables, by group. A field only proves it survives if
#: the value that arrived is NOT the value a dropped field reads back as, so
#: non-vacuity below is asserted per GROUP rather than per key.
_READ_BACK_GROUPS = {
    "list": decision_log_mod._PLAN_LIST_KEYS,
    "dict": decision_log_mod._PLAN_DICT_KEYS,
    "bool": decision_log_mod._PLAN_BOOL_KEYS,
    "name": decision_log_mod._PLAN_NAME_KEYS,
    "text": decision_log_mod._PLAN_TEXT_KEYS,
    "count": decision_log_mod._PLAN_COUNT_KEYS,
    "clock": tuple(decision_log_mod._PLAN_CLOCK_RANGES),
}


def _real_phase_two_plans():
    """``[(tier, resolved, plan), ...]`` from the shipped planner, not from literals.

    TWO plans, because no single one carries a non-default value for every read-back
    group: a plan whose cap fired has an empty degrade reason, and a plan that
    degraded its strategy has no cap verdict. That is the whole difficulty of
    testing this seam — a dropped field reads back AS ITS DEFAULT, so a comparison
    of defaults against defaults passes while the field is being lost. Measured
    while writing this: with the cap plan alone, deleting
    ``strategy_degraded_reason`` from the read-back table changed nothing and the
    round trip still passed, because both sides were "".
    """
    features = {"char_len": 100, "has_code": False, "size_lines": 0, "num_files": 0,
                "has_stacktrace": False, "num_requirements": 0,
                "verb_class": "unknown", "lang": "", "keywords": [],
                "est_input_tokens": 500}
    plans = []
    for tier in ("T3", "T2"):
        resolved = rules_mod.resolve_tiers({"model": tier}, _PHASE_TWO_TIERS)
        # rng is deliberately NOT injected: T2 declares ``random``, and the absent
        # rng is what makes the planner report the degrade.
        plans.append((tier, resolved,
                      rules_mod.plan_chain(resolved, features, when=_PEAK_MONDAY)))
    return plans


def test_every_field_the_planner_emits_survives_the_round_trip_to_the_reader():
    """Writer → routes.jsonl → reader, field for field, on real phase-2 plans.

    Not "the keys I remembered to check": every key the planner produced, so a key
    the planner GAINS is caught here instead of going invisible on replay.
    """
    default = empty_chain_plan()
    non_default = set()

    for tier, resolved, plan in _real_phase_two_plans():
        log = DecisionLog()
        log.record("hard_rule", dict(resolved), chain_plan=plan)
        back = chain_plan_of(_persisted(log.entries()[0]))

        assert set(plan) <= set(CHAIN_PLAN_KEYS), (tier, "emitted, not whitelisted")
        assert set(plan) - set(back) == set(), (tier, "whitelisted, dropped on read")
        for key, value in plan.items():
            assert back[key] == value, (tier, key)
            if key not in default or default[key] != value:
                non_default.add(key)

    # Non-vacuity, per read-back group: each group carried at least one field whose
    # value differs from the empty default, so no group passed by having nothing to
    # lose in the first place.
    for group, keys in _READ_BACK_GROUPS.items():
        assert non_default & set(keys), (group, sorted(non_default))


def test_the_plans_the_round_trip_uses_say_what_the_policy_declared():
    """Anchors for the pair above: the facts those two plans are supposed to carry.

    Without them the round trip could agree perfectly about a plan that means
    nothing. Each assertion is also the behaviour the fixture was chosen for —
    including the UNIT rule, which is the one an operator is most likely to
    disbelieve: glm-5.3 is at 2.0x here and is NOT capped, because a DOLLAR ceiling
    cannot speak to credits spent off an allowance already bought, while metered
    deepseek-v4-pro at the same 2.0x is refused by that very cap.
    """
    plans = {tier: plan for tier, _resolved, plan in _real_phase_two_plans()}

    capped = plans["T3"]
    assert capped["multipliers"]["glm-5.3"] == 2.0
    assert [row["model"] for row in capped["capped"]] == ["deepseek-v4-pro"]
    assert capped["peak_priced"] == ["glm-5.3"] and capped["demoted"] == ["glm-5.3"]
    assert capped["chain"], "no filter, cap or policy may empty the chain"
    assert capped["time_cap"] == {"max_multiplier": 1.5}
    assert capped["requirements"] == {"min_context": 128000}
    assert (capped["utc_hour"], capped["utc_weekday"]) == (7, 0)
    assert capped["time_agnostic"] is False

    degraded = plans["T2"]
    assert degraded["strategy_declared"] == "random"
    assert degraded["strategy"] == "sequential"
    assert degraded["strategy_degraded"] is True
    assert degraded["strategy_degraded_reason"], "the free-text field, non-empty"
    assert degraded["pin_primary"] is False
    assert degraded["independent_rails"] == 3

    # And both tiers' knobs are shapes the write gate accepts, so these are plans
    # production could really be holding rather than shapes lint would refuse.
    for tier in ("T2", "T3"):
        assert rules_mod._lint_time_knobs(tier, _PHASE_TWO_TIERS[tier]) == []


def test_the_read_back_table_covers_exactly_the_keys_that_get_persisted():
    """One table, one pass per key: nothing left out, nothing checked twice.

    ``CHAIN_PLAN_KEYS`` is what this module says it persists; the field groups are
    what it can actually read back. A name in the first and not the second is a
    key that is written and then silently dropped — the phase-2 defect — and a
    name in the second and not the first is a claim no writer honours. Sorted-list
    equality asserts both directions AND that no key sits in two groups, where a
    later group would quietly overrule an earlier one's type check.
    """
    groups = (
        decision_log_mod._PLAN_LIST_KEYS
        + decision_log_mod._PLAN_DICT_KEYS
        + decision_log_mod._PLAN_BOOL_KEYS
        + decision_log_mod._PLAN_NAME_KEYS
        + decision_log_mod._PLAN_TEXT_KEYS
        + decision_log_mod._PLAN_COUNT_KEYS
        + tuple(decision_log_mod._PLAN_CLOCK_RANGES)
    )
    assert sorted(groups) == sorted(CHAIN_PLAN_KEYS)
    # The empty default is the whitelist minus exactly the keys documented as
    # staying absent — the clock/cap trio and the veto trio — so "absent" is a
    # decision this module records once, not per reader.
    assert set(empty_chain_plan()) == (
        set(CHAIN_PLAN_KEYS) - decision_log_mod._OPTIONAL_CHAIN_PLAN_KEYS
    )


def test_the_empty_default_is_the_planners_own_degraded_plan_plus_the_counter():
    """One empty-plan shape across three modules, asserted where it can drift.

    ``rules._empty_chain_plan`` is the original; ``service._empty_chain_plan``
    mirrors it (asserted in test_service.py) and this module mirrors it plus its
    own ``rejected_truncated``. A console branches on these keys and cannot see
    which module handed it the plan, so a key added on one side only is how a
    phase-2 decision comes to render as a phase-1 plan.
    """
    assert empty_chain_plan() == {
        **rules_mod._empty_chain_plan(), "rejected_truncated": 0,
    }


def test_an_out_of_range_clock_reads_as_no_clock_not_as_a_plausible_hour():
    """A wrong-looking hour is worse than no hour: a consumer PRICES a plan by it.

    Absent is unambiguous next to ``time_agnostic``; 0 is midnight and 24 is
    nothing. The two ranges differ, so ``7`` is a valid hour and an invalid
    weekday — the same value has to be accepted by one and refused by the other.
    """
    for bad in (-1, 24, 25, True, False, "7", None, 7.5, [7]):
        assert "utc_hour" not in chain_plan_of({"chain_plan": {"utc_hour": bad}}), bad
    for bad in (-1, 7, 24, True, "3", None, 2.5):
        assert "utc_weekday" not in chain_plan_of(
            {"chain_plan": {"utc_weekday": bad}}
        ), bad
    # Both inclusive bounds are inside, including the two that are falsy.
    for hour, weekday in ((0, 0), (23, 6)):
        plan = chain_plan_of({"chain_plan": {"utc_hour": hour, "utc_weekday": weekday}})
        assert (plan["utc_hour"], plan["utc_weekday"]) == (hour, weekday)
    # And a cap that is not a mapping is not a cap: lint refuses ``time_cap: 1.5``
    # in the policy, so a scalar reaching here is corrupt rather than a ceiling.
    for bad in (1.5, "1.5", [1.5], None, True):
        assert "time_cap" not in chain_plan_of({"chain_plan": {"time_cap": bad}}), bad


def test_module_docstring_cause_set_matches_VALID_CAUSES():
    """The docstring enumeration must match the authoritative VALID_CAUSES set.

    Prevents the defect that introduced ``role_out_of_scope`` without updating
    the prose at lines 6-9, leaving a false claim about the closed set.
    """
    import re

    doc = decision_log_mod.__doc__
    # Extract the enumeration block: "Closed cause set — the only valid strings:\n  X, Y, Z,\n  A, B, C\n"
    match = re.search(
        r"Closed cause set — the only valid strings:\s*\n\s*(.+?)\n\n",
        doc,
        re.DOTALL,
    )
    assert match, "docstring missing the 'Closed cause set' enumeration"
    enum_block = match.group(1)
    # Split by commas and whitespace, filter empty strings
    docstring_causes = {c.strip() for c in enum_block.replace("\n", ",").split(",") if c.strip()}
    assert docstring_causes == VALID_CAUSES, (
        f"docstring enumeration ({sorted(docstring_causes)}) != "
        f"VALID_CAUSES ({sorted(VALID_CAUSES)})"
    )
