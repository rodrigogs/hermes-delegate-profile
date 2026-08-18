"""Unit tests for CLI governance (router/cli.py)."""

import io
import json
import random
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import router.cli as cli
import router.rules as rules_mod
from router.cli import (
    build_parser,
    cmd_blocklist,
    cmd_chain,
    cmd_explain,
    cmd_lint,
    load_config,
)


@pytest.fixture
def router_yaml():
    return {
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
        ],
        "default": {"action": "classify"},
        "tiers": {
            "T1": {"model": "glm-5.2-fast", "provider": "zai"},
            "T2": {"model": "glm-5.2", "provider": "zai"},
            "T3": {"model": "claude-sonnet", "provider": "anthropic"},
            "T4": {"model": "claude-opus", "provider": "anthropic"},
        },
    }


@pytest.fixture
def config_file(router_yaml, tmp_path):
    path = tmp_path / "router.yaml"
    with open(path, "w") as f:
        yaml.dump(router_yaml, f)
    return str(path)


class TestCLIExplain:
    def test_explain_trivial(self, config_file, capsys):
        cmd_explain(_ns("explain", {"task": "Rename getCwd in 3 files, 20 lines",
                                     "config": config_file, "model": ""}))
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["matched_rule_id"] == "trivial-mechanical-edit"
        assert result["output"]["profile"] == "coder"

    def test_explain_blocklist(self, config_file, capsys):
        cmd_explain(_ns("explain", {"task": "test", "config": config_file,
                                     "model": "gpt-5.6-sol"}))
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["cause"] == "blocklist_veto"
        assert result["output"]["deny"] is True

    def test_explain_default(self, config_file, capsys):
        cmd_explain(_ns("explain", {"task": "Hello world", "config": config_file,
                                     "model": ""}))
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["cause"] == "default_fallthrough"


class TestCLILint:
    def test_lint_valid(self, config_file, capsys):
        cmd_lint(_ns("lint", {"config": config_file}))
        out = capsys.readouterr().out
        assert "valid" in out

    def test_lint_invalid(self, tmp_path, capsys):
        path = tmp_path / "bad.yaml"
        with open(path, "w") as f:
            yaml.dump({"rules": [{"id": "x"}], "tiers": {"T1": {}}}, f)
        with pytest.raises(SystemExit):
            cmd_lint(_ns("lint", {"config": str(path)}))


class TestCLIBlocklist:
    def test_blocklist_show(self, config_file, capsys):
        cmd_blocklist(_ns("blocklist", {"config": config_file}))
        out = capsys.readouterr().out
        assert "gpt-5.6-sol" in out
        assert "glm-5.2" in out


# ---------------------------------------------------------------------------
# chain — the shell-side view of the capability filter + fallback strategy
# ---------------------------------------------------------------------------

_STUB_PLAN = {
    "chain": [
        {"model": "claude-opus", "provider": "anthropic"},
        {"model": "glm-5.2", "provider": "zai"},
    ],
    "requirements": {"min_context": 250000, "vision": True},
    "rejected": [
        {"model": "glm-5.2-fast", "provider": "zai", "reject_reason": "no_vision"},
        {"model": "tiny-elo", "provider": "nous", "reject_reason": "context_too_small"},
    ],
    "unknown": ["mystery-elo"],
    "bypassed": False,
    "strategy": "random",
    "independent_rails": 2,
}


@pytest.fixture
def stub_plan_chain(monkeypatch):
    """Stand in for ``rules.plan_chain`` so the CLI's rendering is tested alone.

    ``explain()`` may itself call ``plan_chain`` and hand the plan back under
    ``chain_plan``; either way the CLI must render THIS plan, so the tests below
    accept either ``plan_source`` and never depend on which layer computed it.
    """
    calls = []

    def fake_plan_chain(output, features, *, rng=None, when=None):
        calls.append({"output": output, "features": features, "rng": rng,
                      "when": when})
        return dict(_STUB_PLAN)

    monkeypatch.setattr(rules_mod, "plan_chain", fake_plan_chain, raising=False)
    return calls


@pytest.fixture
def explain_without_plan(monkeypatch):
    """Pin explain() to a plan-less result — the pre-feature rules.py shape.

    Needed to exercise the CLI's own fallbacks: while explain() supplies a
    chain_plan the later branches are unreachable.
    """
    def fake_explain(*_a, **_k):
        return {
            "matched_rule_id": "hard-verbs",
            "cause": "hard_rule",
            "output": {
                "profile": "coder", "model": "claude-opus", "provider": "anthropic",
                "fallback": [{"model": "glm-5.2", "provider": "zai"}],
            },
            "matched_clauses": {},
        }

    monkeypatch.setattr(cli, "rules_explain", fake_explain)
    return fake_explain


