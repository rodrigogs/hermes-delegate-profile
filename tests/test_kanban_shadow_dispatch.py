"""Tests for the kanban-dispatch SHADOW routing (pre_kanban_dispatch hook).

The hook records what the capability router WOULD choose for each dispatched
card — title+body, no classifier, profile-constrained — without writing the
model/provider field. The exit gate (shadow_gate_rate) measures how often
Stage 0 alone falls through on REAL cards.
"""

import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "delegate_profile_plugin", REPO_ROOT / "__init__.py"
)
assert _spec is not None and _spec.loader is not None, "could not load plugin spec"
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def trace_home(tmp_path, monkeypatch):
    """Point the durable trace (and breaker state) at a throwaway HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _card(title, body="", model_override=None, provider_override=None):
    return types.SimpleNamespace(
        title=title,
        body=body,
        model_override=model_override,
        provider_override=provider_override,
    )


def _read_trace() -> list:
    """All durable trace entries, parsed from disk."""
    from router.durable_decision_log import routes_path
    entries = []
    p = routes_path()
    if not p.exists():
        return entries
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# _kanban_task_text — title + body is the routing input
# ---------------------------------------------------------------------------

def test_kanban_task_text_joins_title_and_body():
    text = dp._kanban_task_text(_card("Fix the bug", "The crash happens on save"))
    assert text == "Fix the bug\n\nThe crash happens on save"


def test_kanban_task_text_title_only_when_no_body():
    assert dp._kanban_task_text(_card("Fix the bug")) == "Fix the bug"


def test_kanban_task_text_duck_types_missing_fields():
    assert dp._kanban_task_text(types.SimpleNamespace(title="t")) == "t"


# ---------------------------------------------------------------------------
# _KanbanShadowLog — the shadow stamp and the profile constraint
# ---------------------------------------------------------------------------

def test_shadow_log_stamps_the_persisted_entry(trace_home):
    log = dp._KanbanShadowLog()
    log.record("hard_rule", {"profile": "coder", "model": "T4"},
               task_preview="task")
    entry = _read_trace()
    assert len(entry) == 1
    assert entry[0]["shadow"] is True
    assert entry[0]["cause"] == "hard_rule"


def test_shadow_log_keeps_a_matching_profile_decision_untouched(trace_home):
    log = dp._KanbanShadowLog(allowed_profile="coder")
    log.record("has_code_rule", {"profile": "coder", "model": "glm-4.7"},
               task_preview="rename a var")
    entry = _read_trace()[0]
    assert entry["cause"] == "has_code_rule"
    assert entry["output"]["model"] == "glm-4.7"


def test_shadow_log_refuses_a_profile_changing_decision(trace_home):
    """A rule that moves the ROLE cannot run on the kanban dispatch path."""
    log = dp._KanbanShadowLog(allowed_profile="coder")
    log.record(
        "review-request", {"profile": "reviewer", "model": "gpt-5.5"},
        task_preview="Review this PR",
        steps=[{"stage": "rules", "in": {}, "out": {"profile": "reviewer"},
                "cause": "keyword_match"}],
    )
    entry = _read_trace()[0]
    assert entry["cause"] == "profile_ignored"
    # The model half is dropped — executing it would run half the rule.
    assert "model" not in entry["output"]
    assert "provider" not in entry["output"]
    # The refused profile stays, so the operator sees WHICH role was wanted.
    assert entry["output"]["profile"] == "reviewer"


def test_shadow_log_without_assignee_constrains_nothing(trace_home):
    log = dp._KanbanShadowLog(allowed_profile=None)
    log.record("keyword_match", {"profile": "reviewer", "model": "x"})
    assert _read_trace()[0]["cause"] == "keyword_match"


# ---------------------------------------------------------------------------
# _kanban_profile_refused — the ONE profile constraint
# ---------------------------------------------------------------------------

def test_profile_refused_false_for_a_matching_profile():
    assert dp._kanban_profile_refused({"profile": "coder"}, "coder") is False


def test_profile_refused_true_for_a_role_change():
    assert dp._kanban_profile_refused({"profile": "reviewer"}, "coder") is True


def test_profile_refused_false_without_an_assignee():
    assert dp._kanban_profile_refused({"profile": "reviewer"}, None) is False


def test_profile_refused_false_when_the_decision_names_no_profile():
    assert dp._kanban_profile_refused({"model": "glm-4.7"}, "coder") is False


# ---------------------------------------------------------------------------
# _kanban_live_override — what a live hook may hand the dispatcher
# ---------------------------------------------------------------------------

def test_live_override_uses_the_plan_head_when_a_chain_is_attached():
    decision = {
        "profile": "coder", "model": "glm-5.3", "provider": "zai",
        "chain": [{"model": "gpt-5.6-luna", "provider": "openai-codex"}],
    }
    assert dp._kanban_live_override(decision, "coder") == {
        "model": "gpt-5.6-luna", "provider": "openai-codex",
    }


def test_live_override_refuses_a_chain_head_without_provider():
    decision = {
        "profile": "coder", "model": "glm-5.3", "provider": "zai",
        "chain": [{"model": "gpt-5.6-luna"}],
    }
    assert dp._kanban_live_override(decision, "coder") is None


def test_live_override_falls_back_to_the_declared_pair_without_a_chain():
    decision = {"profile": "coder", "model": "glm-4.7", "provider": "zai"}
    assert dp._kanban_live_override(decision, "coder") == {
        "model": "glm-4.7", "provider": "zai",
    }


def test_live_override_refuses_a_declared_model_without_provider():
    decision = {"profile": "coder", "model": "glm-4.7"}
    assert dp._kanban_live_override(decision, "coder") is None


def test_live_override_refuses_a_decision_with_no_model():
    decision = {"profile": "coder", "action": "classify"}
    assert dp._kanban_live_override(decision, "coder") is None


def test_live_override_refuses_a_profile_changing_decision():
    decision = {
        "profile": "reviewer", "model": "gpt-5.5", "provider": "openai-codex",
    }
    assert dp._kanban_live_override(decision, "coder") is None


def test_live_override_without_an_assignee_constrains_nothing():
    decision = {
        "profile": "reviewer", "model": "gpt-5.5", "provider": "openai-codex",
    }
    assert dp._kanban_live_override(decision, None) == {
        "model": "gpt-5.5", "provider": "openai-codex",
    }


# ---------------------------------------------------------------------------
# _read_kanban_task — board DB read, guarded like every hermes_cli access
# ---------------------------------------------------------------------------

def test_read_kanban_task_degrades_without_hermes_cli(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    assert dp._read_kanban_task("t1", None) is None


def test_read_kanban_task_returns_the_card(monkeypatch):
    card = _card("Fix the bug", "body text")
    fake_cli = types.ModuleType("hermes_cli")
    fake_kb = types.ModuleType("hermes_cli.kanban_db")

    class _Conn:
        def close(self):
            pass

    fake_kb.connect = lambda board=None: _Conn()
    fake_kb.get_task = lambda conn, task_id: card
    fake_cli.kanban_db = fake_kb
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_kb)
    assert dp._read_kanban_task("t1", "board") is card


def test_read_kanban_task_empty_id_short_circuits():
    assert dp._read_kanban_task("", None) is None


def test_read_kanban_task_returns_none_when_the_board_read_raises(monkeypatch):
    fake_cli = types.ModuleType("hermes_cli")
    fake_kb = types.ModuleType("hermes_cli.kanban_db")

    class _Conn:
        def close(self):
            pass

    def _boom(conn, task_id):
        raise RuntimeError("db locked")

    fake_kb.connect = lambda board=None: _Conn()
    fake_kb.get_task = _boom
    fake_cli.kanban_db = fake_kb
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake_kb)
    assert dp._read_kanban_task("t1", "board") is None


# ---------------------------------------------------------------------------
# _on_pre_kanban_dispatch — shadow consumer
# ---------------------------------------------------------------------------

def test_shadow_hook_records_the_decision_and_never_writes_the_field(
    trace_home, monkeypatch,
):
    """Happy path: card routes through Stage 0, trace gets a shadow entry."""
    monkeypatch.setattr(
        dp, "_read_kanban_task",
        lambda task_id, board: _card(
            "Rename getCwd in src/utils.py", "small mechanical change",
        ),
    )
    result = dp._on_pre_kanban_dispatch(
        task_id="t1", profile_name="trama-engineer", board="default",
        assignee="coder", run_id=1,
    )
    # SHADOW: the hook never returns a model/provider dict.
    assert result is None
    entries = _read_trace()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["shadow"] is True
    assert entry["task"] == "Rename getCwd in src/utils.py\n\nsmall mechanical change"
    # The live policy routes this card deterministically (Stage 0, no classifier).
    assert entry["cause"] not in ("", None)
    assert "steps" in entry


def test_shadow_hook_refuses_a_rule_that_changes_profile(trace_home, monkeypatch):
    """review-request wants a reviewer; a coder card cannot become one."""
    monkeypatch.setattr(
        dp, "_read_kanban_task",
        lambda task_id, board: _card("Review this PR for security issues"),
    )
    dp._on_pre_kanban_dispatch(
        task_id="t2", profile_name="x", board="default",
        assignee="coder", run_id=2,
    )
    entry = _read_trace()[0]
    assert entry["cause"] == "profile_ignored"
    assert entry["output"]["profile"] == "reviewer"
    assert "model" not in entry["output"]


def test_shadow_hook_is_silent_when_router_is_disabled(trace_home, monkeypatch):
    monkeypatch.setattr(
        dp, "_read_kanban_task",
        lambda task_id, board: _card("Rename getCwd in src/utils.py"),
    )
    monkeypatch.setattr(dp, "_load_router_config", lambda: {"enabled": False})
    assert dp._on_pre_kanban_dispatch(task_id="t3", assignee="coder") is None
    assert _read_trace() == []


def test_live_hook_routes_records_and_returns_the_decision(trace_home, monkeypatch):
    """shadow.enabled: false is LIVE mode, not off: same routing and trace,
    but the hook now returns the model decision for the dispatcher to apply."""
    monkeypatch.setattr(
        dp, "_read_kanban_task",
        lambda task_id, board: _card(
            "Rename getCwd in src/utils.py", "small mechanical change",
        ),
    )
    monkeypatch.setattr(
        dp, "_load_router_config",
        lambda: {"enabled": True, "shadow": {"enabled": False}},
    )
    result = dp._on_pre_kanban_dispatch(
        task_id="t4", profile_name="trama-engineer", board="default",
        assignee="coder", run_id=4,
    )
    # Live: both fields set — the head of the (declared-order) chain, which for
    # a fail-safe decision is the declared pair itself.
    assert result == {"model": "deepseek-v4-pro", "provider": "deepseek"}
    entries = _read_trace()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["shadow"] is False
    assert entry["cause"] == "fail_safe_strong"


def test_live_hook_refuses_a_rule_that_changes_profile(trace_home, monkeypatch):
    """LIVE mode applies the SAME refusal the trace records: a reviewer-role
    decision must not drive a coder card's dispatch, and the trace says so."""
    monkeypatch.setattr(
        dp, "_read_kanban_task",
        lambda task_id, board: _card("Review this PR for security issues"),
    )
    monkeypatch.setattr(
        dp, "_load_router_config",
        lambda: {
            "enabled": True,
            "shadow": {"enabled": False},
            "rules": [
                {"id": "review-request", "status": "stable",
                 "when": {"keywords": {"contains": "review"}},
                 "then": {"profile": "reviewer", "model": "T1"}},
            ],
            "tiers": {"T1": {"model": "glm-4.7", "provider": "zai"}},
        },
    )
    result = dp._on_pre_kanban_dispatch(
        task_id="t6", profile_name="x", board="default",
        assignee="coder", run_id=6,
    )
    assert result is None
    entry = _read_trace()[0]
    assert entry["shadow"] is False
    assert entry["cause"] == "profile_ignored"
    assert entry["output"]["profile"] == "reviewer"
    assert "model" not in entry["output"]


