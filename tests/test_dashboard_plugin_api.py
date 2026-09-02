"""Dashboard plugin API <-> RouterService shape parity.

SPLIT OUT of tests/test_webui_extension.py, and the split is the point.

``pytest.importorskip("fastapi")`` sat at module level in the MIDDLE of that
file, with the console's static-contract tests above it. A module-level
``importorskip`` skips the WHOLE MODULE, not the tests below it — so on any
interpreter without fastapi, all 67 tests in that file silently did not run.
CI is exactly that interpreter: ``.github/workflows/ci.yml`` installed
``pyyaml pytest pytest-cov`` and nothing else.

Measured on this tree: with fastapi present, 1964 pass; with it absent, 1897 —
the 67 missing are that whole file, including the XSS-sink scan, the
one-wall-clock scan, the six-tab assertion and the ``CAUSE_WORDS`` closed-set
agreement. The gate that guards the browser-facing surface was not running in
the gate that is supposed to own it. Same shape as the ``node --test <missing
path>`` incident the CI comments already memorialise: a gate that passes when
its inputs vanish is not a gate.

So the fastapi-dependent tests live here, behind a gate that is the FIRST
statement in the file, and the console contract tests stay where they were with
no gate at all. ``fastapi`` is also installed in CI now, so ``plugin_api.py``
(185 statements) actually enters the 100 % coverage gate instead of being
invisible to it.

Why the parity tests exist at all: the extension mounts the console and the
console reads the plugin API, so a key the service reports and the plugin API
drops renders as an empty panel — which an operator reads as "the feature is
broken". The same goes for a plan: the two surfaces must answer the same
question at the same instant, or the operator is comparing a preview against a
production route that never matched it.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

# ── Dashboard plugin API ↔ RouterService shape parity ────────────────
#
# fastapi is a Hermes-dashboard dependency, not one of this plugin's (pyyaml
# only), so these skip cleanly where the dashboard is not installed rather than
# forcing a new dependency on the router package.

pytest.importorskip("fastapi")

from fastapi import FastAPI, HTTPException  # noqa: E402

from dashboard import plugin_api  # noqa: E402
from router.decision_log import DecisionLog, empty_chain_plan  # noqa: E402
from router.service import RouterService  # noqa: E402


# Valid policy (lint returns []) that nonetheless produces ONE advisory warning:
# T2 names an elo the capability registry has never heard of, which is
# unverifiable, not wrong. This is the config that proves warnings and
# validation_errors are separate axes.
_WARNING_BUT_VALID = {
    "enabled": True,
    "default": {"model": "T1"},
    "rules": [],
    "tiers": {
        "T1": {
            "model": "glm-4.7",
            "provider": "zai",
            "billing_mode": "plan",
            "fallback_strategy": "sequential",
            "pin_primary": True,
            "requirements": {"tool_calling": True},
            "time_policy": {"avoid_peak": ["zai", "deepseek"], "prefer": ["mimo-v2.5"]},
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [
                {"model": "deepseek-v4-flash", "provider": "deepseek",
                 "billing_mode": "metered"},
            ],
        },
        "T2": {"model": "made-up-elo-9000", "provider": "acme", "fallback": []},
        "T3": {"model": "glm-4.7", "provider": "zai"},
        "T4": {"model": "glm-4.7", "provider": "zai"},
    },
}

# The row router.yaml invites operators to enable: heavy work is pushed down a
# tier during the 06:00-10:00 UTC peak, when deepseek and zai both bill at 2.0x.
# It is keyed on utc_hour, which signals.extract() cannot produce — the edge
# INJECTS it — so this policy is the direct probe for "did the clock arrive?".
_TIME_KEYED = {
    "enabled": True,
    "default": {"model": "T1"},
    "rules": [
        {
            "id": "defer-heavy-work-off-peak",
            "when": {"utc_hour": {"gte": 6, "lt": 10}, "verb_class": {"eq": "hard"}},
            "then": {"model": "T3"},
        },
    ],
    "tiers": {
        "T1": {
            "model": "glm-5.3-flash",
            "provider": "zai",
            "billing_mode": "plan",
            "fallback_strategy": "cheapest_now",
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [
                {"model": "gpt-5.6-luna", "provider": "openai-codex"},
                {"model": "mimo-v2.5", "provider": "xiaomi"},
            ],
        },
        "T2": {"model": "glm-5.3", "provider": "zai"},
        "T3": {"model": "mimo-v2.5", "provider": "xiaomi"},
        "T4": {"model": "glm-5.3-flash", "provider": "zai"},
    },
}

# verb_class == "hard", so the time-keyed row's second clause holds and only the
# hour decides whether it fires.
_HARD_TASK = "Debug a race condition across 3 files in the scheduler"

# verb_class == "trivial": the time-keyed row never matches it, so it falls through
# to the default T1 — the tier carrying cheapest_now and the 1.5x ceiling, which is
# where the price window is observable.
_TRIVIAL_TASK = "fix typo in the code function"

# Monday 07:30 UTC — inside the peak, and a WEEKDAY, because the zai peak is
# weekday-gated. 07:30 rather than 07:00 so the hour truncation is exercised too.
_PEAK = "2026-08-17T07:30:00Z"
_OFF_PEAK = "2026-08-17T15:00:00Z"


@pytest.fixture
def plugin_config(tmp_path, monkeypatch):
    """Point the plugin API at a throwaway router.yaml and return a writer."""
    path = tmp_path / "router.yaml"

    def write(config):
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return path

    write(_WARNING_BUT_VALID)
    monkeypatch.setattr(plugin_api, "_CONFIG_PATH", path)
    return write


def _service_explain(task, at, prompt_text=""):
    """``RouterService.explain`` for the same task, instant and prompt.

    Each parameter is forwarded only when the installed service declares it, so
    this parity assertion still MEANS something against a service predating the
    injected clock or the composed prompt instead of dying on an unexpected
    keyword — the plugin and the service are then compared as the two equally
    time-agnostic (or equally goal-sized) surfaces they both are.
    """
    service = RouterService(plugin_api._CONFIG_PATH)
    declared = inspect.signature(service.explain).parameters
    kwargs = {}
    if "at" in declared:
        kwargs["at"] = at
    if "prompt_text" in declared:
        kwargs["prompt_text"] = prompt_text
    return service.explain(task, **kwargs)


def _plan_of(payload):
    """The chain plan of an explain payload, from either surface's shape."""
    for candidate in (payload.get("chain_plan"),
                      (payload.get("decision") or {}).get("chain_plan")):
        if isinstance(candidate, dict):
            return candidate
    return empty_chain_plan()


def test_plugin_status_carries_warnings_and_errors_as_separate_axes(plugin_config):
    """A warning informs; only validation_errors may flip valid.

    The console renders the two in different places. Merged — or with warnings
    absent, as this endpoint shipped — an advisory finding either reads as a
    broken router or vanishes entirely.
    """
    status = asyncio.run(plugin_api.api_status())

    assert "validation_errors" in status and "warnings" in status
    assert status["validation_errors"] == []
    assert status["valid"] is True, "a warning must never flip valid to false"
    assert isinstance(status["warnings"], list)
    assert any("made-up-elo-9000" in w for w in status["warnings"]), (
        "an elo the capability registry cannot describe is advisory, not an error"
    )
    # Parity with the service the console's other surface reads.
    service_status = RouterService(plugin_api._CONFIG_PATH).status()
    assert set(service_status) <= set(status)
    for key in ("valid", "validation_errors", "enabled", "tiers"):
        assert status[key] == service_status[key]
    assert status["warnings"] == service_status.get("warnings", [])
    # Legacy keys the bundled dashboard UI reads are still served.
    assert status["rules_count"] == 0
    assert status["banned_models"] == []
    assert "classifier_model" in status