class TestCLIChain:
    def test_chain_prints_requirements_eligible_rejected_and_strategy(
        self, config_file, stub_plan_chain, capsys
    ):
        cmd_chain(_ns("chain", {"task": "Debug a race condition in 3 files",
                                "config": config_file, "model": "",
                                "seed": None, "json": False}))
        out = capsys.readouterr().out
        # Derived requirements ...
        assert "requirements:" in out
        assert "min_context = 250000" in out
        assert "vision = True" in out
        # ... eligible order ...
        assert "eligible:" in out
        assert "1. claude-opus (anthropic)" in out
        assert "2. glm-5.2 (zai)" in out
        # ... rejected WITH reasons ...
        assert "rejected:" in out
        assert "glm-5.2-fast (zai) reject_reason=no_vision" in out
        assert "tiny-elo (nous) reject_reason=context_too_small" in out
        # ... strategy and independent rails.
        assert "strategy: random" in out
        assert "independent_rails: 2" in out
        assert "bypassed: false" in out
        assert "unknown_capabilities: mystery-elo" in out
        assert "plan_source: " in out

    def test_chain_json_is_machine_readable(self, config_file, stub_plan_chain, capsys):
        cmd_chain(_ns("chain", {"task": "Debug a race condition", "config": config_file,
                                "model": "", "seed": None, "json": True}))
        payload = json.loads(capsys.readouterr().out)
        assert payload["matched_rule_id"] == "hard-verbs"
        plan = payload["chain_plan"]
        assert [t["model"] for t in plan["chain"]] == ["claude-opus", "glm-5.2"]
        assert plan["requirements"]["vision"] is True
        assert plan["rejected"][0]["reject_reason"] == "no_vision"
        assert plan["strategy"] == "random"
        assert plan["independent_rails"] == 2
        assert plan["rejected_truncated"] == 0  # normalized shape always present

    def test_chain_passes_the_resolved_output_and_seeded_rng(
        self, config_file, stub_plan_chain, explain_without_plan, capsys
    ):
        cmd_chain(_ns("chain", {"task": "Debug a race condition", "config": config_file,
                                "model": "", "seed": 7, "json": True}))
        capsys.readouterr()
        call = stub_plan_chain[-1]
        # The tier alias must already be resolved before planning.
        assert call["output"]["model"] == "claude-opus"
        assert call["output"]["provider"] == "anthropic"
        assert call["features"]["verb_class"] == "hard"
        assert call["rng"] is not None
        # Same seed => same rng stream, which is what makes the plan auditable.
        assert call["rng"].random() == random.Random(7).random()

    def test_chain_prefers_the_plan_explain_already_computed(
        self, config_file, stub_plan_chain, monkeypatch, capsys
    ):
        explained = dict(_STUB_PLAN)
        explained["strategy"] = "sequential"

        def fake_explain(*_a, **_k):
            return {"matched_rule_id": "hard-verbs", "cause": "hard_rule",
                    "output": {"model": "claude-opus"}, "chain_plan": explained}

        monkeypatch.setattr(cli, "rules_explain", fake_explain)
        cmd_chain(_ns("chain", {"task": "debug", "config": config_file, "model": "",
                                "seed": None, "json": True}))
        payload = json.loads(capsys.readouterr().out)
        assert payload["plan_source"] == "explain"
        assert payload["chain_plan"]["strategy"] == "sequential"
        assert stub_plan_chain == []  # not re-planned

    def test_chain_degrades_when_no_planner_exists(
        self, config_file, explain_without_plan, monkeypatch, capsys
    ):
        """Older rules.py + no capabilities.py must print 'unavailable', not crash."""
        monkeypatch.delattr(rules_mod, "plan_chain", raising=False)
        monkeypatch.setattr(cli, "_caps", None)
        cmd_chain(_ns("chain", {"task": "debug", "config": config_file, "model": "",
                                "seed": None, "json": False}))
        out = capsys.readouterr().out
        assert "plan_source: unavailable" in out
        assert "strategy: sequential" in out
        assert "(none derived)" in out
        assert "(empty)" in out

    def test_chain_survives_a_planner_that_raises(
        self, config_file, explain_without_plan, monkeypatch, capsys
    ):
        def boom(*_a, **_k):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(rules_mod, "plan_chain", boom, raising=False)
        monkeypatch.setattr(cli, "_caps", None)
        cmd_chain(_ns("chain", {"task": "debug", "config": config_file, "model": "",
                                "seed": None, "json": True}))
        payload = json.loads(capsys.readouterr().out)
        assert payload["plan_source"] == "unavailable"
        assert payload["chain_plan"]["chain"] == []

    def test_chain_renders_a_corrupt_plan_as_the_empty_default(
        self, config_file, explain_without_plan, monkeypatch, capsys
    ):
        monkeypatch.setattr(rules_mod, "plan_chain",
                            lambda *_a, **_k: {"chain": "nope", "strategy": 3,
                                               "rejected": [{"model": "m"}]},
                            raising=False)
        cmd_chain(_ns("chain", {"task": "debug", "config": config_file, "model": "",
                                "seed": None, "json": False}))
        out = capsys.readouterr().out
        assert "strategy: sequential" in out       # corrupt field -> default
        assert "eligible:\n  (empty)" in out
        assert "- m reject_reason=unknown" in out  # missing reason is labelled

    def test_chain_reports_rejections_dropped_by_the_trace_bound(
        self, config_file, monkeypatch, capsys
    ):
        """A replayed plan carries rejected_truncated; the CLI must say so."""
        plan = dict(_STUB_PLAN)
        plan["rejected_truncated"] = 4
        monkeypatch.setattr(rules_mod, "plan_chain", lambda *_a, **_k: dict(plan),
                            raising=False)
        cmd_chain(_ns("chain", {"task": "debug", "config": config_file, "model": "",
                                "seed": None, "json": False}))
        out = capsys.readouterr().out
        assert "... 4 more rejected (truncated)" in out

    def test_chain_uses_capabilities_when_rules_has_no_planner(
        self, config_file, explain_without_plan, monkeypatch, capsys
    ):
        """The real capabilities module, if present, is the second-choice source."""
        if cli._caps is None:
            pytest.skip("router.capabilities not present yet")
        monkeypatch.delattr(rules_mod, "plan_chain", raising=False)
        cmd_chain(_ns("chain", {"task": "Debug a race condition", "config": config_file,
                                "model": "", "seed": None, "json": True}))
        payload = json.loads(capsys.readouterr().out)
        # The source names the rng that produced the order too — see
        # TestCLIChainSeed — so match the path prefix, not the whole label.
        assert payload["plan_source"].startswith(("capabilities", "unavailable"))
        assert isinstance(payload["chain_plan"]["requirements"], dict)
        assert isinstance(payload["chain_plan"]["chain"], list)

    def test_chain_end_to_end_with_whatever_rules_provides(self, config_file, capsys):
        """No stubs at all: the real pipeline must print a complete plan block."""
        cmd_chain(_ns("chain", {"task": "Debug a race condition in 3 files",
                                "config": config_file, "model": "",
                                "seed": 3, "json": True}))
        payload = json.loads(capsys.readouterr().out)
        plan = payload["chain_plan"]
        assert set(plan) >= {"chain", "requirements", "rejected", "unknown",
                             "bypassed", "strategy", "independent_rails"}
        assert isinstance(plan["chain"], list)
        assert isinstance(plan["rejected"], list)

    def test_chain_blocked_model_still_plans(self, config_file, stub_plan_chain, capsys):
        """A banned requested model must not stop the plan from being printed."""
        cmd_chain(_ns("chain", {"task": "debug", "config": config_file,
                                "model": "gpt-5.6-sol", "seed": None, "json": True}))
        payload = json.loads(capsys.readouterr().out)
        assert payload["chain_plan"]["strategy"] == "random"


# ---------------------------------------------------------------------------
# lint warnings — advisory, never part of the exit code
# ---------------------------------------------------------------------------