def test_live_entries_never_count_in_the_gate(trace_home, monkeypatch):
    """shadow_gate_rate counts ``shadow is True`` only — a live-only trace is
    an empty sample even when the live decision fell through Stage 0."""
    monkeypatch.setattr(
        dp, "_read_kanban_task",
        lambda task_id, board: _card("Some ambiguous task with no rule"),
    )
    monkeypatch.setattr(
        dp, "_load_router_config",
        lambda: {"enabled": True, "shadow": {"enabled": False}},
    )
    result = dp._on_pre_kanban_dispatch(task_id="t7", assignee="coder")
    assert result is not None  # live: the fail-safe decision was returned
    entries = _read_trace()
    assert len(entries) == 1
    assert entries[0]["shadow"] is False
    # The only entry is live — the sample is empty, so the gate reads unmet.
    assert dp.shadow_gate_rate() is None
    assert dp._shadow_gate_ok() is False


def test_shadow_hook_never_breaks_dispatch_when_routing_raises(trace_home, monkeypatch):
    """A broken router call is a no-op — byte-identical to no subscriber."""
    import router.adapter as adapter_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("route exploded")

    monkeypatch.setattr(
        dp, "_read_kanban_task",
        lambda task_id, board: _card("Rename getCwd in src/utils.py"),
    )
    monkeypatch.setattr(adapter_mod, "route", _boom)
    assert dp._on_pre_kanban_dispatch(task_id="t9", assignee="coder") is None
    assert _read_trace() == []


def test_shadow_hook_is_silent_when_the_config_load_raises(trace_home, monkeypatch):
    def _boom():
        raise RuntimeError("yaml broken")

    monkeypatch.setattr(dp, "_load_router_config", _boom)
    assert dp._on_pre_kanban_dispatch(task_id="t8", assignee="coder") is None
    assert _read_trace() == []


def test_shadow_hook_never_breaks_dispatch_on_failure(trace_home, monkeypatch):
    """A broken card read is a no-op, byte-identical to having no subscriber."""
    monkeypatch.setattr(dp, "_read_kanban_task", lambda task_id, board: None)
    assert dp._on_pre_kanban_dispatch(task_id="t5", assignee="coder") is None
    assert _read_trace() == []


# ---------------------------------------------------------------------------
# The exit gate — no_classifier + fallthrough on REAL cards
# ---------------------------------------------------------------------------

def _shadow_entry(cause, reason=None, shadow=True):
    steps = []
    if reason is not None:
        steps.append({"stage": "fail_safe", "in": {"reason": reason},
                      "out": {}, "cause": "fail_safe_strong"})
    entry = {"ts": 0.0, "cause": cause, "output": {}, "rule_id": None,
             "task": "", "steps": steps}
    if shadow:
        entry["shadow"] = True
    return entry


