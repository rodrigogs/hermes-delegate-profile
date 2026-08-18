"""Unit tests for CLI governance (router/cli.py)."""

import io
import json
import random
import re
import sys
import tempfile
from datetime import datetime, timezone
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


_HARD_TASK = "Debug a race condition in 3 files"
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


def _chain_text(capsys, config, *argv):
    """Run ``chain`` through main() (so the parser is exercised) -> stdout."""
    cli.main(["--config", config, "chain", _HARD_TASK, *argv])
    return capsys.readouterr().out


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
# The injected clock — prices, multipliers and the time-layer flags
# ---------------------------------------------------------------------------

class _FakeCaps:
    """Stand-in for the time-layer price API while capabilities.py is mid-write.

    Implements exactly the two documented entry points with the VERIFIED
    provider windows, so the CLI's rendering is testable independently of which
    agent has landed the registry half. ``glm-5.3`` has no dollar price and must
    surface as unpriced, never as 0.0.
    """

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
        assert "at: 2026-08-17T07:00:00+00:00 (utc_hour=7 utc_weekday=0) source=--at" in text
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

    def test_default_is_the_real_current_utc_time(
        self, time_config_file, shuffling_planner, capsys
    ):
        before = datetime.now(timezone.utc)
        payload = _chain_json(capsys, time_config_file)
        after = datetime.now(timezone.utc)
        at = datetime.fromisoformat(payload["at"])
        assert payload["at_source"] == "now"
        assert before <= at <= after
        assert payload["utc_hour"] == at.hour
        assert payload["utc_weekday"] == at.weekday()

    def test_a_bare_hour_and_an_iso_timestamp_are_both_accepted(
        self, time_config_file, capsys
    ):
        by_hour = _chain_json(capsys, time_config_file, "--at", "7")
        by_clock = _chain_json(capsys, time_config_file, "--at", "07:30")
        by_iso = _chain_json(capsys, time_config_file, "--at", _MON_PEAK)
        assert by_hour["utc_hour"] == by_clock["utc_hour"] == by_iso["utc_hour"] == 7
        # The hour-only forms inherit today's UTC date; ISO carries its own.
        assert by_hour["utc_weekday"] == datetime.now(timezone.utc).weekday()
        assert by_iso["utc_weekday"] == 0

    def test_a_naive_and_an_offset_timestamp_normalise_to_utc(
        self, time_config_file, capsys
    ):
        naive = _chain_json(capsys, time_config_file, "--at", "2026-08-17T07:00:00")
        offset = _chain_json(capsys, time_config_file,
                             "--at", "2026-08-17T09:00:00+02:00")
        assert naive["utc_hour"] == 7
        assert offset["utc_hour"] == 7

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
        assert result["at_source"] == "--at"


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