def test_plugin_status_never_lets_a_warning_flip_valid(plugin_config, monkeypatch):
    """A non-empty advisory list is passed through and changes nothing else.

    Stubbing the service's warnings is the only way to pin THIS module's half of
    the contract — that it neither drops the list nor lets it reach ``valid`` —
    independently of which findings the current registry happens to produce.
    """
    real_status = RouterService.status
    advisory = ["model 'made-up-elo-9000': not in the capability registry"]

    def stubbed(self):
        reported = dict(real_status(self))
        reported["warnings"] = list(advisory)
        return reported

    monkeypatch.setattr(RouterService, "status", stubbed)
    status = asyncio.run(plugin_api.api_status())
    assert status["warnings"] == advisory
    assert status["validation_errors"] == []
    assert status["valid"] is True


def test_plugin_status_reports_a_broken_policy_instead_of_raising(plugin_config):
    """Read paths never raise: an invalid policy is a diagnostic, not a 500."""
    plugin_config({"enabled": True, "rules": []})  # no default, no tiers
    status = asyncio.run(plugin_api.api_status())
    assert status["valid"] is False
    assert status["validation_errors"], "the operator must be told what is wrong"
    assert status["warnings"] == []


def test_plugin_status_survives_a_corrupt_config(plugin_config, tmp_path):
    """Unparseable YAML degrades to empty defaults plus an error string."""
    (tmp_path / "router.yaml").write_text("enabled: [unclosed\n", encoding="utf-8")
    status = asyncio.run(plugin_api.api_status())
    assert status["valid"] is False
    assert status["banned_models"] == [] and status["tiers"] == []


def test_plugin_policy_exposes_the_per_tier_knobs(plugin_config):
    """Every tier field reaches the console, including the time layer.

    The tier mapping is copied whole precisely so the next knob added does not
    need this endpoint edited — and so the console can render the ones added
    this phase instead of an empty panel.
    """
    policy = asyncio.run(plugin_api.api_rules())
    tier = policy["tiers"]["T1"]
    for knob in ("fallback_strategy", "pin_primary", "billing_mode",
                 "requirements", "time_policy", "time_cap"):
        assert knob in tier, f"policy must expose tier knob '{knob}'"
    assert tier["time_cap"] == {"max_multiplier": 1.5}
    assert tier["time_policy"]["avoid_peak"] == ["zai", "deepseek"]
    assert policy == RouterService(plugin_api._CONFIG_PATH).policy()
    # Historical keys the bundled UI reads.
    assert policy["rules"] == [] and policy["default"] == {"model": "T1"}


def test_plugin_explain_returns_and_records_the_chain_plan(plugin_config, monkeypatch):
    """A recorded decision carries chain_plan, and the response lifts it.

    Without the recorded plan, replaying a decision cannot show which elos were
    eligible, which were rejected and why, or in what order they would be tried.
    """
    log = plugin_api.DecisionLog()
    monkeypatch.setattr(plugin_api, "_log", log)  # never leak into other tests
    result = asyncio.run(plugin_api.api_explain(task="refactor the parser module"))

    assert isinstance(result["chain_plan"], dict)
    for key in ("chain", "requirements", "rejected", "strategy"):
        assert key in result["chain_plan"]
    entry = log.tail(1)[0]
    assert "chain_plan" in entry, "a recorded decision must carry chain_plan"
    assert entry["chain_plan"]["chain"] == result["chain_plan"]["chain"]


def test_plugin_explain_preview_is_stable_across_polls(plugin_config, monkeypatch):
    """The dashboard polls /explain, so the previewed chain must not churn.

    Stability is promised WITHIN the hour, not across it: the clock is pinned to a
    fixed minute here only to prove that two polls seconds apart cannot differ,
    which is the property the fixed preview seed plus the hour truncation buy.
    """
    monkeypatch.setattr(
        plugin_api, "_utc_now",
        lambda: datetime(2026, 8, 17, 7, 44, 12, tzinfo=timezone.utc),
    )
    first = asyncio.run(plugin_api.api_explain(task="write a unit test"))
    second = asyncio.run(plugin_api.api_explain(task="write a unit test"))
    assert first == second


def test_plugin_explain_still_answers_on_an_invalid_policy(plugin_config):
    """Unlike the write-gated service, this preview must not refuse.

    A broken config is exactly when an operator needs to see where a task lands;
    /status already reports the errors, so refusing here only hides information.
    """
    plugin_config({"enabled": True, "rules": []})
    result = asyncio.run(plugin_api.api_explain(task="anything"))
    assert "chain_plan" in result and "output" in result


def test_plugin_lint_keeps_warnings_out_of_valid(plugin_config):
    result = asyncio.run(plugin_api.api_lint())
    assert result["valid"] is True and result["errors"] == []
    assert isinstance(result["warnings"], list)
    assert result["warnings"] == (
        RouterService(plugin_api._CONFIG_PATH).status().get("warnings", [])
    )


# ── The clock: /explain must ask production's question ───────────────


def test_plugin_explain_fires_a_time_keyed_rule(plugin_config):
    """utc_hour reaches the matcher, so an hour-keyed row is live here.

    This endpoint used to build the feature vector from signals.extract() alone.
    utc_hour is INJECTED, never extracted, so the clause could not be satisfied
    and this row was permanently inert on the dashboard while firing in
    production — the operator's preview answered a question production never asks.
    """
    plugin_config(_TIME_KEYED)

    inside = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_PEAK))
    assert inside["matched_rule_id"] == "defer-heavy-work-off-peak"
    assert inside["matched_clauses"]["utc_hour"] == {"gte": 6, "lt": 10}
    assert inside["output"]["model"] == "mimo-v2.5", "T3 is where the peak defers to"
    assert inside["evaluated_at"]["utc_hour"] == 7
    assert inside["evaluated_at"]["utc_weekday"] == 0, "Monday: the zai peak is gated"
    assert inside["evaluated_at"]["at_source"] == "explicit"

    outside = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_OFF_PEAK))
    assert outside["matched_rule_id"] is None, "off peak the row must NOT fire"
    assert outside["output"]["model"] == "glm-5.3-flash"
    assert outside["evaluated_at"]["utc_hour"] == 15


def test_plugin_explain_agrees_with_the_service_at_the_same_instant(plugin_config):
    """The two operator surfaces must render ONE plan for one task and instant.

    Agreement is the actual requirement — not any particular chain — because the
    plan RouterService composes is the one production attempts. When the plugin
    omitted the clock the two diverged on exactly the material an operator uses to
    decide: the chain order, the price multipliers in force, and which rails a
    time_cap refused.
    """
    plugin_config(_TIME_KEYED)

    plugin = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    service = _service_explain(_TRIVIAL_TASK, _PEAK)
    decision = service["decision"]

    for key in ("matched_rule_id", "output", "cause", "matched_clauses"):
        assert plugin[key] == decision[key], f"the two surfaces disagree on {key}"

    plugin_plan, service_plan = _plan_of(plugin), _plan_of(service)
    for key in ("chain", "multipliers", "capped", "strategy", "time_agnostic"):
        assert plugin_plan.get(key) == service_plan.get(key), (
            f"chain_plan.{key} differs between the dashboard and the service"
        )
    assert plugin_plan == service_plan
    assert plugin["evaluated_at"] == service["evaluated_at"]

    # Two blank plans also "agree", so pin that this instant produced REAL time
    # material — otherwise the assertions above would still pass with the clock
    # dropped on both surfaces. At Monday 07:00 UTC zai bills glm-5.3-flash at
    # 2.0x, over T1's declared ceiling of 1.5.
    ceiling = _TIME_KEYED["tiers"]["T1"]["time_cap"]["max_multiplier"]
    assert plugin_plan["time_agnostic"] is False, "the clock must reach the planner"
    assert plugin_plan["multipliers"]["glm-5.3-flash"] > ceiling, (
        "the multipliers in force at this hour must be reported"
    )
    # T1's ceiling is a DOLLAR ceiling and the primary is plan-billed, so the cap
    # cannot evict it (see capabilities.apply_time_cap: a credit multiplier adds no
    # dollars, and paying metered money to dodge a sunk cost is not a cost
    # control). What the cap governs on this tier is the metered tail, and neither
    # hop is over the ceiling at this hour — hence nothing removed.
    chain_models = [hop.get("model") for hop in plugin_plan["chain"]]
    assert "glm-5.3-flash" in chain_models, (
        "a dollar ceiling may not evict a plan rail"
    )
    # Whatever the cap DID remove must be gone from the chain, unless it gave way
    # entirely — the one invariant that holds under either unit regime.
    if not plugin_plan.get("time_cap_bypassed"):
        for entry in plugin_plan["capped"]:
            assert entry["model"] not in chain_models
    assert plugin_plan["chain"], "a cap may never empty the chain"


def test_plugin_explain_defaults_to_the_current_utc_hour(plugin_config, monkeypatch):
    """With no ``at`` the plan is evaluated at NOW, truncated to the hour.

    A time-agnostic default would put the endpoint back where it started: every
    multiplier 1.0 and every hour-keyed row inert. The truncation is what makes
    two polls in the same hour byte-identical while the next hour may differ.
    """
    plugin_config(_TIME_KEYED)
    monkeypatch.setattr(
        plugin_api, "_utc_now",
        lambda: datetime(2026, 8, 17, 7, 44, 12, tzinfo=timezone.utc),
    )
    result = asyncio.run(plugin_api.api_explain(task=_HARD_TASK))
    assert result["evaluated_at"]["at_source"] == "now"
    assert result["evaluated_at"]["at"] == "2026-08-17T07:00:00+00:00"
    assert result["matched_rule_id"] == "defer-heavy-work-off-peak"


def test_plugin_explain_refuses_an_unusable_at(plugin_config):
    """An unparseable clock is the CALLER's error: a 400, never a wrong hour.

    Falling back to "now" would answer a different question than the one asked
    and record it as though it were the answer — on an audit surface that is worse
    than refusing.
    """
    with pytest.raises(HTTPException) as raised:
        asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at="tuesday-ish"))
    assert raised.value.status_code == 400
    assert "ISO-8601" in str(raised.value.detail)


def test_plugin_explain_treats_a_blank_at_as_now(plugin_config):
    """An empty query string is an absent parameter, not a bad one.

    A form that submits ``at=`` must not 400 the panel that renders the plan.
    """
    plugin_config(_TIME_KEYED)
    result = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at="  "))
    assert result["evaluated_at"]["at_source"] == "now"


# ── The size: /explain must measure the turn production sends ────────
#
# The clock was the first half of this endpoint answering a question production
# does not ask; the SIZE was the second. ``task`` is the goal line and
# ``prompt_text`` is the composed context + goal the child really receives, which
# is what ``est_input_tokens`` — and therefore every context-conditional rule and
# the derived ``min_context`` requirement — has to be measured from.

# A context-keyed row: the shape router.yaml ships for long reads. It is keyed on
# est_input_tokens, which is measured from the TEXT, so this policy is the direct
# probe for "was the turn sized from the prompt or from the goal?".
_CONTEXT_KEYED = {
    "enabled": True,
    "default": {"model": "T1"},
    "rules": [
        {
            "id": "huge-context-read",
            "when": {"est_input_tokens": {"gt": 20000}},
            "then": {"model": "T3"},
        },
    ],
    "tiers": {
        "T1": {"model": "glm-4.7", "provider": "zai"},
        "T2": {"model": "glm-5.3", "provider": "zai"},
        "T3": {"model": "gpt-5.6-terra", "provider": "openai-codex",
               "requirements": {"min_context": 200000}},
        "T4": {"model": "gpt-5.5", "provider": "openai-codex"},
    },
}

# A goal line that matches no row on its own (6-ish estimated tokens) plus a
# context that production really would send. 120k chars is ~33k tokens at the
# router's 3.6-chars-per-token ratio, so it clears the row's 20k threshold.
_TRIVIAL_GOAL = "summarise this log"
_BIG_CONTEXT = "WARN retry scheduled for the nightly job\n" * 3000
_COMPOSED = f"Context: {_BIG_CONTEXT.strip()}\n\nTask: {_TRIVIAL_GOAL}"


def test_plugin_explain_sizes_the_turn_from_the_prompt_not_the_goal(plugin_config):
    """A context-heavy turn must preview as the route production takes.

    Sized from the goal line the same turn measures ~6 estimated tokens: no row
    matches, the plan derives a trivial ``min_context``, and the operator is shown
    a plan that never existed. Both calls go through the same endpoint, so the
    ONLY difference is which text was measured.
    """
    plugin_config(_CONTEXT_KEYED)

    sized = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_OFF_PEAK, prompt_text=_COMPOSED,
    ))
    assert sized["matched_rule_id"] == "huge-context-read"
    assert sized["matched_clauses"]["est_input_tokens"] == {"gt": 20000}
    assert sized["output"]["model"] == "gpt-5.6-terra", "T3 is the long-context tier"
    assert sized["preview"]["sized_from"] == "prompt_text"
    assert sized["preview"]["prompt_chars"] == len(_COMPOSED)
    sized_plan = _plan_of(sized)
    assert sized_plan["requirements"]["min_context"] > 20000, (
        "the derived floor must come from the real input size"
    )
    assert sized_plan["chain"], "a filter may never empty the chain"

    # The same goal with no prompt: the historical behaviour, now LABELLED.
    goal_only = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, at=_OFF_PEAK))
    assert goal_only["matched_rule_id"] is None, "6 tokens matches no context row"
    assert goal_only["output"]["model"] == "glm-4.7", "it falls through to T1"
    assert goal_only["preview"]["sized_from"] == "task"
    assert goal_only["preview"]["prompt_chars"] == len(_TRIVIAL_GOAL)
    assert _plan_of(goal_only)["requirements"]["min_context"] < 1000


def test_plugin_explain_agrees_with_the_service_on_the_same_prompt(plugin_config):
    """One turn, one instant, one prompt — the two surfaces must agree.

    Agreement is the assertion, not any particular chain: the plan
    ``RouterService`` composes is the one production attempts. Asserting the
    dashboard's own answer alone is exactly how this endpoint shipped twice with a
    plan production would never produce — first with no clock, then with the turn
    sized from the goal line.
    """
    plugin_config(_CONTEXT_KEYED)

    plugin = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    ))
    service = _service_explain(_TRIVIAL_GOAL, _PEAK, prompt_text=_COMPOSED)
    decision = service["decision"]

    for key in ("matched_rule_id", "output", "cause", "matched_clauses"):
        assert plugin[key] == decision[key], f"the two surfaces disagree on {key}"
    assert _plan_of(plugin) == _plan_of(service)
    assert plugin["evaluated_at"] == service["evaluated_at"]
    # The size disclaimer is shared material too: a console rendering either
    # surface must read the same "this measured the real turn" note.
    assert plugin["preview"] == service["preview"]
    # Two goal-sized plans would also "agree", so pin that this call really
    # measured the composed prompt on BOTH surfaces.
    assert plugin["preview"]["sized_from"] == "prompt_text"
    assert plugin["matched_rule_id"] == "huge-context-read"


def test_plugin_explain_refuses_an_unusable_prompt_text(plugin_config):
    """An unusable prompt is the CALLER's error: a 400, never a wrong size.

    Truncating or coercing it would answer with a smaller ``est_input_tokens``
    than the turn really has — a confidently wrong plan, which is the precise
    failure this parameter exists to fix — and the bound is what keeps an
    unauthenticated read path from being made to cost arbitrary CPU.
    """
    plugin_config(_CONTEXT_KEYED)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=17))
    assert raised.value.status_code == 400
    assert "prompt_text" in str(raised.value.detail)

    oversized = "x" * (1_048_576 + 1)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=oversized))
    assert raised.value.status_code == 400
    assert "prompt_text" in str(raised.value.detail)
    # The refusal is the service's own, so the two surfaces refuse the same input.
    with pytest.raises(ValueError):
        _service_explain(_TRIVIAL_GOAL, _OFF_PEAK, prompt_text=oversized)


def test_plugin_explain_treats_an_empty_prompt_text_as_the_task(plugin_config):
    """An empty field is an absent parameter; whitespace is text, as production has it.

    A form that submits ``prompt_text=`` must not change the answer, and must not
    400 the panel. Whitespace is NOT normalised away — ``adapter.route`` sizes the
    turn with the same falsy test — so that case is asserted against the SERVICE
    rather than against a number this file made up.
    """
    plugin_config(_CONTEXT_KEYED)

    empty = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=""))
    assert empty["preview"]["sized_from"] == "task"
    assert empty["preview"]["prompt_chars"] == len(_TRIVIAL_GOAL)

    blank = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_OFF_PEAK, prompt_text="   ",
    ))
    service = _service_explain(_TRIVIAL_GOAL, _OFF_PEAK, prompt_text="   ")
    assert blank["preview"] == service["preview"]


def test_plugin_explain_post_is_the_same_answer_through_a_wider_pipe(plugin_config):
    """A 120k-char prompt does not fit in a URL, so the body carries it.

    The two forms must be one handler: same names, same validation, same payload
    for the same input. A POST that answered differently would be the two-surfaces
    defect inside a single file — and a GET-only endpoint could not carry the
    prompt this parameter exists for at all, which is a preview that silently goes
    back to measuring the goal line.
    """
    plugin_config(_CONTEXT_KEYED)

    posted = asyncio.run(plugin_api.api_explain_post(
        {"task": _TRIVIAL_GOAL, "at": _OFF_PEAK, "prompt_text": _COMPOSED}
    ))
    got = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_OFF_PEAK, prompt_text=_COMPOSED,
    ))
    assert posted == got
    assert posted["matched_rule_id"] == "huge-context-read"
    assert posted["preview"]["prompt_chars"] == len(_COMPOSED)
    assert len(_COMPOSED) > 100_000, "the pipe has to be wider than a query string"

    # An explicit null is the one thing a query string cannot express: it reads as
    # "not supplied", not as an unusable value.
    nulled = asyncio.run(plugin_api.api_explain_post(
        {"task": _TRIVIAL_GOAL, "at": _OFF_PEAK, "prompt_text": None}
    ))
    assert nulled["preview"]["sized_from"] == "task"

    # Fail-closed on the caller's own input, with the sidecar's wording.
    for body, fragment in (
        ({"task": _TRIVIAL_GOAL, "at": 7}, "at must be a string"),
        ({"task": _TRIVIAL_GOAL, "prompt_text": 17}, "prompt_text must be a string"),
        ({"at": _OFF_PEAK}, "task is required"),
        ("not an object", "must be a JSON object"),
        # No body at all is "no task", not a TypeError: the parameter defaults to
        # None and an absent task is the caller's error, refused like any other.
        (None, "task is required"),
    ):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(plugin_api.api_explain_post(body))
        assert raised.value.status_code == 400
        assert fragment in str(raised.value.detail)


def test_plugin_explain_reads_a_datetime_at_exactly_as_its_iso_spelling(plugin_config):
    """``at`` may arrive as a datetime, and must mean the same instant.

    The HTTP layer only ever produces text, but this module's helpers are called
    in process too (the sidecar and the CLI pass datetimes to the service), and a
    surface where ``07:30Z`` and ``datetime(7, 30, tzinfo=utc)`` answer differently
    is the same drift in miniature. A naive value is taken to already BE UTC —
    the reading ``rules`` and ``capabilities`` use — rather than localised, because
    localising it would silently move the hour every price window is keyed on.
    """
    plugin_config(_TIME_KEYED)

    spelled = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    aware = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_TASK, at=datetime(2026, 8, 17, 7, 30, tzinfo=timezone.utc),
    ))
    naive = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_TASK, at=datetime(2026, 8, 17, 7, 30),
    ))
    assert aware == spelled and naive == spelled
    # Another zone, the same instant: converted, not re-read as a local hour.
    shifted = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_TASK,
        at=datetime(2026, 8, 17, 9, 30, tzinfo=timezone(timedelta(hours=2))),
    ))
    assert shifted == spelled
    assert spelled["evaluated_at"]["utc_hour"] == 7


# ── Every read path, over the install an operator actually has ────────
#
# "No route may raise over the ROUTER's state" is this module's own contract, and
# it is not a nicety: an operator opens this panel BECAUSE the config is broken,
# and a panel that 500s tells them nothing about why. So every route is asserted
# over the states a router.yaml is really found in, and the degraded shapes are
# pinned — a console that receives {} where it expected a list renders "undefined"
# and reads as a feature that is missing rather than a file that is wrong.

_BROKEN_CONFIGS = {
    "unparseable": "enabled: [unclosed\n",
    "scalar_root": "just a string\n",
    "sequence_root": "- glm-4.7\n- mimo-v2.5\n",
    "empty": "",
}

# Every field name that would be a credential if this surface ever grew one.
# "token" is deliberately absent: est_input_tokens and max_input_tokens are
# routing material, and a substring rule that flagged them would have to be
# weakened until it caught nothing.
_CREDENTIAL_SHAPED = (
    "api_key", "apikey", "secret", "password", "passwd", "credential",
    "authorization", "bearer", "private_key", "access_token", "auth_token",
)

# A policy carrying real bans, so the two ban surfaces have something to agree on.
_BANNED = {
    **_TIME_KEYED,
    "blocklist": {
        "manual_ban": [
            {"model": "glm-5.3", "provider": "zai", "reason": "quota exhausted"},
            {"model": "gpt-5.5", "provider": "openai-codex", "reason": "billing"},
        ],
        "fallback_chain": ["glm-4.7", "mimo-v2.5"],
        # Enabled so /blocklist really reaches for persisted breaker state, which
        # is what gives "a read path never writes it" something to prove.
        "auto_breaker": {"enabled": True, "threshold": 3, "cooldown_seconds": 900},
    },
}


@pytest.fixture
def hermetic_state(tmp_path, monkeypatch):
    """Point breaker state at a throwaway HERMES_HOME and return its path.

    /blocklist reads the REAL persisted breaker state, so without this the suite
    reads (and a regression could write) the operator's own state file. The
    returned path is also the assertion: a read path must never create it.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return tmp_path / "hermes" / "hermes-smart-router" / "state" / "breaker-state.json"


def _reads():
    """Every route on this surface except /explain, which needs a task."""
    return {
        "status": plugin_api.api_status,
        "rules": plugin_api.api_rules,
        "blocklist": plugin_api.api_blocklist,
        "log": plugin_api.api_log,
        "lint": plugin_api.api_lint,
    }


@pytest.mark.parametrize("flavour", [*sorted(_BROKEN_CONFIGS), "missing"])
def test_plugin_read_paths_answer_over_a_broken_install(
    plugin_config, hermetic_state, monkeypatch, flavour
):
    """No route raises over the router's state, whatever state that is.

    Four real ones plus an absent file: a half-typed flow sequence (the shape a
    hand edit leaves behind), a scalar and a sequence root (both load fine and are
    both the wrong shape, so a type guard rather than the parser has to catch
    them), and an empty file — which is what a truncated atomic write leaves.
    """
    monkeypatch.setattr(plugin_api, "_log", plugin_api.DecisionLog())
    path = plugin_config(_TIME_KEYED)
    if flavour == "missing":
        path.unlink()
    else:
        path.write_text(_BROKEN_CONFIGS[flavour], encoding="utf-8")

    served = {name: asyncio.run(read()) for name, read in _reads().items()}

    status, lint = served["status"], served["lint"]
    assert status["valid"] is False
    assert status["validation_errors"], "the operator must be told what is wrong"
    # The panel's light and the write gate refuse for the SAME reason. They read
    # one loader, so a divergence here would mean an operator staring at a green
    # panel while every apply is refused.
    assert lint["errors"] == status["validation_errors"]
    assert lint["valid"] == status["valid"]
    assert status["warnings"] == [], "a load failure is an error, never an advisory"
    assert status["tiers"] == [] and status["banned_models"] == []
    assert status["classifier_model"] == ""
    assert served["rules"] == {
        "rules": [], "default": {}, "tiers": {}, "fail_safe": {},
        # The price-window overlay degrades to an empty table over a broken
        # install, exactly as `tiers` does.
        "price_windows": {},
        # The compaction choice degrades to absent over a broken install too.
        "compaction": None,
    }
    assert served["blocklist"] == RouterService(plugin_api._CONFIG_PATH).blocklist()
    assert served["blocklist"]["manual_bans"] == []
    assert served["log"] == {"entries": []}

    # /explain deliberately still answers, and its plan keeps the shape the
    # console branches on — including time_agnostic, which is what stops the
    # browser pricing a clockless plan against its own hour.
    explained = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK))
    assert explained["output"] == {} and explained["matched_rule_id"] is None
    plan = explained["chain_plan"]
    for key in ("chain", "requirements", "rejected", "strategy", "time_agnostic"):
        assert key in plan, f"the degraded plan must still carry {key}"
    assert plan["chain"] == [], "no policy, nothing to attempt"
    # The decision was still recorded, so replay does not go blind on a bad file.
    assert asyncio.run(plugin_api.api_log(tail=1))["entries"][0]["task"] == (
        _TRIVIAL_TASK
    )
    assert not hermetic_state.exists(), "a read path must not create breaker state"
    # ...and a PREVIEW is not a route: the operator's replay trace must not fill
    # up with dashboard polls. This plugin records to its own in-memory log only.
    assert not Path(os.environ["HERMES_ROUTE_TRACE_FILE"]).exists()


def _key_names_and_strings(payload):
    """Every key name and every string value anywhere in a served payload."""
    keys, values, stack = [], [], [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                keys.append(str(key))
                stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            values.append(item)
    return keys, values


def test_plugin_serves_no_credential(plugin_config, hermetic_state, monkeypatch):
    """Only non-secret operational state, asserted rather than assumed.

    This panel is reachable from a browser and its answers get pasted into
    issues, so a token echoed once is leaked for good. Two halves: no field this
    surface serves is credential-shaped, and nothing it serves came from the
    process environment, which is where the provider keys actually live — the
    router config holds none, and a read path that started resolving them would
    be a leak no shape assertion would catch.
    """
    plugin_config(_BANNED)
    monkeypatch.setenv("ZAI_API_KEY", "sk-canary-must-not-be-served")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-canary-must-not-be-served")

    served = {name: asyncio.run(read()) for name, read in _reads().items()}
    served["explain"] = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_TASK, at=_PEAK, prompt_text=_COMPOSED,
    ))
    # A traversal that reached nothing would pass every assertion below, so pin
    # that it descends into the nested material first.
    policy_keys, policy_values = _key_names_and_strings(served["rules"])
    assert "time_cap" in policy_keys and "glm-5.3-flash" in policy_values

    for name, payload in served.items():
        keys, values = _key_names_and_strings(payload)
        assert keys, f"/{name} served nothing to check"
        for key in keys:
            assert not any(shape in key.lower() for shape in _CREDENTIAL_SHAPED), (
                f"/{name} serves a credential-shaped field: {key}"
            )
        for value in values:
            assert "sk-canary" not in value, f"/{name} echoed an environment secret"

    # /status invents exactly the two legacy keys the bundled UI reads on top of
    # the service's snapshot — nothing else, secret or otherwise, is added here.
    service_status = RouterService(plugin_api._CONFIG_PATH).status()
    assert set(served["status"]) - set(service_status) == {
        "banned_models", "classifier_model",
    }


def test_plugin_blocklist_and_status_name_the_same_bans(plugin_config, hermetic_state):
    """The chip list and the blocklist endpoint are one fact served twice.

    ``banned_models`` is projected BY HAND in this module while ``manual_bans``
    comes from ``Blocklist``; the bundled dashboard UI reads the first and the
    console reads the second. Asserting either alone would let them drift into
    disagreeing about which rails are banned — which is the whole recurring
    defect, scoped to one file.
    """
    plugin_config(_BANNED)

    status = asyncio.run(plugin_api.api_status())
    blocked = asyncio.run(plugin_api.api_blocklist())

    assert blocked == RouterService(plugin_api._CONFIG_PATH).blocklist()
    assert status["banned_models"] == [ban["model"] for ban in blocked["manual_bans"]]
    assert status["banned_models"] == ["glm-5.3", "gpt-5.5"]
    assert blocked["fallback_chain"] == ["glm-4.7", "mimo-v2.5"]
    # One config field, two projections on one surface.
    assert status["breaker_enabled"] == blocked["breaker_enabled"] is True
    assert blocked["breaker_cooldowns"] == [], "no persisted state, no cooldowns"
    assert not hermetic_state.exists(), "reading the blocklist must not write state"


def test_plugin_status_drops_a_ban_row_it_cannot_name(plugin_config):
    """A ban row without a model is omitted, not rendered as a blank chip.

    ``manual_ban`` is hand-edited YAML, so the rows really do arrive malformed.
    The chip list can only show models, so a row that names none is dropped here
    while /blocklist still reports it verbatim for the console to describe.
    """
    plugin_config({**_TIME_KEYED, "blocklist": {"manual_ban": [
        "glm-5.3", {"reason": "someone forgot the model"}, {"model": "gpt-5.5"},
    ]}})

    status = asyncio.run(plugin_api.api_status())
    assert status["banned_models"] == ["gpt-5.5"]
    assert asyncio.run(plugin_api.api_blocklist())["manual_bans"] == [
        "glm-5.3", {"reason": "someone forgot the model"}, {"model": "gpt-5.5"},
    ]


def test_plugin_log_serves_the_decision_explain_returned(plugin_config, monkeypatch):
    """One decision, described twice: the response and the recorded entry.

    /log is the surface that DISPLAYS what /explain ran, so a divergence here is
    the recurring defect at its smallest scale. Two ways it could arise, both
    asserted: ``DecisionLog.record`` rewrites any cause outside its closed set to
    ``fail_safe_strong``, and the recorded attempted head comes off the PLAN while
    ``output.model`` is the declared tier primary — a replay reading the wrong one
    names a rail the planner never chose.
    """
    log = plugin_api.DecisionLog()
    monkeypatch.setattr(plugin_api, "_log", log)
    plugin_config(_TIME_KEYED)

    explained = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    served = asyncio.run(plugin_api.api_log())

    assert served["entries"] == log.tail(50)
    entry = served["entries"][-1]
    assert entry["cause"] == explained["cause"], "the log must not relabel the cause"
    assert entry["rule_id"] == explained["matched_rule_id"]
    assert entry["output"]["model"] == explained["output"]["model"]
    head = explained["chain_plan"]["chain"][0]
    assert entry["output"]["attempted_model"] == head["model"]
    assert entry["output"]["attempted_provider"] == head["provider"]
    assert entry["chain_plan"]["chain"] == explained["chain_plan"]["chain"]
    assert entry["task"] == _TRIVIAL_TASK

    # ``tail`` bounds the window for the browser AND has a real Python default.
    # Spelled ``tail: int = Query(50, ...)`` the default an in-process caller
    # received was fastapi's Query sentinel, and DecisionLog.tail died on
    # ``-Query(...)`` — a TypeError out of the one surface that promises never to
    # raise, invisible to the HTTP layer that substitutes the default itself. Both
    # halves are asserted, because losing the bound to fix the default is no fix.
    for _ in range(3):
        asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    assert len(asyncio.run(plugin_api.api_log(tail=2))["entries"]) == 2
    assert asyncio.run(plugin_api.api_log()) == {"entries": log.tail(50)}
    assert inspect.signature(plugin_api.api_log).parameters["tail"].default == 50
    # The bound as the BROWSER is told it, read off the mounted schema rather than
    # off the parameter object, so this passes only while the HTTP contract holds.
    app = FastAPI()
    app.include_router(plugin_api.router)
    declared = next(
        parameter
        for parameter in app.openapi()["paths"]["/log"]["get"]["parameters"]
        if parameter["name"] == "tail"
    )
    assert declared["required"] is False
    assert declared["schema"]["default"] == 50
    assert (declared["schema"]["minimum"], declared["schema"]["maximum"]) == (1, 500)


# ── The router/ vintages this file can be deployed beside ─────────────
#
# dashboard/ and router/ are deployed by FILE COPY, so this module can land next
# to a router/ that predates any helper it delegates to. That is why each
# delegation has a local mirror — and why the mirrors have to be exercised: an
# unexercised mirror is a second implementation of the composition that produced
# the missing clock in the first place, and nobody would know if it drifted.

# The helpers this surface delegates to RouterService for.
_DELEGATED = (
    "_resolve_prompt", "_explain_features", "_explain_decision",
    "_chain_plan_of", "_evaluated_at", "_preview_note",
)

# task, at, prompt_text — one input sized from the goal and one from a composed
# prompt, so both arms of the mirrored prompt resolution are compared.
_MIRROR_INPUTS = (
    (_TRIVIAL_TASK, _PEAK, None),
    (_TRIVIAL_GOAL, _PEAK, _COMPOSED),
)


@pytest.mark.parametrize("service_clock", [True, False],
                         ids=["with_clock_helper", "without_clock_helper"])
def test_plugin_mirrors_agree_with_the_helpers_they_mirror(
    plugin_config, monkeypatch, service_clock
):
    """The local mirrors must answer exactly what the delegates answer.

    "Behaviourally equivalent" is the claim the mirrors are documented with, and
    it is the only thing that makes them safe: an operator on an older router/
    must not be shown a different plan from the one this surface shows on a
    current one. Asserted against the delegated payload rather than against
    literals, so the mirror cannot pass by agreeing with a copy of itself.

    The one deliberate difference is the preview NOTE, which degrades to the two
    keys this route resolved itself — an absent ``sized_from`` renders as
    "undefined" and reads as a preview that measured nothing at all.
    """
    plugin_config(_TIME_KEYED)
    delegated = [
        asyncio.run(plugin_api.api_explain(task=task, at=at, prompt_text=prompt))
        for task, at, prompt in _MIRROR_INPUTS
    ]

    for name in _DELEGATED:
        monkeypatch.delattr(RouterService, name)
    if not service_clock:
        # A router/ predating the time layer entirely: the clock features are the
        # EDGE's job, so their absence downstream may not make this endpoint
        # time-blind again.
        monkeypatch.delattr(plugin_api._service_mod, "_clock_features")

    for (task, at, prompt), expected in zip(_MIRROR_INPUTS, delegated):
        mirrored = asyncio.run(
            plugin_api.api_explain(task=task, at=at, prompt_text=prompt)
        )
        assert mirrored["preview"] == {
            "sized_from": expected["preview"]["sized_from"],
            "prompt_chars": expected["preview"]["prompt_chars"],
        }
        for key, value in expected.items():
            if key == "preview":
                continue
            assert mirrored[key] == value, f"the mirror disagrees on {key}"
        # Two time-blind answers would also "agree", so pin that the clock still
        # reached the planner through the mirrored composition.
        assert mirrored["chain_plan"]["time_agnostic"] is False
        assert mirrored["evaluated_at"]["utc_hour"] == 7


def test_plugin_mirror_refuses_an_unusable_prompt_in_the_same_words(plugin_config):
    """Both vintages refuse a non-string prompt identically.

    The refusal is the service's when the service has one, and the mirror's when
    it does not. A mirror that coerced instead would size the turn from ``str(17)``
    and answer confidently about a plan that never existed.
    """
    plugin_config(_CONTEXT_KEYED)
    with pytest.raises(HTTPException) as delegated:
        asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=17))

    with pytest.MonkeyPatch.context() as patch:
        patch.delattr(RouterService, "_resolve_prompt")
        with pytest.raises(HTTPException) as mirrored:
            asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=17))

    assert mirrored.value.status_code == delegated.value.status_code == 400
    assert str(mirrored.value.detail) == str(delegated.value.detail)
    assert "prompt_text must be a string" in str(mirrored.value.detail)


def test_plugin_reports_that_the_clock_did_not_land_instead_of_claiming_an_hour(
    plugin_config, monkeypatch
):
    """Beside a rules.py that cannot take a clock, the two halves must agree.

    ``evaluated_at.time_aware`` is read back OFF THE PLAN, so "here is the hour I
    asked about, and no, it was not used" is a pair that cannot come apart. This
    is the honest form of the exact report the missing clock produced falsely: a
    ``cheapest_now`` tier degraded to declared order because prices could not be
    compared, which is a lie when a clock WAS injected and the truth when it was
    not.
    """
    plugin_config(_TIME_KEYED)
    for name in _DELEGATED:
        monkeypatch.delattr(RouterService, name)
    monkeypatch.setattr(plugin_api, "_EXPLAIN_ACCEPTS_RNG", False)
    monkeypatch.setattr(plugin_api, "_EXPLAIN_ACCEPTS_WHEN", False)

    result = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    plan, evaluated = result["chain_plan"], result["evaluated_at"]

    assert plan["time_agnostic"] is True
    assert evaluated["time_aware"] is False, "the plan saw no clock; say so"
    assert plan["multipliers"] == {}, "no hour, no multipliers to report"
    assert plan["strategy_declared"] == "cheapest_now"
    assert plan["strategy"] == "sequential" and plan["strategy_degraded"] is True
    assert "no clock" in plan["strategy_degraded_reason"]
    # The hour asked about is still named — an audit record without it cannot be
    # told apart from one that answered a different question.
    assert (evaluated["at"], evaluated["utc_hour"]) == (
        "2026-08-17T07:00:00+00:00", 7
    )
    # A time-keyed RULE is a feature-vector question, not a planner one, so it
    # still fires: the vector is built at the edge either way.
    hard = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_PEAK))
    assert hard["matched_rule_id"] == "defer-heavy-work-off-peak"
    assert hard["evaluated_at"]["time_aware"] is False


def test_plugin_bypasses_a_planner_helper_it_cannot_call_correctly(
    plugin_config, monkeypatch
):
    """A helper whose parameters this surface cannot satisfy is not called at all.

    Both halves of the resolution are the point. A pre-clock helper (no ``when``)
    must be BYPASSED — calling it would silently drop the clock, which is the
    original defect — while a helper that simply predates the ``features``
    argument must still be called, by keyword, against what it declares: the
    helper has grown a parameter once already, and a positional call would turn
    the next such addition into a TypeError inside a read path.
    """
    plugin_config(_TIME_KEYED)
    real = RouterService._explain_decision
    delegated = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))

    pre_clock_calls = []

    def pre_clock(task, features, config):
        # Never reached; recorded so the assertion below can prove that.
        pre_clock_calls.append(task)
        return {}

    monkeypatch.setattr(RouterService, "_explain_decision", staticmethod(pre_clock))
    assert asyncio.run(
        plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK)
    ) == delegated
    assert pre_clock_calls == [], "a clockless helper must not plan for this surface"

    # Keyword-ONLY on purpose: a positional call would raise TypeError here, which
    # is what pins that the plugin calls this helper against its declared names.
    pre_features_calls = []

    def pre_features(*, task, config, when):
        pre_features_calls.append(task)
        return real(task=task, config=config, when=when,
                    features=RouterService._explain_features(task, when))

    monkeypatch.setattr(RouterService, "_explain_decision",
                        staticmethod(pre_features))
    assert asyncio.run(
        plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK)
    ) == delegated
    assert pre_features_calls == [_TRIVIAL_TASK]


@pytest.mark.parametrize("helper, unusable", [
    ("_resolve_prompt", "not a (text, sized_from) pair"),
    ("_explain_features", ["not", "a", "feature", "vector"]),
    ("_chain_plan_of", None),
    ("_evaluated_at", "not a mapping"),
])
def test_plugin_ignores_a_helper_answering_in_a_shape_it_cannot_use(
    plugin_config, monkeypatch, helper, unusable
):
    """An unusable answer degrades to the mirror, not to a broken panel.

    Every delegation is type-guarded because the helper on the other side belongs
    to a separately deployed file. The guard is only worth having if what it
    degrades TO is the same answer, so that is what is asserted.
    """
    plugin_config(_TIME_KEYED)
    delegated = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    ))

    if helper == "_resolve_prompt":  # an instance method, not a static one
        monkeypatch.setattr(
            RouterService, helper, lambda self, task, prompt_text: unusable
        )
    else:
        monkeypatch.setattr(RouterService, helper,
                            staticmethod(lambda *args: unusable))

    assert asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    )) == delegated


def test_plugin_injects_the_clock_even_when_the_service_helper_is_unusable(
    plugin_config, monkeypatch
):
    """The clock features are the edge's contribution and cannot be lost downstream.

    ``signals.extract()`` is pure, so ``utc_hour`` exists in the vector only
    because this edge puts it there. A service whose ``_clock_features`` answers in
    a shape this surface cannot use does not get to make the endpoint time-blind
    again — that is precisely the state a time-keyed rule was inert in.
    """
    plugin_config(_TIME_KEYED)
    delegated = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_PEAK))

    monkeypatch.delattr(RouterService, "_explain_features")
    monkeypatch.setattr(plugin_api._service_mod, "_clock_features",
                        lambda when: "not a mapping")

    result = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_PEAK))
    assert result == delegated
    assert result["matched_rule_id"] == "defer-heavy-work-off-peak"
    assert result["evaluated_at"]["utc_hour"] == 7


def test_plugin_preview_note_always_names_the_text_it_measured(
    plugin_config, monkeypatch
):
    """Whatever the note helper is, the preview says which text produced the plan.

    A note that predates the two size keys keeps its own material and gains
    them; a note this surface cannot use at all degrades to exactly the two keys
    this route resolved itself. The failure both cases exist to prevent is the
    same: a preview sized from the goal line is byte-shaped like one sized from
    the real turn, so an unlabelled note answers a different question invisibly.
    """
    plugin_config(_CONTEXT_KEYED)

    monkeypatch.setattr(RouterService, "_preview_note", staticmethod(
        lambda decision, plan: {"seed": 0, "reproducible_within": "utc_hour"}
    ))
    older = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    ))
    assert older["preview"] == {
        "seed": 0,
        "reproducible_within": "utc_hour",
        "sized_from": "prompt_text",
        "prompt_chars": len(_COMPOSED),
    }

    monkeypatch.setattr(RouterService, "_preview_note",
                        staticmethod(lambda *args: "not a note"))
    unusable = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    ))
    assert unusable["preview"] == {
        "sized_from": "prompt_text", "prompt_chars": len(_COMPOSED),
    }
    # The rest of the answer is the delegated one either way.
    assert older["matched_rule_id"] == unusable["matched_rule_id"] == (
        "huge-context-read"
    )


# ── The two module layouts this file is deployed into ─────────────────


def test_plugin_binds_the_sibling_router_under_a_package_layout(tmp_path, monkeypatch):
    """Imported as a package, the plugin must bind its SIBLING router modules.

    Resolving the absolute ``router`` name first did technically work under
    Hermes's ``hermes_plugins.<slug>`` shape — the sys.path insertion at the top
    of the module makes the plugin root importable — but it bound a SECOND,
    independent copy of the router package under the top-level name, so this read
    path saw different module-level state (its own rule caches, its own breaker)
    than the write path did. Two copies of one router is the same defect as two
    views of one decision, so the binding is asserted instead of assumed.

    The insertion itself is asserted here too, in the layout that needs it: the
    plugin root has to become importable EXACTLY once, since a duplicate sys.path
    entry is another way a second copy appears.
    """
    package = tmp_path / "hermes_plugins_probe"
    (package / "dashboard").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "dashboard" / "__init__.py").write_text("", encoding="utf-8")
    # Symlinked, not copied: the file under test must be the one that ships.
    (package / "dashboard" / "plugin_api.py").symlink_to(
        ROOT / "dashboard" / "plugin_api.py"
    )
    (package / "router").symlink_to(ROOT / "router", target_is_directory=True)

    plugin_dir = str(plugin_api._PLUGIN_DIR)
    monkeypatch.setattr(
        sys, "path", [entry for entry in sys.path if entry != plugin_dir]
    )
    sys.path.insert(0, str(tmp_path))
    try:
        packaged = importlib.import_module(
            "hermes_plugins_probe.dashboard.plugin_api"
        )
        sibling = importlib.import_module("hermes_plugins_probe.router.service")
        assert packaged.RouterService is sibling.RouterService
        assert packaged.RouterService is not RouterService, (
            "the top-level copy is a different module with its own state"
        )
        assert sys.path.count(plugin_dir) == 1, (
            "the plugin root must become importable exactly once"
        )
    finally:
        for name in [n for n in sys.modules if n.startswith("hermes_plugins_probe")]:
            del sys.modules[name]


def test_plugin_makes_its_own_plugin_root_importable_in_the_flat_layout(monkeypatch):
    """In the shipped flat layout the absolute import needs that sys.path entry.

    ``dashboard`` is itself top-level there, so ``..router`` is beyond the
    top-level package and ``router`` is the only name that can resolve — which it
    can only do while the plugin root is on sys.path. Asserted by importing this
    module with the entry REMOVED, which is how it arrives in the dashboard's
    plugin loader: it must put the entry back, exactly once (a duplicate entry is
    one of the ways a second copy of the router package appears), and still bind
    the very modules the write path uses rather than fresh ones.

    Reloaded rather than imported under a second name on purpose: a second name
    would BE the duplicate-copy defect this test exists to rule out.
    """
    plugin_dir = str(plugin_api._PLUGIN_DIR)
    monkeypatch.setattr(
        sys, "path", [entry for entry in sys.path if entry != plugin_dir]
    )

    reloaded = importlib.reload(plugin_api)

    assert sys.path.count(plugin_dir) == 1, "the plugin root must be reachable again"
    assert reloaded is plugin_api
    assert reloaded.RouterService is RouterService, (
        "one router package for both paths, not a second top-level copy"
    )
    assert reloaded.DecisionLog is DecisionLog
    assert reloaded._CONFIG_PATH == reloaded._PLUGIN_DIR / "router.yaml"


def test_every_dashboard_id_site_names_the_same_plugin():
    """The bundle, the manifest and the API mount must agree. They did not.

    Three-way split, verified: `dashboard/manifest.json` said
    `hermes-smart-router`, `plugin_api.py`'s docstring said the same, and
    `dist/index.js` said `delegate-profile` in three places — the API prefix it
    fetches, the id it registers under, and its header comment. Commit `40f533d`
    renamed three of the four and left the bundle.

    Under any host derivation at least one half was broken: `21dc5d1` records that
    the dashboard filters the manifest `name` against the enabled plugin set, so a
    bundle registering another id is either not served or serves a panel whose every
    fetch 404s.

    Asserted as agreement between the files rather than against a literal, so the
    next rename cannot leave one behind. `plugin.yaml:name` and the TOOL name are
    deliberately NOT part of this set — the migration doc keeps both as
    `delegate-profile`, and the host derives the dashboard id from the manifest.
    """
    import json as _json
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    manifest = _json.loads(
        (root / "dashboard" / "manifest.json").read_text(encoding="utf-8")
    )
    name = manifest["name"]
    bundle = (root / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")

    assert f'"/api/plugins/{name}"' in bundle, (
        f"the bundle does not fetch /api/plugins/{name} — the manifest name"
    )
    assert f'register("{name}"' in bundle, (
        f"the bundle does not register under {name!r}"
    )
    # And the retired spelling appears at NEITHER id site.
    for retired_site in ('"/api/plugins/delegate-profile"',
                         'register("delegate-profile"'):
        assert retired_site not in bundle, retired_site

    # The API module documents the same mount point.
    api_doc = (root / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    assert f"/api/plugins/{name}/" in api_doc


def test_the_dashboard_log_card_does_not_call_a_dry_run_a_decision_log():
    """`GET /log` serves SIMULATIONS, and the card was titled "Decision Log".

    Its only writer is `_explain_payload` — one entry per Stage-0 dry run, never a
    dispatched turn — rendered with the same `cause=`/`rule=`/`→ model` shape as a
    real trace line. `PRODUCT.md:60` forbids that verbatim and `:70` reserves
    "decision log" for `routes.jsonl`. The old empty state ("No routing decisions
    yet") was the COMMON case and affirmatively false on a busy router.
    """
    import re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    bundle = (root / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")
    # Comments stripped first — this file's own comments quote the retired strings
    # in order to record why they are retired, which is the same reason
    # tests/test_console_logic.js strips them before its literal scans.
    rendered = re.sub(r"//[^\n]*", "", bundle)

    # It still reads the endpoint...
    assert "/log?tail=" in rendered
    # ...and no longer claims to be the decision log.
    assert "Decision Log" not in rendered
    assert "No routing decisions yet" not in rendered
    # It says what it IS, and where the real thing lives.
    assert "Simula" in rendered
    assert "routes.jsonl" in rendered


def test_the_only_writer_of_the_dashboard_log_is_the_dry_run():
    """The premise of the test above, asserted against the code rather than assumed."""
    import re
    from pathlib import Path as _Path

    src = (_Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py").read_text(
        encoding="utf-8"
    )
    writers = re.findall(r"^\s*_log\.record\(", src, re.M)
    assert len(writers) == 1, f"expected exactly one writer, found {len(writers)}"
    # And it sits inside the explain payload builder, not on any dispatch path.
    explain_start = src.index("def _explain_payload")
    nxt = src.find("\ndef ", explain_start + 1)
    assert "_log.record(" in src[explain_start:nxt if nxt > 0 else len(src)]