def test_gate_empty_trace_is_not_met(trace_home):
    assert dp.shadow_gate_rate() is None
    assert dp._shadow_gate_ok() is False


def test_gate_counts_only_shadow_entries(trace_home):
    from router.durable_decision_log import routes_path
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        # A delegate-tool entry (no shadow key) must not be counted.
        json.dumps(_shadow_entry("fail_safe_strong", "no_classifier", shadow=False)),
        json.dumps(_shadow_entry("has_code_rule", shadow=True)),
        json.dumps(_shadow_entry("fail_safe_strong", "no_classifier", shadow=True)),
        json.dumps(_shadow_entry("fail_safe_strong", "fallthrough", shadow=True)),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 2 of 3 shadow entries fell through.
    assert dp.shadow_gate_rate() == pytest.approx(2 / 3)
    assert dp._shadow_gate_ok() is False


def test_gate_zero_fallthrough_is_met(trace_home):
    from router.durable_decision_log import routes_path
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(_shadow_entry("has_code_rule", shadow=True)),
        json.dumps(_shadow_entry("keyword_match", shadow=True)),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert dp.shadow_gate_rate() == 0.0
    assert dp._shadow_gate_ok() is True


def test_gate_boundary_at_the_agreed_limit(trace_home):
    from router.durable_decision_log import routes_path
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 1 of 5 shadow entries fell through == exactly the agreed 0.20 limit.
    lines = [
        json.dumps(_shadow_entry("fail_safe_strong", "no_classifier", shadow=True)),
    ] + [
        json.dumps(_shadow_entry("has_code_rule", shadow=True)) for _ in range(4)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert dp.shadow_gate_rate() == pytest.approx(0.20)
    assert dp._shadow_gate_ok() is True


def test_gate_profile_ignored_still_counts_as_fallthrough(trace_home):
    """The cause may be rewritten to profile_ignored; the steps keep the truth."""
    from router.durable_decision_log import routes_path
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = _shadow_entry("profile_ignored", "no_classifier", shadow=True)
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert dp.shadow_gate_rate() == 1.0


def test_gate_ignores_steps_that_are_not_a_fail_safe_fallthrough(trace_home):
    """A step that is not a fail_safe stage (or not a dict) is not a fallthrough."""
    from router.durable_decision_log import routes_path
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = _shadow_entry("has_code_rule", shadow=True)
    entry["steps"] = [{"stage": "rules", "in": {}, "out": {}, "cause": "hard_rule"}]
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert dp.shadow_gate_rate() == 0.0


def test_gate_ok_honors_an_explicit_rate():
    assert dp._shadow_gate_ok(0.0) is True
    assert dp._shadow_gate_ok(0.5) is False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_subscribes_the_pre_kanban_dispatch_hook(monkeypatch):
    class Ctx:
        def __init__(self):
            self.tools = []
            self.hooks = []

        def dispatch_tool(self, name, args):
            return "{}:{}".format(name, args["goal"])

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_hook(self, *args, **kwargs):
            self.hooks.append((args, kwargs))

    monkeypatch.setattr(dp, "_get_active_profile_name", lambda: "parent")
    ctx = Ctx()
    dp.register(ctx)
    names = [args[0] for args, _ in ctx.hooks]
    assert "pre_kanban_dispatch" in names
    assert "post_tool_call" in names