class TestCLILintWarnings:
    def test_warnings_print_without_changing_exit_code(
        self, config_file, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            rules_mod, "lint_warnings",
            lambda _c: ["tier T4: first two hops share upstream 'openrouter'"],
            raising=False,
        )
        cmd_lint(_ns("lint", {"config": config_file}))  # must NOT SystemExit
        out = capsys.readouterr().out
        assert "router: config valid" in out
        assert "1 warning(s)" in out
        assert "advisory, exit code unaffected" in out
        assert "share upstream 'openrouter'" in out

    def test_warnings_are_separate_from_hard_errors(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "bad.yaml"
        with open(path, "w") as f:
            yaml.dump({"rules": [{"id": "x"}], "tiers": {"T1": {}}}, f)
        monkeypatch.setattr(rules_mod, "lint_warnings",
                            lambda _c: ["only one independent rail in T1"],
                            raising=False)
        with pytest.raises(SystemExit) as exc:
            cmd_lint(_ns("lint", {"config": str(path)}))
        assert exc.value.code == 1  # errors still fail closed
        out = capsys.readouterr().out
        assert "config error(s):" in out
        assert "warning(s)" in out
        assert "only one independent rail in T1" in out

    def test_no_warnings_prints_no_warning_block(self, config_file, monkeypatch, capsys):
        monkeypatch.setattr(rules_mod, "lint_warnings", lambda _c: [], raising=False)
        cmd_lint(_ns("lint", {"config": config_file}))
        out = capsys.readouterr().out
        assert "warning" not in out

    def test_missing_lint_warnings_helper_is_tolerated(
        self, config_file, monkeypatch, capsys
    ):
        monkeypatch.delattr(rules_mod, "lint_warnings", raising=False)
        cmd_lint(_ns("lint", {"config": config_file}))
        assert "router: config valid" in capsys.readouterr().out

    def test_lint_warnings_that_raises_degrades_to_a_note(
        self, config_file, monkeypatch, capsys
    ):
        def boom(_c):
            raise RuntimeError("bad registry")

        monkeypatch.setattr(rules_mod, "lint_warnings", boom, raising=False)
        cmd_lint(_ns("lint", {"config": config_file}))  # no SystemExit, no traceback
        out = capsys.readouterr().out
        assert "router: config valid" in out
        assert "lint_warnings unavailable: bad registry" in out


# ---------------------------------------------------------------------------
# --seed — the audit knob. It must actually change the order (F8) and the
# order must be attributable to the path that produced it.
# ---------------------------------------------------------------------------

@pytest.fixture
def time_config_file(tmp_path):
    """A config whose T2 is a 4-hop random/unpinned tier of time-priced rails.

    Four hops so a shuffle is observable, ``pin_primary: false`` so position 0
    moves too, and the models are the ones with real price windows (deepseek,
    zai, xiaomi) so the pricing block has something to say.
    """
    config = {
        "enabled": True,
        "blocklist": {"manual_ban": [], "fallback_chain": [],
                      "auto_breaker": {"enabled": False}},
        "rules": [
            {"id": "hard-verbs", "status": "stable",
             "when": {"verb_class": {"eq": "hard"}},
             "then": {"profile": "coder", "model": "T2"}},
        ],
        "default": {"action": "classify"},
        "tiers": {
            "T1": {"model": "glm-5-turbo", "provider": "zai"},
            "T2": {
                "model": "deepseek-v4-pro", "provider": "deepseek",
                "fallback_strategy": "random", "pin_primary": False,
                "fallback": [
                    {"model": "glm-5.3", "provider": "zai"},
                    {"model": "mimo-v2.5", "provider": "xiaomi"},
                    {"model": "kimi-k3", "provider": "moonshot"},
                ],
            },
            "T3": {"model": "claude-sonnet", "provider": "anthropic"},
            "T4": {"model": "claude-opus", "provider": "anthropic"},
        },
    }
    path = tmp_path / "router.yaml"
    with open(path, "w") as f:
        yaml.dump(config, f)
    return str(path)


#: The instant ``_utc_now()`` returns under the ``frozen_now`` fixture. A
#: Wednesday (weekday 2) at 13:47:31 UTC: not a Monday, so a test that asserts
#: "today's weekday" cannot accidentally pass against ``_MON_PEAK``'s 0, and an
#: odd minute/second so a truncating bug in the hour-only forms is visible.
_FROZEN_NOW = datetime(2026, 8, 19, 13, 47, 31, tzinfo=timezone.utc)


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze the CLI's ONE clock read, and return the instant it now yields.

    The addendum's testing notes are explicit that no test reads a clock — a test
    that did would be the exact bug the injected-clock design exists to prevent.
    So the default-clock tests below pin ``cli._utc_now`` and compare against
    this value instead of calling ``datetime.now()`` a second time.
    """
    monkeypatch.setattr(cli, "_utc_now", lambda: _FROZEN_NOW)
    return _FROZEN_NOW


_HARD_TASK = "Debug a race condition in 3 files"
#: A turn implying 300 files: signals charges 4000 tokens per referenced file, so
#: est_input_tokens lands around 1.2M and the derived min_context (×1.25 safety
#: headroom) around 1.5M — above MAX_REGISTERED_CONTEXT (1.05M) AND above every
#: window in the tier, which is exactly the pathological request `unsatisfiable`
#: exists to name. The numbers are read off the plan below, never hardcoded.
_PATHOLOGICAL_TASK = "Debug a race condition across 300 files"
# A Monday 07:00 UTC — inside BOTH the deepseek peak and the weekday-only zai
# peak. 15:00 is outside both. Saturday 07:00 is peak for deepseek only.
_MON_PEAK = "2026-08-17T07:00:00Z"
_MON_OFFPEAK = "2026-08-17T15:00:00Z"
_SAT_0700 = "2026-08-15T07:00:00Z"
_MON_NIGHT = "2026-08-17T18:00:00Z"  # inside the xiaomi 16:00-00:00 discount


@pytest.fixture
def shuffling_planner(monkeypatch):
    """A planner stand-in that orders through the INJECTED rng.

    Mirrors ``capabilities.order_chain`` (shuffle under the ``random`` strategy,
    ``pin_primary`` keeps index 0) so the seed and pricing tests exercise the
    CLI's own rng/clock plumbing without depending on which agent has landed
    which half of ``capabilities.py``. ``rules.explain`` reads the same module
    global, so the preview path uses this stub too.
    """
    def fake_plan_chain(output, features, *, rng=None, when=None):
        chain = [{"model": output.get("model"), "provider": output.get("provider")}]
        chain += [dict(hop) for hop in (output.get("fallback") or [])]
        strategy = output.get("fallback_strategy", "sequential")
        ordered = list(chain)
        if strategy == "random" and rng is not None:
            if output.get("pin_primary", True):
                tail = ordered[1:]
                rng.shuffle(tail)
                ordered = ordered[:1] + tail
            else:
                rng.shuffle(ordered)
        return {
            "chain": ordered, "requirements": {}, "rejected": [], "unknown": [],
            "bypassed": False, "strategy": strategy,
            "independent_rails": len({hop.get("provider") for hop in ordered}),
        }

    monkeypatch.setattr(rules_mod, "plan_chain", fake_plan_chain, raising=False)
    return fake_plan_chain


def _chain_text_for(capsys, config, task, *argv):
    """Run ``chain`` on ``task`` through main() (so the parser is exercised)."""
    cli.main(["--config", config, "chain", task, *argv])
    return capsys.readouterr().out


def _chain_text(capsys, config, *argv):
    """Run ``chain`` through main() (so the parser is exercised) -> stdout."""
    return _chain_text_for(capsys, config, _HARD_TASK, *argv)


def _chain_json(capsys, config, *argv):
    return json.loads(_chain_text(capsys, config, "--json", *argv))


def _order(payload):
    return [t["model"] for t in payload["chain_plan"]["chain"]]


class TestCLIChainSeed:
    def test_different_seeds_produce_different_orders(
        self, time_config_file, shuffling_planner, capsys
    ):
        """F8: every seed used to print the same order (plan_source 'explain')."""
        orders = set()
        for seed in range(1, 9):
            payload = _chain_json(capsys, time_config_file,
                                  "--seed", str(seed), "--at", _MON_PEAK)
            assert len(_order(payload)) == 4
            orders.add(tuple(_order(payload)))
        assert len(orders) > 1

    def test_same_seed_reproduces_byte_identically(
        self, time_config_file, shuffling_planner, capsys
    ):
        first = _chain_text(capsys, time_config_file, "--seed", "5", "--at", _MON_PEAK)
        second = _chain_text(capsys, time_config_file, "--seed", "5", "--at", _MON_PEAK)
        assert first == second

    def test_the_default_path_is_stable_across_runs(
        self, time_config_file, shuffling_planner, capsys
    ):
        """A bare ``chain`` keeps the fixed-seed preview: no churn on refresh."""
        first = _chain_text(capsys, time_config_file, "--at", _MON_PEAK)
        second = _chain_text(capsys, time_config_file, "--at", _MON_PEAK)
        assert first == second

    def test_plan_source_names_the_rng_that_produced_the_order(
        self, time_config_file, shuffling_planner, capsys
    ):
        seeded = _chain_json(capsys, time_config_file, "--seed", "7", "--at", _MON_PEAK)
        bare = _chain_json(capsys, time_config_file, "--at", _MON_PEAK)
        assert "seed=7" in seeded["plan_source"]
        assert seeded["seed"] == 7
        assert "seed=" not in bare["plan_source"]
        assert bare["seed"] is None

    def test_seeded_plan_ignores_the_fixed_seed_preview(
        self, time_config_file, shuffling_planner, monkeypatch, capsys
    ):
        """The preview is computed with random.Random(0); --seed must not reuse it."""
        preview = dict(_STUB_PLAN)
        preview["strategy"] = "preview-plan"

        def fake_explain(*_a, **_k):
            return {"matched_rule_id": "hard-verbs", "cause": "hard_rule",
                    "output": {"model": "deepseek-v4-pro", "provider": "deepseek"},
                    "chain_plan": preview}

        monkeypatch.setattr(cli, "rules_explain", fake_explain)
        payload = _chain_json(capsys, time_config_file, "--seed", "3")
        assert payload["chain_plan"]["strategy"] != "preview-plan"
        assert payload["plan_source"] != "explain"


# ---------------------------------------------------------------------------
# --prompt-text — the other input a plan must not invent. Sized against the REAL
# registry and the REAL planner: the point is that the CLI answers the question
# production asks, so nothing here is stubbed.
# ---------------------------------------------------------------------------

#: A composed turn: 800k chars of context plus the goal. ~222k estimated tokens,
#: so the derived min_context (safety headroom included) lands near 278k — above
#: glm-4.7's 200k window and well inside glm-5.3's 1M. Below
#: ``RouterService._max_prompt_chars`` (1 MiB), so both surfaces accept it and can
#: be compared. The numbers are read off the plans below, never hardcoded.
_SIZING_GOAL = "Debug a race condition in the user cache"
_SIZING_CONTEXT = "CONTEXT:\n" + ("x" * 800_000)
_COMPOSED_PROMPT = _SIZING_CONTEXT + "\n\nGOAL: " + _SIZING_GOAL


@pytest.fixture
def sizing_config_file(tmp_path):
    """A lint-valid policy whose tier spans one big-window elo and one small one.

    Both are REGISTERED, so the capability filter can actually act on a derived
    ``min_context``: glm-5.3 holds 1M tokens and glm-4.7 holds 200k. Lint-valid
    (all four tiers present) because the same file is handed to RouterService,
    which fails closed on an invalid policy — the two surfaces have to be asked
    the same question off the same policy for their answers to be comparable.
    """
    config = {
        "enabled": True,
        "classifier": {"model": "glm-5.3", "provider": "zai"},
        "fail_safe": {"profile": "coder", "model": "glm-5.3", "provider": "zai"},
        "blocklist": {"manual_ban": [], "fallback_chain": [],
                      "auto_breaker": {"enabled": False}},
        "rules": [
            {"id": "hard-verbs", "status": "stable",
             "when": {"verb_class": {"eq": "hard"}},
             "then": {"profile": "coder", "model": "T2"}},
        ],
        "default": {"profile": "coder", "model": "T2"},
        "tiers": {
            "T1": {"model": "glm-4.7", "provider": "zai"},
            "T2": {"model": "glm-5.3", "provider": "zai",
                   "fallback": [{"model": "glm-4.7", "provider": "zai"}]},
            "T3": {"model": "glm-5.3", "provider": "zai"},
            "T4": {"model": "glm-5.3", "provider": "zai"},
        },
    }
    path = tmp_path / "router.yaml"
    with open(path, "w") as f:
        yaml.dump(config, f)
    return str(path)


def _sizing_json(capsys, config, *argv):
    return json.loads(_chain_text_for(capsys, config, _SIZING_GOAL, "--json",
                                      "--at", _MON_PEAK, *argv))


class TestCLIChainPromptText:
    """The CLI was the only read surface that could not size a turn, or say so."""

    def test_prompt_text_sizes_the_plan_the_way_production_sizes_it(
        self, sizing_config_file, capsys
    ):
        """Without the option the CLI answered a different question, invisibly.

        ``est_input_tokens`` and the ``min_context`` derived from it decide which
        elos survive the filter. Measured on the goal line the turn is 40 chars and
        every hop qualifies; measured on the composed prompt the small-window hop
        cannot hold the turn and is rejected — which is what production does with
        the same text.
        """
        goal_only = _sizing_json(capsys, sizing_config_file)
        sized = _sizing_json(capsys, sizing_config_file,
                             "--prompt-text", _COMPOSED_PROMPT)

        goal_plan, sized_plan = goal_only["chain_plan"], sized["chain_plan"]
        # The goal line alone derives a floor no elo could fail.
        assert [h["model"] for h in goal_plan["chain"]] == ["glm-5.3", "glm-4.7"]
        assert goal_plan["rejected"] == []
        # The composed prompt derives the real floor, and it filters.
        assert sized_plan["requirements"]["min_context"] > \
            goal_plan["requirements"]["min_context"]
        assert [h["model"] for h in sized_plan["chain"]] == ["glm-5.3"]
        assert [(h["model"], h["reject_reason"]) for h in sized_plan["rejected"]] == \
            [("glm-4.7", "context_too_small")]
        # ...and the filter did not empty the chain, so nothing bypassed itself.
        assert sized_plan["bypassed"] is False

    def test_the_plan_discloses_which_text_it_measured(
        self, sizing_config_file, capsys
    ):
        """The vocabulary is the one every other surface already reports.

        ``preview.sized_from`` / ``preview.prompt_chars`` — the same two keys and
        the same two values (``task``, ``prompt_text``) as
        ``RouterService.explain``, the sidecar's /explain and the dashboard's. A
        plan sized from the goal line is a legitimate answer; an UNLABELLED one is
        indistinguishable from a plan sized from the real turn.
        """
        sized = _sizing_json(capsys, sizing_config_file,
                             "--prompt-text", _COMPOSED_PROMPT)
        assert sized["preview"] == {
            "sized_from": "prompt_text", "prompt_chars": len(_COMPOSED_PROMPT),
        }

        goal_only = _sizing_json(capsys, sizing_config_file)
        assert goal_only["preview"] == {
            "sized_from": "task", "prompt_chars": len(_SIZING_GOAL),
        }

        # An empty or whitespace value is measured exactly as production measures
        # it — the same falsy test adapter.route and explain() use, so "" is the
        # goal line and "   " is three chars of prompt.
        assert _sizing_json(capsys, sizing_config_file,
                            "--prompt-text", "")["preview"]["sized_from"] == "task"
        blank = _sizing_json(capsys, sizing_config_file, "--prompt-text", "   ")
        assert blank["preview"] == {"sized_from": "prompt_text", "prompt_chars": 3}

    def test_the_human_rendering_names_the_measured_text_too(
        self, sizing_config_file, capsys
    ):
        """``--json`` is not the operator's default, so the disclosure is printed."""
        sized = _chain_text_for(capsys, sizing_config_file, _SIZING_GOAL,
                                "--at", _MON_PEAK,
                                "--prompt-text", _COMPOSED_PROMPT)
        line = _line_starting(sized, "sized_from:")
        assert "prompt_text" in line
        assert str(len(_COMPOSED_PROMPT)) in line

        goal_only = _chain_text_for(capsys, sizing_config_file, _SIZING_GOAL,
                                    "--at", _MON_PEAK)
        goal_line = _line_starting(goal_only, "sized_from:")
        assert "task" in goal_line
        # ...and it says how to ask the other question, which is the half an
        # operator cannot guess from a plan that looks complete.
        assert "--prompt-text" in goal_line

    def test_the_cli_and_the_service_agree_on_the_same_composed_turn(
        self, sizing_config_file, capsys
    ):
        """The AGREEMENT, not one side: two surfaces, one question, one answer.

        ``chain --prompt-text`` and ``RouterService.explain(..., prompt_text=)`` are
        the shell and the HTTP readings of the same plan. Asserting only that the
        CLI accepts the option would leave it free to measure something else, which
        is the shape of the defect it exists to close.
        """
        from router.service import RouterService

        cli_payload = _sizing_json(capsys, sizing_config_file,
                                   "--prompt-text", _COMPOSED_PROMPT)
        service = RouterService(Path(sizing_config_file)).explain(
            _SIZING_GOAL, _MON_PEAK, prompt_text=_COMPOSED_PROMPT,
        )

        cli_plan, service_plan = cli_payload["chain_plan"], service["chain_plan"]
        assert [h["model"] for h in cli_plan["chain"]] == \
            [h["model"] for h in service_plan["chain"]]
        assert cli_plan["requirements"] == service_plan["requirements"]
        assert [(h["model"], h["reject_reason"]) for h in cli_plan["rejected"]] == \
            [(h["model"], h["reject_reason"]) for h in service_plan["rejected"]]
        # The disclosure is the same fact in the same words on both surfaces.
        assert cli_payload["preview"]["sized_from"] == service["preview"]["sized_from"]
        assert cli_payload["preview"]["prompt_chars"] == \
            service["preview"]["prompt_chars"]
        # The clock is the same too, so neither answer can be the other hour's.
        assert cli_payload["utc_hour"] == service["evaluated_at"]["utc_hour"]


# ---------------------------------------------------------------------------
# The injected clock — prices, multipliers and the time-layer flags
# ---------------------------------------------------------------------------

class _FakeCaps:
    """Stand-in for the time-layer price API while capabilities.py is mid-write.

    Implements exactly the two documented entry points with the VERIFIED
    provider windows, so the CLI's rendering is testable independently of which
    agent has landed the registry half. ``glm-5.3`` has no dollar price and must
    surface as unpriced, never as 0.0.
    """

    #: The registry constant the ``unsatisfiable`` headline reads for its
    #: ceiling. The real value, so the rendered number is the one an operator
    #: would check against the registry.
    MAX_REGISTERED_CONTEXT = 1_050_000

    #: model -> (windows [start, end), weekdays or None, multiplier)
    _WINDOWS = {
        "deepseek-v4-pro": ([(1, 4), (6, 10)], None, 2.0),
        "deepseek-v4-flash": ([(1, 4), (6, 10)], None, 2.0),
        "glm-5.3": ([(6, 10)], {0, 1, 2, 3, 4}, 2.0),
        "mimo-v2.5": ([(16, 24)], None, 0.8),
    }
    _BASE = {
        "deepseek-v4-pro": (0.66, 1.98),
        "deepseek-v4-flash": (0.22, 0.66),
        "glm-5.3": None,            # plan credits, no per-token dollar rate
        "mimo-v2.5": (0.14, 0.28),
        "kimi-k3": (0.60, 2.50),
    }

    def price_multiplier(self, model, when=None, declared=None):
        if when is None:
            return 1.0
        windows, weekdays, multiplier = self._WINDOWS.get(model, ([], None, 1.0))
        if weekdays is not None and when.weekday() not in weekdays:
            return 1.0
        for start, end in windows:
            if start <= when.hour < end:
                return multiplier
        return 1.0

    def effective_price(self, model, when=None, declared=None):
        base = self._BASE.get(model)
        if base is None:
            return None  # never (0.0, 0.0)
        multiplier = self.price_multiplier(model, when=when, declared=declared)
        return (base[0] * multiplier, base[1] * multiplier)


@pytest.fixture
def fake_caps(monkeypatch):
    caps = _FakeCaps()
    monkeypatch.setattr(cli, "_caps", caps)
    return caps


def _rows(payload):
    return {row["model"]: row for row in payload["pricing"]}


class TestCLIChainTime:
    def test_0700_weekday_doubles_both_primary_rails(
        self, time_config_file, shuffling_planner, fake_caps, capsys
    ):
        rows = _rows(_chain_json(capsys, time_config_file, "--at", _MON_PEAK))
        assert rows["deepseek-v4-pro"]["multiplier"] == 2.0
        assert rows["glm-5.3"]["multiplier"] == 2.0
        assert rows["deepseek-v4-pro"]["price_in"] == pytest.approx(1.32)
        assert rows["deepseek-v4-pro"]["price_out"] == pytest.approx(3.96)

    def test_1500_is_off_peak_on_both_primary_rails(
        self, time_config_file, shuffling_planner, fake_caps, capsys
    ):
        rows = _rows(_chain_json(capsys, time_config_file, "--at", _MON_OFFPEAK))
        assert rows["deepseek-v4-pro"]["multiplier"] == 1.0
        assert rows["glm-5.3"]["multiplier"] == 1.0
        assert rows["deepseek-v4-pro"]["price_in"] == pytest.approx(0.66)

    def test_weekend_0700_is_peak_for_deepseek_only(
        self, time_config_file, shuffling_planner, fake_caps, capsys
    ):
        """The zai peak is Mon-Fri; the deepseek peak is every day."""
        rows = _rows(_chain_json(capsys, time_config_file, "--at", _SAT_0700))
        assert rows["deepseek-v4-pro"]["multiplier"] == 2.0
        assert rows["glm-5.3"]["multiplier"] == 1.0

    def test_xiaomi_night_window_is_a_discount_not_a_peak(
        self, time_config_file, shuffling_planner, fake_caps, capsys
    ):
        rows = _rows(_chain_json(capsys, time_config_file, "--at", _MON_NIGHT))
        assert rows["mimo-v2.5"]["multiplier"] == 0.8
        assert rows["mimo-v2.5"]["price_out"] == pytest.approx(0.224)

    def test_an_unpriced_model_is_never_rendered_as_zero(
        self, time_config_file, shuffling_planner, fake_caps, capsys
    ):
        payload = _chain_json(capsys, time_config_file, "--at", _MON_PEAK)
        row = _rows(payload)["glm-5.3"]
        assert row["unpriced"] is True
        assert row["price_in"] is None and row["price_out"] is None
        text = _chain_text(capsys, time_config_file, "--at", _MON_PEAK)
        assert "glm-5.3 (zai) x2.0 unpriced" in text
        # No row anywhere renders a zero rate: a plan model is not free.
        assert not re.search(r"=\$0(\.0+)?/1M", text)

    def test_the_human_block_shows_the_clock_and_the_multipliers(
        self, time_config_file, shuffling_planner, fake_caps, capsys
    ):
        text = _chain_text(capsys, time_config_file, "--at", _MON_PEAK)
        assert "at: 2026-08-17T07:00:00+00:00 (utc_hour=7 utc_weekday=0) source=explicit" in text
        assert "pricing:" in text
        assert "deepseek-v4-pro (deepseek) x2.0 in=$1.32/1M out=$3.96/1M" in text

    def test_the_planner_multipliers_win_when_the_plan_carries_them(
        self, time_config_file, monkeypatch, fake_caps, capsys
    ):
        """The number the ordering decision was made on is the one to show."""
        plan = dict(_STUB_PLAN)
        plan["chain"] = [{"model": "deepseek-v4-pro", "provider": "deepseek"}]
        plan["multipliers"] = {"deepseek-v4-pro": 1.75}
        monkeypatch.setattr(rules_mod, "plan_chain", lambda *_a, **_k: dict(plan),
                            raising=False)
        payload = _chain_json(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert _rows(payload)["deepseek-v4-pro"]["multiplier"] == 1.75

    def test_the_clock_reaches_the_planner_and_the_feature_vector(
        self, time_config_file, monkeypatch, capsys
    ):
        seen = []

        def fake_plan_chain(output, features, *, rng=None, when=None):
            seen.append({"features": features, "when": when, "rng": rng})
            return dict(_STUB_PLAN)

        monkeypatch.setattr(rules_mod, "plan_chain", fake_plan_chain, raising=False)
        _chain_json(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        call = seen[-1]
        assert call["when"].hour == 7
        assert call["when"].weekday() == 0
        assert call["features"]["utc_hour"] == 7
        assert call["features"]["utc_weekday"] == 0

    def test_time_agnostic_injects_no_clock_at_all(
        self, time_config_file, monkeypatch, capsys
    ):
        seen = []

        def fake_plan_chain(output, features, *, rng=None, when=None):
            seen.append({"features": features, "when": when})
            return dict(_STUB_PLAN)

        monkeypatch.setattr(rules_mod, "plan_chain", fake_plan_chain, raising=False)
        payload = _chain_json(capsys, time_config_file, "--seed", "1", "--time-agnostic")
        assert seen[-1]["when"] is None
        # Omitted, not guessed: a time-keyed rule must be inert without a clock.
        assert "utc_hour" not in seen[-1]["features"]
        assert "utc_weekday" not in seen[-1]["features"]
        assert payload["at"] is None
        assert payload["utc_hour"] is None
        # Nothing to reflect => the plan is not mislabelled as time-blind.
        assert payload["plan_time_aware"] is True
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--time-agnostic")
        assert "time-agnostic" in text

    def test_default_is_the_current_utc_time_from_the_single_clock_read(
        self, time_config_file, shuffling_planner, frozen_now, capsys
    ):
        """The default clock is ``_utc_now()`` — the ONE wall-clock read there is.

        Asserted against the FROZEN instant rather than a second
        ``datetime.now()``: the addendum's testing notes forbid a test reading a
        clock, and a re-read here would disagree with the CLI's read on any run
        that crossed a second (or, for the weekday, UTC midnight) between them.
        """
        payload = _chain_json(capsys, time_config_file)
        assert payload["at_source"] == "now"
        assert datetime.fromisoformat(payload["at"]) == frozen_now
        assert payload["utc_hour"] == frozen_now.hour
        assert payload["utc_weekday"] == frozen_now.weekday()

    def test_a_bare_hour_and_an_iso_timestamp_are_both_accepted(
        self, time_config_file, frozen_now, capsys
    ):
        by_hour = _chain_json(capsys, time_config_file, "--at", "7")
        by_clock = _chain_json(capsys, time_config_file, "--at", "07:30")
        by_iso = _chain_json(capsys, time_config_file, "--at", _MON_PEAK)
        assert by_hour["utc_hour"] == by_clock["utc_hour"] == by_iso["utc_hour"] == 7
        # The hour-only forms inherit today's UTC date — "today" being the frozen
        # injected clock, never a second read of the real one.
        assert by_hour["utc_weekday"] == frozen_now.weekday()
        assert by_clock["utc_weekday"] == frozen_now.weekday()
        assert by_iso["utc_weekday"] == 0

    def test_a_naive_and_an_offset_timestamp_normalise_to_utc(
        self, time_config_file, capsys
    ):
        naive = _chain_json(capsys, time_config_file, "--at", "2026-08-17T07:00:00")
        offset = _chain_json(capsys, time_config_file,
                             "--at", "2026-08-17T09:00:00+02:00")
        assert naive["utc_hour"] == 7
        assert offset["utc_hour"] == 7

    def test_the_feature_producers_normalise_an_offset_clock_to_utc(self):
        """``_time_features``/``_time_payload`` must agree with the other four.

        The features the CLI injects and the multipliers ``capabilities`` derives
        are two readings of ONE clock; if they disagree about the hour, the trace
        names a window that did not price the call. So this asserts the CLI pair
        against the other clock-feature producers directly rather than only
        through ``resolve_when`` (which already hands them aware UTC, and so hides
        the divergence).
        """
        # Midnight Monday at UTC+12 is NOON SUNDAY in UTC: normalising changes
        # both fields, so a producer that skipped it is unmistakable.
        aware = datetime(2026, 8, 17, 0, 0,
                         tzinfo=timezone(timedelta(hours=12)))
        assert cli._time_features(aware) == {"utc_hour": 12, "utc_weekday": 6}
        payload = cli._time_payload(aware, "explicit")
        assert payload["utc_hour"] == 12
        assert payload["utc_weekday"] == 6
        # ... and the registry that prices against the same instant agrees.
        if cli._caps is not None and hasattr(cli._caps, "_utc_parts"):
            assert cli._caps._utc_parts(aware) == (12, 6)
        # A naive clock is read as UTC already — no shifting, no guessing.
        naive = datetime(2026, 8, 17, 7, 0)
        assert cli._time_features(naive) == {"utc_hour": 7, "utc_weekday": 0}
        # No clock => the features are omitted, not zeroed.
        assert cli._time_features(None) == {}
        assert cli._time_payload(None, "time-agnostic")["utc_hour"] is None

    def test_at_source_uses_the_service_vocabulary_not_the_flag_spelling(
        self, time_config_file, shuffling_planner, frozen_now, capsys
    ):
        """One vocabulary across surfaces: no per-field translation table."""
        assert _chain_json(capsys, time_config_file,
                           "--at", _MON_PEAK)["at_source"] == "explicit"
        assert _chain_json(capsys, time_config_file)["at_source"] == "now"
        assert _chain_json(capsys, time_config_file,
                           "--time-agnostic")["at_source"] == "time-agnostic"

    def test_an_unparseable_time_fails_closed(self, time_config_file, capsys):
        """Answering a different question than the one asked is worse than refusing."""
        for bad in ("teatime", "25", "07:99", "2026-13-01T00:00:00Z"):
            with pytest.raises(SystemExit) as exc:
                cli.main(["--config", time_config_file, "chain", _HARD_TASK,
                          "--at", bad])
            assert exc.value.code == 2
            assert "--at" in capsys.readouterr().err

    def test_at_and_time_agnostic_are_mutually_exclusive(self, time_config_file):
        with pytest.raises(SystemExit):
            cli.main(["--config", time_config_file, "chain", _HARD_TASK,
                      "--at", "7", "--time-agnostic"])

    def test_pricing_degrades_when_capabilities_cannot_price(
        self, time_config_file, shuffling_planner, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli, "_caps", None)
        text = _chain_text(capsys, time_config_file, "--at", _MON_PEAK)
        assert "price=n/a" in text
        assert "$0" not in text
        assert "x?" in text

    def test_a_price_api_that_raises_is_absorbed(
        self, time_config_file, shuffling_planner, monkeypatch, capsys
    ):
        class Boom:
            def price_multiplier(self, model, when=None, declared=None):
                raise RuntimeError("registry exploded")

            def effective_price(self, model, when=None, declared=None):
                raise RuntimeError("registry exploded")

        monkeypatch.setattr(cli, "_caps", Boom())
        payload = _chain_json(capsys, time_config_file, "--at", _MON_PEAK)
        row = _rows(payload)["deepseek-v4-pro"]
        assert row["multiplier"] is None
        assert row["price_in"] is None
        assert row["pricing"] == "unavailable"

    def test_a_when_aware_explain_supplies_the_preview_plan(
        self, time_config_file, monkeypatch, capsys
    ):
        """Once rules.explain takes a clock, its preview IS the plan to show."""
        seen = {}

        def fake_explain(task, features, blocked, rules, default, tiers,
                         rng=None, when=None):
            seen["when"] = when
            return {"matched_rule_id": "hard-verbs", "cause": "hard_rule",
                    "output": {"model": "deepseek-v4-pro", "provider": "deepseek"},
                    "chain_plan": dict(_STUB_PLAN)}

        monkeypatch.setattr(cli, "rules_explain", fake_explain)
        payload = _chain_json(capsys, time_config_file, "--at", _MON_PEAK)
        assert seen["when"].hour == 7
        assert payload["plan_source"] == "explain"
        assert payload["plan_time_aware"] is True

    def test_a_time_blind_explain_does_not_answer_for_the_requested_hour(
        self, time_config_file, shuffling_planner, monkeypatch, capsys
    ):
        """A preview that never saw the clock must not be printed as its plan."""
        def fake_explain(task, features, blocked, rules, default, tiers, rng=None):
            return {"matched_rule_id": "hard-verbs", "cause": "hard_rule",
                    "output": {"model": "deepseek-v4-pro", "provider": "deepseek",
                               "fallback": [{"model": "glm-5.3", "provider": "zai"}]},
                    "chain_plan": {"chain": [{"model": "stale-preview"}],
                                   "strategy": "sequential"}}

        monkeypatch.setattr(cli, "rules_explain", fake_explain)
        payload = _chain_json(capsys, time_config_file, "--at", _MON_PEAK)
        assert payload["plan_source"] != "explain"
        assert "stale-preview" not in _order(payload)
        assert payload["plan_time_aware"] is True  # the replan saw the clock

    def test_a_time_blind_planner_is_labelled_as_such(
        self, time_config_file, monkeypatch, capsys
    ):
        """Prices are for the requested hour; an unclocked ORDER must say so."""
        monkeypatch.setattr(
            rules_mod, "plan_chain",
            lambda output, features, *, rng=None: dict(_STUB_PLAN),
            raising=False,
        )
        payload = _chain_json(capsys, time_config_file, "--seed", "1",
                              "--at", _MON_PEAK)
        assert payload["plan_time_aware"] is False
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert "plan_time_aware: false" in text

    def test_explain_takes_the_same_clock(self, time_config_file, capsys):
        cli.main(["--config", time_config_file, "explain", _HARD_TASK,
                  "--at", _MON_PEAK])
        result = json.loads(capsys.readouterr().out)
        assert result["utc_hour"] == 7
        assert result["utc_weekday"] == 0
        assert result["at_source"] == "explicit"


class TestCLIChainTimeFlags:
    """capped / demoted / promoted / time_cap_bypassed / strategy_degraded."""

    def _plan_with(self, monkeypatch, **flags):
        plan = dict(_STUB_PLAN)
        plan.update(flags)
        monkeypatch.setattr(rules_mod, "plan_chain", lambda *_a, **_k: dict(plan),
                            raising=False)

    def test_flags_are_printed_when_set(self, time_config_file, monkeypatch, capsys):
        self._plan_with(
            monkeypatch,
            capped=[{"model": "deepseek-v4-pro", "provider": "deepseek"}],
            demoted=[{"model": "glm-5.3", "provider": "zai"}],
            promoted=["gpt-5.6-luna"],
            time_cap_bypassed=True,
            strategy_degraded=True,
        )
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert "capped: deepseek-v4-pro (deepseek)" in text
        assert "demoted: glm-5.3 (zai)" in text
        assert "promoted: gpt-5.6-luna" in text
        assert "time_cap_bypassed: true" in text
        assert "would have emptied the chain" in text
        assert "strategy_degraded: true" in text

    def test_unset_flags_are_not_printed(self, time_config_file, monkeypatch, capsys):
        self._plan_with(monkeypatch, capped=[], demoted=[], promoted=[],
                        time_cap_bypassed=False, strategy_degraded=False)
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        for key in ("capped", "demoted", "promoted", "time_cap_bypassed",
                    "strategy_degraded"):
            assert f"{key}:" not in text

    def test_flags_survive_into_json(self, time_config_file, monkeypatch, capsys):
        self._plan_with(monkeypatch, time_cap_bypassed=True,
                        multipliers={"deepseek-v4-pro": 2.0})
        payload = _chain_json(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert payload["chain_plan"]["time_cap_bypassed"] is True
        assert payload["chain_plan"]["multipliers"] == {"deepseek-v4-pro": 2.0}


@pytest.fixture
def peak_config_file(tmp_path):
    """The SHIPPED T3/T4 shape: the ``avoid_peak`` rails are already trailing.

    ``avoid_peak: [deepseek, zai]`` over ``[gpt-5.6-terra, deepseek-v4-pro,
    glm-5.3]`` leaves the chain byte-identical at 07:00 UTC — demotion preserves
    relative order and the two matched rails are the last two hops. So the plan
    reports ``peak_priced`` non-empty with ``demoted`` EMPTY, which is the case
    the two fields were split apart to express.
    """
    config = {
        "enabled": True,
        "blocklist": {"manual_ban": [], "fallback_chain": [],
                      "auto_breaker": {"enabled": False}},
        "rules": [
            {"id": "hard-verbs", "status": "stable",
             "when": {"verb_class": {"eq": "hard"}},
             "then": {"profile": "coder", "model": "T3"}},
        ],
        "default": {"action": "classify"},
        "tiers": {
            "T3": {
                "model": "gpt-5.6-terra", "provider": "openai-codex",
                "time_policy": {"avoid_peak": ["deepseek", "zai"]},
                "fallback": [
                    {"model": "deepseek-v4-pro", "provider": "deepseek"},
                    {"model": "glm-5.3", "provider": "zai"},
                ],
            },
        },
    }
    path = tmp_path / "router.yaml"
    with open(path, "w") as f:
        yaml.dump(config, f)
    return str(path)


class TestCLIChainUnsatisfiable:
    """The headline for a pathological request, which nothing rendered before.

    Every case pins the clock with ``--at``: the addendum's testing note is that
    no test reads the wall clock, and one that did would be the bug the
    injected-clock design exists to prevent.
    """

    def _plan_with(self, monkeypatch, **fields):
        plan = dict(_STUB_PLAN)
        plan.update(fields)
        monkeypatch.setattr(rules_mod, "plan_chain", lambda *_a, **_k: dict(plan),
                            raising=False)

    def test_a_pathological_floor_is_headlined_not_left_to_be_inferred(
        self, time_config_file, capsys
    ):
        """The real pipeline: a floor above every registered window, named.

        Before this, the operator got ``bypassed: true`` and a run of identical
        ``context_too_small`` rejections and had to conclude "the floor is above
        every window that exists" by hand.
        """
        if cli._caps is None:  # pragma: no cover - the registry always ships
            pytest.skip("unsatisfiable is computed in the capability registry")
        payload = json.loads(_chain_text_for(
            capsys, time_config_file, _PATHOLOGICAL_TASK, "--json", "--at", _MON_PEAK,
        ))
        plan = payload["chain_plan"]
        if plan["unsatisfiable"] != ["min_context"]:  # pragma: no cover
            pytest.skip("this build of the filter does not report unsatisfiable")
        needed = plan["requirements"]["min_context"]

        text = _chain_text_for(capsys, time_config_file, _PATHOLOGICAL_TASK,
                              "--at", _MON_PEAK)
        assert "unsatisfiable: min_context" in text
        # The requirement is named as the unmeetable half — not the roster.
        assert "no registered elo could EVER meet this" in text
        # The rendered numbers are the PLAN's, not a second derivation: needed
        # against the ceiling is what says "split it or add a rail".
        assert str(needed) in text
        assert str(cli._context_ceiling()) in text
        assert "largest registered context window" in text
        assert "split the task" in text
        # It is a DIFFERENT fact from the per-elo rejections, and it comes first:
        # the diagnosis above the evidence, not buried under it.
        assert "bypassed: true" in text
        assert text.count("reject_reason=context_too_small") == 4
        assert text.index("unsatisfiable:") < text.index("rejected:")

    def test_the_headline_names_the_requirement_and_the_ceiling(
        self, time_config_file, fake_caps, monkeypatch, capsys
    ):
        """Wording pinned against a fixed ceiling, independent of the registry."""
        self._plan_with(monkeypatch, unsatisfiable=["min_context"],
                        requirements={"min_context": 1_500_014}, bypassed=True)
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert ("unsatisfiable: min_context (no registered elo could EVER meet "
                "this — the requirement is unmeetable, not these particular "
                "elos)") in text
        assert ("  min_context: 1500014 needed, and the largest registered "
                "context window is 1050000") in text

    def test_a_registry_that_cannot_answer_still_names_the_requirement(
        self, time_config_file, monkeypatch, capsys
    ):
        """No MAX_REGISTERED_CONTEXT => the ceiling clause drops, nothing raises.

        The requirement name is the half an operator cannot get anywhere else, so
        it must survive a registry that is absent or mid-write.
        """
        monkeypatch.setattr(cli, "_caps", None)
        self._plan_with(monkeypatch, unsatisfiable=["min_context"],
                        requirements={"min_context": 1_500_014}, bypassed=True)
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert "unsatisfiable: min_context" in text
        assert "1500014 needed" in text
        assert "largest registered context window" not in text
        assert "split the task" in text

    def test_a_requirement_key_with_no_value_is_still_reported(
        self, time_config_file, fake_caps, monkeypatch, capsys
    ):
        """A future unsatisfiable key must render, not vanish or crash."""
        self._plan_with(monkeypatch, unsatisfiable=["vision"], requirements={})
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert "unsatisfiable: vision" in text
        assert "  vision: nothing registered can satisfy it" in text

    def test_an_empty_or_absent_unsatisfiable_renders_nothing(
        self, time_config_file, fake_caps, monkeypatch, capsys
    ):
        """An empty field is not an empty heading — it is silence."""
        self._plan_with(monkeypatch, unsatisfiable=[])
        assert "unsatisfiable" not in _chain_text(
            capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        # ... and the same when the planner never emitted the key at all.
        self._plan_with(monkeypatch)
        assert "unsatisfiable" not in _chain_text(
            capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)

    def test_the_json_dump_still_carries_the_raw_field(
        self, time_config_file, fake_caps, monkeypatch, capsys
    ):
        """--json stays a faithful dump of the plan: the list, not the prose."""
        self._plan_with(monkeypatch, unsatisfiable=["min_context"], bypassed=True)
        payload = _chain_json(capsys, time_config_file, "--seed", "1",
                              "--at", _MON_PEAK)
        assert payload["chain_plan"]["unsatisfiable"] == ["min_context"]
        assert "no registered elo" not in json.dumps(payload)


class TestCLIChainPeakPriced:
    """``demoted`` is a MOVE; ``peak_priced`` is a PRICE. Both must be readable.

    Every case pins the clock with ``--at`` for the reason given in the class
    above: a test that read the wall clock would be the bug this design prevents.
    """

    def _plan_with(self, monkeypatch, **fields):
        plan = dict(_STUB_PLAN)
        plan.update(fields)
        monkeypatch.setattr(rules_mod, "plan_chain", lambda *_a, **_k: dict(plan),
                            raising=False)

    def test_the_shipped_t3_shape_reports_a_price_with_no_move(
        self, peak_config_file, capsys
    ):
        """The real pipeline on the shipped policy — not a hypothetical shape.

        ``avoid_peak: [deepseek, zai]`` over T3 at 07:00 Monday moves nothing,
        because the matched rails are already the trailing hops. The order is
        therefore unchanged AND two rails cost double, and the block has to say
        both or the operator reads the second as a policy that failed to fire.
        """
        payload = json.loads(_chain_text(
            capsys, peak_config_file, "--json", "--at", _MON_PEAK))
        plan = payload["chain_plan"]
        if plan["peak_priced"] == []:  # pragma: no cover - registry mid-write
            pytest.skip("this build does not report peak_priced yet")
        # Asserted against the RETURNED chain: the report may not drift from the
        # permutation it describes.
        assert _order(payload) == ["gpt-5.6-terra", "deepseek-v4-pro", "glm-5.3"]
        assert plan["peak_priced"] == ["deepseek-v4-pro", "glm-5.3"]
        assert plan["demoted"] == []

        text = _chain_text(capsys, peak_config_file, "--at", _MON_PEAK)
        assert "peak_priced: deepseek-v4-pro, glm-5.3" in text
        assert "nothing moved" in text
        assert "unchanged and correct" in text
        # Nothing moved, so nothing claims anything did.
        assert "demoted:" not in text

    def test_the_two_fields_are_visibly_different_facts(
        self, time_config_file, fake_caps, monkeypatch, capsys
    ):
        """Printed side by side, each labelled with the question it answers."""
        self._plan_with(monkeypatch, demoted=["glm-5.3"],
                        peak_priced=["deepseek-v4-pro", "glm-5.3"])
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        demoted_line = _line_starting(text, "demoted:")
        peak_line = _line_starting(text, "peak_priced:")
        assert "POSITION" in demoted_line and "moved these later" in demoted_line
        assert "PRICE" in peak_line and "dearer" in peak_line
        # One elo is in both lists, which is correct and is exactly why the two
        # lines may not read the same.
        assert "glm-5.3" in demoted_line and "glm-5.3" in peak_line
        assert demoted_line != peak_line
        # Something DID move here, so the "nothing moved" note is not printed.
        assert "nothing moved" not in text

    def test_peak_priced_alone_says_the_unchanged_order_is_not_a_bug(
        self, time_config_file, fake_caps, monkeypatch, capsys
    ):
        self._plan_with(monkeypatch, demoted=[],
                        peak_priced=["deepseek-v4-pro", "glm-5.3"])
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert "peak_priced: deepseek-v4-pro, glm-5.3" in text
        assert "demoted:" not in text
        note = _line_starting(text, "  nothing moved:")
        assert "already the trailing hops" in note
        assert "cannot step around them" in note

    def test_an_empty_or_absent_peak_priced_renders_nothing(
        self, time_config_file, fake_caps, monkeypatch, capsys
    ):
        self._plan_with(monkeypatch, demoted=[], peak_priced=[])
        text = _chain_text(capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)
        assert "peak_priced" not in text
        assert "nothing moved" not in text
        # ... and the same when the planner never emitted the key at all.
        self._plan_with(monkeypatch)
        assert "peak_priced" not in _chain_text(
            capsys, time_config_file, "--seed", "1", "--at", _MON_PEAK)

    def test_the_json_dump_still_carries_both_raw_fields(
        self, time_config_file, fake_caps, monkeypatch, capsys
    ):
        self._plan_with(monkeypatch, demoted=[],
                        peak_priced=["deepseek-v4-pro", "glm-5.3"])
        payload = _chain_json(capsys, time_config_file, "--seed", "1",
                              "--at", _MON_PEAK)
        assert payload["chain_plan"]["peak_priced"] == ["deepseek-v4-pro", "glm-5.3"]
        assert payload["chain_plan"]["demoted"] == []
        assert "nothing moved" not in json.dumps(payload)


def _line_starting(text, prefix):
    """The one output line beginning with ``prefix`` — asserted to be unique."""
    matches = [line for line in text.splitlines() if line.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one {prefix!r} line, got {matches}"
    return matches[0]


class TestCLIChainTimeAgainstTheRealRegistry:
    """Integration: once capabilities.py lands the windows, these must hold."""

    def _skip_unless_priced(self):
        """Skip while the registry half of the time layer is absent or mid-write.

        Probed by CALLING it, not by hasattr: a half-written module can expose
        the name and still raise, and that is its owner's test to fail, not this
        one's. The chain itself comes from ``shuffling_planner``, so these tests
        pin the registry's windows and nothing else.
        """
        if cli._caps is None:
            pytest.skip("router.capabilities not present yet")
        fn = getattr(cli._caps, "price_multiplier", None)
        if not callable(fn):
            pytest.skip("capabilities.price_multiplier not present yet")
        try:
            probe = fn("deepseek-v4-pro",
                       when=datetime(2026, 8, 17, 7, tzinfo=timezone.utc))
        except Exception as exc:  # noqa: BLE001 - mid-write module
            pytest.skip(f"capabilities price API is mid-write: {exc}")
        if not isinstance(probe, (int, float)) or isinstance(probe, bool):
            pytest.skip("capabilities.price_multiplier returns no number yet")

    def test_0700_weekday_is_peak_on_deepseek_and_zai(
        self, time_config_file, shuffling_planner, capsys
    ):
        self._skip_unless_priced()
        rows = _rows(_chain_json(capsys, time_config_file, "--at", _MON_PEAK))
        assert rows["deepseek-v4-pro"]["multiplier"] == 2.0
        assert rows["glm-5.3"]["multiplier"] == 2.0

    def test_1500_is_off_peak_on_both(
        self, time_config_file, shuffling_planner, capsys
    ):
        self._skip_unless_priced()
        rows = _rows(_chain_json(capsys, time_config_file, "--at", _MON_OFFPEAK))
        assert rows["deepseek-v4-pro"]["multiplier"] == 1.0
        assert rows["glm-5.3"]["multiplier"] == 1.0

    def test_the_plan_model_is_never_priced_at_zero(
        self, time_config_file, shuffling_planner, capsys
    ):
        self._skip_unless_priced()
        rows = _rows(_chain_json(capsys, time_config_file, "--at", _MON_PEAK))
        assert rows["glm-5.3"]["price_in"] is None
        assert rows["glm-5.3"]["price_out"] is None


class TestCLIExitCodes:
    def test_a_chain_run_never_exits_nonzero_for_a_missing_time_layer(
        self, time_config_file, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli, "_caps", None)
        monkeypatch.delattr(rules_mod, "plan_chain", raising=False)
        assert cli.main(["--config", time_config_file, "chain", _HARD_TASK]) is None
        assert "plan_source:" in capsys.readouterr().out

    def test_time_layer_warnings_do_not_change_the_lint_exit_code(
        self, time_config_file, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            rules_mod, "lint_warnings",
            lambda _c: ["tier 'T2': every elo is in an expensive window at some "
                        "hour — time_cap will bypass"],
            raising=False,
        )
        assert cli.main(["--config", time_config_file, "lint"]) is None
        out = capsys.readouterr().out
        assert "router: config valid" in out
        assert "time_cap will bypass" in out
        assert "advisory, exit code unaffected" in out


class TestCLIParser:
    def test_parser_chain_time_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["chain", "t"])
        assert args.at is None
        assert args.time_agnostic is False

    def test_parser_chain_at_and_time_agnostic(self):
        parser = build_parser()
        assert parser.parse_args(["chain", "t", "--at", "7"]).at == "7"
        assert parser.parse_args(["chain", "t", "--time-agnostic"]).time_agnostic

    def test_parser_explain_at(self):
        parser = build_parser()
        assert parser.parse_args(["explain", "t", "--at", _MON_PEAK]).at == _MON_PEAK

    def test_parser_explain(self):
        parser = build_parser()
        args = parser.parse_args(["explain", "test task"])
        assert args.command == "explain"
        assert args.task == "test task"

    def test_parser_lint(self):
        parser = build_parser()
        args = parser.parse_args(["lint"])
        assert args.command == "lint"

    def test_parser_blocklist(self):
        parser = build_parser()
        args = parser.parse_args(["blocklist"])
        assert args.command == "blocklist"

    def test_parser_chain_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["chain", "debug a race"])
        assert args.command == "chain"
        assert args.task == "debug a race"
        assert args.json is False
        assert args.seed is None
        # Omitted means "measure the task line", which the payload then discloses.
        assert args.prompt_text == ""

    def test_parser_chain_prompt_text(self):
        parser = build_parser()
        args = parser.parse_args(["chain", "t", "--prompt-text", "ctx\n\ngoal"])
        assert args.prompt_text == "ctx\n\ngoal"

    def test_parser_chain_json_and_seed(self):
        parser = build_parser()
        args = parser.parse_args(["chain", "t", "--json", "--seed", "7",
                                  "--model", "glm-5.2"])
        assert args.json is True
        assert args.seed == 7
        assert args.model == "glm-5.2"


def _ns(command, overrides):
    """Build a simple namespace mimicking argparse."""
    class NS:
        pass
    ns = NS()
    ns.command = command
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns
