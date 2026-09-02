"""Integration tests — capability router wired into delegate_profile.

Tests the _route_task() bridge function and the full delegation path
when profile is omitted/auto. Uses mocks for ctx.llm (classifier) and
the subprocess spawn (no real hermes process needed).

The spawn mock is conftest's autouse ``_no_real_spawn``; it is named as a
parameter wherever a test depends on it so the dependency is visible, and
test_spawn_guard_applies_to_the_module_under_test asserts it actually landed on
this file's ``dp`` — for a long time it did not, and the handler tests dispatched
real agents while reading as if they were hermetic.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# The plugin __init__.py lives in a directory with a hyphen, which is not a
# valid Python module name. Import it dynamically via importlib.util.
import copy
import importlib.util
import subprocess
import threading
import types

_PLUGIN_INIT = Path(__file__).resolve().parent.parent / "__init__.py"

# Integration tests cover the plugin bridge, not the mutable production policy.
# Keep the expected routing contract hermetic: router.yaml can evolve with the
# live model roster without making these unit-level assertions flaky.
_TEST_ROUTER_CONFIG = {
    "enabled": True,
    "classifier": {
        "model": "glm-5.2",
        "provider": "zai",
        "temperature": 0,
        "max_tokens": 128,
        "timeout_seconds": 8,
    },
    "fail_safe": {"profile": "coder", "model": "claude-opus", "provider": "anthropic"},
    "blocklist": {
        "manual_ban": [
            {"model": "gpt-5.6-sol", "provider": "openai-codex", "reason": "test-ban"}
        ],
        "fallback_chain": ["gpt-5.6-sol", "glm-5.2"],
        "auto_breaker": {"enabled": False},
    },
    "rules": [
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

# The plugin imports from router/ which needs the plugin dir on sys.path
_PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


@pytest.fixture(scope="module")
def dp():
    """Load the plugin __init__.py as a module."""
    spec = importlib.util.spec_from_file_location("delegate_profile_plugin", _PLUGIN_INIT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def isolated_router_config(dp):
    """Give each bridge test a fresh, stable router policy."""
    with patch.object(dp, "_load_router_config", return_value=copy.deepcopy(_TEST_ROUTER_CONFIG)):
        yield


def test_spawn_guard_applies_to_the_module_under_test(dp, _no_real_spawn):
    """The guard must stub the copy these tests call, not a second copy of it.

    ``dp`` is built with spec_from_file_location + exec_module and is never put in
    sys.modules, so a guard that looks itself up by module name finds nothing (or
    worse, finds one of the other registered copies) and is a no-op here while
    still reporting green. Assert the property — the stub is on THIS object — so
    that regressing the lookup fails a test instead of quietly resuming real
    dispatches.
    """
    assert _no_real_spawn.stub_for(dp) is dp._spawn
    assert _no_real_spawn.stub_for(dp) is not None


def test_plugin_package_routes_without_checkout_on_sys_path(tmp_path):
    """The installed package must resolve its bundled router from any cwd."""
    code = f"""
import importlib.util
import json
import sys
import types
from pathlib import Path

init_file = Path({str(_PLUGIN_INIT)!r})
namespace = types.ModuleType("hermes_plugins")
namespace.__path__ = []
sys.modules["hermes_plugins"] = namespace
spec = importlib.util.spec_from_file_location(
    "hermes_plugins.delegate_profile_probe",
    init_file,
    submodule_search_locations=[str(init_file.parent)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
result = module._route_task("Rename helper in src/utils.py", "", None)
print(json.dumps(result, sort_keys=True))
assert result and result["profile"] == "coder"
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_package_loader_executes_relative_router_import_paths(monkeypatch, tmp_path):
    """Installed-plugin imports use relative modules, unlike direct test loading."""
    namespace = types.ModuleType("hermes_plugins")
    namespace.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_plugins", namespace)
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.delegate_profile_coverage",
        _PLUGIN_INIT,
        submodule_search_locations=[str(_PLUGIN_INIT.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module._LOADED_AS_PACKAGE is True

    # This copy is created inside the test body, after conftest's _no_real_spawn
    # has already stubbed everything it could see, so it is the one plugin copy in
    # the suite that still holds the real `_spawn`. Nothing below dispatches — only
    # _make_classify_fn / _route_task / _record_breaker_outcome are exercised — so
    # raising is stronger than faking: if this test ever grows a code path that
    # dispatches, it fails here instead of SSHing to a billed rail.
    def _no_dispatch(*args, **kwargs):
        raise AssertionError(
            "this test must not spawn; the setup-time guard cannot reach a copy "
            "registered in the test body"
        )

    monkeypatch.setattr(module, "_spawn", _no_dispatch)
    monkeypatch.setattr(module, "_run_watched", _no_dispatch)

    config = copy.deepcopy(_TEST_ROUTER_CONFIG)
    monkeypatch.setattr(module, "_load_router_config", lambda: config)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class Result:
        text = '{"tier":"T1","confidence":"high"}'

    class LLM:
        def complete(self, **_kwargs):
            return Result()

    class Ctx:
        llm = LLM()

    classify = module._make_classify_fn(Ctx())
    assert classify is not None
    assert classify("Rename helper in utils.py", {})["tier"] == "T1"
    assert module._route_task("Rename helper in utils.py", "", None)["profile"] == "coder"
    module._record_breaker_outcome("coder", "glm-5.2-fast", None)

    # The kanban-dispatch hook takes the same relative-import path; route a card
    # through the package copy so its hook branch is exercised too. Shadow mode
    # (no shadow section in this config) — the hook must return None.
    monkeypatch.setattr(
        module, "_read_kanban_task",
        lambda task_id, board: types.SimpleNamespace(
            title="Rename helper in utils.py", body="",
            model_override=None, provider_override=None,
        ),
    )
    assert module._on_pre_kanban_dispatch(
        task_id="t1", profile_name="x", board="default",
        assignee="coder", run_id=1,
    ) is None


# ---------------------------------------------------------------------------
# _route_task tests
# ---------------------------------------------------------------------------

class TestRouteTask:
    """Test the _route_task bridge function."""

    def test_routes_hard_task_to_t4(self, dp):
        """A hard task (debug, race condition) routes to coder + T4."""
        def mock_classify(task, features):
            return {"tier": "T4", "confidence": "high", "signals": "", "needs_capability": ""}

        result = dp._route_task(
            goal="Debug this race condition in the connection pool",
            requested_model="",
            classify_fn=mock_classify,
        )
        assert result is not None
        assert result["profile"] == "coder"
        assert result["model"] == "claude-opus"  # T4

    def test_routes_trivial_task_to_t1(self, dp):
        """A trivial task (rename) routes to coder + T1."""
        result = dp._route_task(
            goal="Rename getCwd to getCurrentWorkingDirectory in src/utils.py",
            requested_model="",
            classify_fn=None,
        )
        assert result is not None
        assert result["profile"] == "coder"
        assert result["model"] == "glm-5.2-fast"  # T1

    def test_routes_review_task_uses_failsafe_model_but_keeps_the_role(self, dp):
        """A review task with no classifier: fail-safe MODEL, the rule's own ROLE.

        review-request has action:classify, so with classify_fn=None the adapter
        falls back to fail_safe for the model - picking a tier was the only thing
        the classifier was going to do. The role axis is a separate decision the
        rule already made deterministically, and it is still true when the
        classifier is down.

        This previously asserted profile == "coder", pinning the bug: the
        fail-safe overwrote the role, so /explain reported reviewer while route()
        dispatched coder - the explanation surface and the dispatch disagreed
        about the same task.
        """
        result = dp._route_task(
            goal="Review this PR for security issues",
            requested_model="",
            classify_fn=None,
        )
        assert result is not None
        assert result["profile"] == "reviewer"     # the rule decision survives
        assert result["model"] == "claude-opus"  # fail_safe

    def test_returns_none_when_router_disabled(self, dp):
        """When router is disabled in config, _route_task returns None."""
        with patch.object(dp, "_load_router_config", return_value={"enabled": False}):
            result = dp._route_task("any task", "", None)
        assert result is None

    def test_returns_none_on_router_yaml_missing(self, dp):
        """Missing router.yaml → None (best-effort, never blocks)."""
        with patch.object(dp, "_load_router_config", return_value={}):
            result = dp._route_task("any task", "", None)
        assert result is None

    def test_recursion_guard_blocks_same_thread(self, dp):
        """A re-entrant call on the SAME thread (mid-classifier) is stopped."""
        dp._router_guard.active = True
        try:
            result = dp._route_task("Debug race condition", "", None)
            assert result is None
        finally:
            dp._router_guard.active = False

    def test_recursion_guard_cleared_after_call(self, dp):
        """After _route_task returns, the per-thread guard is released."""
        assert getattr(dp._router_guard, "active", False) is False
        dp._route_task(
            "Rename getCwd to getCurrentWorkingDirectory in src/utils.py", "", None
        )
        assert getattr(dp._router_guard, "active", False) is False

    def test_concurrent_routes_do_not_interfere(self, dp):
        """A slow classifier on one thread must not suppress a concurrent route.

        The recursion guard is per-thread. The previous process-global
        ``os.environ`` sentinel made one in-flight classifier suppress every
        concurrent ``_route_task`` — a second delegation returned None and the
        handler answered "profile is required". Reproducing that here: thread A
        blocks inside the classifier (guard held), thread B routes a trivial
        task and must still get its concrete T1 target.
        """
        started = threading.Event()
        release = threading.Event()
        holder_result = {}

        def slow_classify(task, features):
            started.set()
            release.wait(timeout=10)
            return {"tier": "T4", "confidence": "high"}

        def holder():
            holder_result["r"] = dp._route_task(
                "ambiguous task with no clear signal", "", slow_classify
            )

        t = threading.Thread(target=holder)
        t.start()
        assert started.wait(timeout=5), "classifier never started"
        try:
            result = dp._route_task(
                "Rename getCwd to getCurrentWorkingDirectory in src/utils.py",
                "", None,
            )
        finally:
            release.set()
            t.join(timeout=5)

        assert result is not None
        assert result["profile"] == "coder"
        assert result["model"] == "glm-5.2-fast"  # T1
        # The held thread also completed once released (no deadlock / lost guard).
        assert holder_result["r"] is not None

    def test_blocklist_veto_returns_none(self, dp):
        """When requested_model is blocklisted, _route_task returns None."""
        result = dp._route_task(
            goal="Do something",
            requested_model="gpt-5.6-sol",  # banned in router.yaml
            classify_fn=None,
        )
        assert result is None

    def test_classifier_exception_uses_failsafe(self, dp):
        """When the classifier explodes, the adapter catches it and uses fail-safe."""
        def exploding_classify(task, features):
            raise RuntimeError("classifier exploded")

        result = dp._route_task(
            goal="ambiguous task with no clear signal",
            requested_model="",
            classify_fn=exploding_classify,
        )
        # Adapter catches classifier exception → fail_safe_strong
        assert result is not None
        assert result["model"] == "claude-opus"  # fail_safe


# ---------------------------------------------------------------------------
# _make_classify_fn tests
# ---------------------------------------------------------------------------

class TestMakeClassifyFn:
    """Test the classify_fn factory."""

    def test_returns_none_when_router_disabled(self, dp):
        with patch.object(dp, "_load_router_config", return_value={"enabled": False}):
            fn = dp._make_classify_fn(ctx=MagicMock())
        assert fn is None

    def test_returns_none_when_ctx_has_no_llm(self, dp):
        ctx = MagicMock()
        del ctx.llm
        fn = dp._make_classify_fn(ctx=ctx)
        assert fn is None

    def test_returns_none_when_ctx_is_none(self, dp):
        fn = dp._make_classify_fn(ctx=None)
        assert fn is None

    def test_classify_fn_calls_ctx_llm(self, dp):
        """The returned classify_fn calls ctx.llm.complete with correct params."""
        mock_result = MagicMock()
        mock_result.text = '{"tier": "T3", "confidence": "med", "signals": "", "needs_capability": ""}'
        mock_ctx = MagicMock()
        mock_ctx.llm.complete.return_value = mock_result

        fn = dp._make_classify_fn(ctx=mock_ctx)
        assert fn is not None

        result = fn("Build a REST API endpoint", {
            "verb_class": "unknown",
            "has_code": True,
            "size_lines": 0,
            "num_files": 1,
        })

        assert result["tier"] == "T3"
        mock_ctx.llm.complete.assert_called_once()
        call_kwargs = mock_ctx.llm.complete.call_args
        assert call_kwargs.kwargs["provider"] == "zai"
        assert call_kwargs.kwargs["model"] == "glm-5.2"
        assert call_kwargs.kwargs["temperature"] == 0

    def test_classify_fn_strips_markdown_fences(self, dp):
        """JSON wrapped in markdown fences is parsed correctly."""
        mock_result = MagicMock()
        mock_result.text = '```json\n{"tier": "T2", "confidence": "high"}\n```'
        mock_ctx = MagicMock()
        mock_ctx.llm.complete.return_value = mock_result

        fn = dp._make_classify_fn(ctx=mock_ctx)
        result = fn("Add a function to utils.py", {"verb_class": "unknown"})
        assert result["tier"] == "T2"


# ---------------------------------------------------------------------------
# Handler integration tests
# ---------------------------------------------------------------------------

class TestHandlerIntegration:
    """Test the delegate_profile handler with router wired in."""

    def _make_handler(self, dp, classify_fn=None):
        """Build a handler with a mock ctx and mock dispatch_delegate."""
        mock_ctx = MagicMock()
        patch_target = patch.object(dp, "_make_classify_fn", return_value=classify_fn)
        with patch_target:
            handler = dp._make_handler(
                current_profile="test",
                dispatch_delegate=lambda args: json.dumps({"success": True}),
                ctx=mock_ctx,
            )
        return handler

    def test_explicit_profile_skips_router(self, dp):
        """When profile is given explicitly, router is not called."""
        handler = self._make_handler(dp, classify_fn=None)
        result = handler({"goal": "test", "profile": "nonexistent_profile_xyz"})
        parsed = json.loads(result)
        assert parsed.get("failure_kind") == "unknown_profile"

    def test_auto_profile_triggers_router(self, dp, _no_real_spawn, monkeypatch):
        """profile='auto' routes to T4 and dispatches THAT target, hermetically.

        Both this test and test_no_profile_triggers_router used to accept any of
        ("unknown_profile", "quota_exhausted", "agent_error", "hard_timeout", None),
        on the stated grounds that "the spawn itself is not stubbed here, so any
        downstream provider verdict is acceptable". That premise was false in the
        way that matters: the conftest guard meant to stub the spawn looked the
        module up as ``sys.modules["delegate_profile_plugin"]``, a name the ``dp``
        fixture never registers, so it silently stubbed nothing and these tests
        dispatched real ``hermes -p coder chat`` subprocesses against a billed rail.

        Each widening was therefore the wrong instinct twice over. It converted a
        real signal — that rail returns no verdict on that box — into a pass, and
        it did so for a value the router itself treats as a stall: breaker.py rates
        ``hard_timeout`` at weight 1, "could be slow, could be broken". Nothing
        else was watching either, because _TEST_ROUTER_CONFIG disables the
        auto-breaker, so accepting the verdict here was the only place it could
        have been noticed. A set of five accepted outcomes is not an assertion.

        With the guard fixed the spawn is a stub, so the only remaining source of
        variance is whether the routed profile exists on the host — the axis that
        actually produced the old two-box split (``unknown_profile`` on a dev
        machine without a ``coder`` profile, a live provider verdict on the WSL box
        with one). It is a precondition of what this test claims, not a finding, so
        it is pinned rather than tolerated. The profile-absent branch keeps its own
        test in test_router_failure_falls_through, which asserts unknown_profile.
        """
        monkeypatch.setattr(dp, "_profile_exists", lambda profile: True)

        def mock_classify(task, features):
            return {"tier": "T4", "confidence": "high"}

        handler = self._make_handler(dp, classify_fn=mock_classify)
        result = handler({"goal": "Debug race condition", "profile": "auto"})
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed.get("failure_kind") is None
        # The router's chosen target, not just "a target": T4 -> claude-opus/anthropic.
        assert parsed["profile"] == "coder"
        assert parsed["model"] == "claude-opus"
        assert parsed["provider"] == "anthropic"
        # ...and that target is what the dispatch carried, intercepted by the guard
        # rather than executed. An empty list here means the handler answered
        # without dispatching; a real process would never appear in it at all.
        assert _no_real_spawn.blocked == [[
            dp._resolve_hermes_bin(), "-p", "coder", "chat",
            "-q", "Debug race condition", "-m", "claude-opus",
            "--provider", "anthropic",
        ]]

    def test_no_profile_triggers_router(self, dp, _no_real_spawn, monkeypatch):
        """Omitting profile triggers the router (same as auto), here routing to T1.

        Same contract as test_auto_profile_triggers_router — see its docstring for
        why the accepted-value list is gone — on the other tier, so the assertion
        pins the classifier's tier through to the argv instead of merely observing
        that something was dispatched.
        """
        monkeypatch.setattr(dp, "_profile_exists", lambda profile: True)

        def mock_classify(task, features):
            return {"tier": "T1", "confidence": "high"}

        handler = self._make_handler(dp, classify_fn=mock_classify)
        result = handler({"goal": "Rename getCwd in src/utils.py"})
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed.get("failure_kind") is None
        assert parsed["profile"] == "coder"
        assert parsed["model"] == "glm-5.2-fast"   # T1
        assert parsed["provider"] == "zai"
        assert _no_real_spawn.blocked == [[
            dp._resolve_hermes_bin(), "-p", "coder", "chat",
            "-q", "Rename getCwd in src/utils.py", "-m", "glm-5.2-fast",
            "--provider", "zai",
        ]]

    def test_router_failure_falls_through(self, dp, monkeypatch):
        """If router fails, delegation proceeds without crashing.

        The router falls through to a fail-safe profile; with that profile
        absent we get a clean unknown_profile error, and with it present the
        (hermetically stubbed) spawn succeeds. Either way the handler must
        return valid JSON and never raise."""
        def exploding_classify(task, features):
            raise RuntimeError("boom")

        monkeypatch.setattr(dp, "_profile_exists", lambda p: False)
        handler = self._make_handler(dp, classify_fn=exploding_classify)
        result = handler({"goal": "ambiguous task"})
        parsed = json.loads(result)
        assert "error" in parsed or parsed.get("failure_kind") == "unknown_profile"
# Appended test block for cross-rail fallback execution (follow-up #2).
import json as _json


def test_router_fallback_executes_on_retryable_failure(dp, monkeypatch):
    """When the routed primary target fails RETRYABLY, the executor tries the
    router's fallback targets in order until one succeeds (cross-rail failover)."""
    # Router picks a primary + a fallback list (e.g. Mac-only primary, non-Mac fallback).
    monkeypatch.setattr(dp, "_route_task", lambda goal, model, cf: {
        "profile": "coder", "model": "us.anthropic.claude-opus-4-8", "provider": "bedrock",
        "fallback": [{"model": "deepseek-v4-pro", "provider": "deepseek"}],
    }, raising=False)
    monkeypatch.setattr(dp, "_profile_exists", lambda p: True)

    calls = []
    class _P:
        pid = 4242
        returncode = 0
        stdout = None
        stderr = None
        def poll(self): return 0
        def wait(self, timeout=None): return 0
    monkeypatch.setattr(dp, "_spawn", lambda cmd, env: (calls.append(list(cmd)) or _P()), raising=False)

    # first attempt (primary/bedrock) fails retryably; second (fallback/deepseek) succeeds
    seq = iter([("spawn_error_sim", 1, "", "boom"), ("exited", 0, "DONE", "")])
    def fake_watch(proc, pgid, ttfb, idle, hard, grace):
        try: return next(seq)
        except StopIteration: return ("exited", 0, "DONE", "")
    monkeypatch.setattr(dp, "_run_watched", fake_watch, raising=False)
    # make the first classify retryable
    real_classify = dp._classify
    def patched_classify(reason, rc):
        if reason == "spawn_error_sim": return ("spawn_error", True)
        return real_classify(reason, rc)
    monkeypatch.setattr(dp, "_classify", patched_classify, raising=False)

    h = dp._make_handler(current_profile="other", dispatch_delegate=lambda a: "{}", ctx=None)
    out = _json.loads(h({"goal": "hard task"}))
    assert out.get("success") is True, out
    # the second (fallback) target's provider must have reached the cmd
    assert any("deepseek" in " ".join(c) for c in calls), calls
    assert any("--provider" in c for c in calls), calls


def test_router_no_fallback_when_primary_succeeds(dp, monkeypatch):
    """Primary success => no fallback attempt, provider still passed."""
    monkeypatch.setattr(dp, "_route_task", lambda goal, model, cf: {
        "profile": "coder", "model": "glm-5.2", "provider": "zai",
        "fallback": [{"model": "deepseek-v4-pro", "provider": "deepseek"}],
    }, raising=False)
    monkeypatch.setattr(dp, "_profile_exists", lambda p: True)
    calls = []
    class _P:
        pid = 4242; returncode = 0
        stdout = None
        stderr = None
        def poll(self): return 0
        def wait(self, timeout=None): return 0
    monkeypatch.setattr(dp, "_spawn", lambda cmd, env: (calls.append(list(cmd)) or _P()), raising=False)
    monkeypatch.setattr(dp, "_run_watched", lambda *a: ("exited", 0, "OK", ""), raising=False)
    h = dp._make_handler(current_profile="other", dispatch_delegate=lambda a: "{}", ctx=None)
    out = _json.loads(h({"goal": "task"}))
    assert out.get("success") is True
    assert len(calls) == 1, "should not try fallback on primary success"
    assert any("zai" in " ".join(c) for c in calls)


class TestTheClassifierFollowsTheLivePolicy:
    """router.example.yaml promises "editing router.yaml is live with no restart".

    `classify_fn` was built ONCE at register time, closing over `enabled`,
    `provider`, `model`, `temperature`, `max_tokens` and `timeout` — while every
    other consumer re-reads the policy per decision and `enabled` is in
    `RouterService._HOT_KEYS`. So flipping `enabled: false -> true` in the console
    turned routing on and left `classify_fn is None`, producing
    `fail_safe_strong / no_classifier`: the same signature as the measured
    trust-grant incident, and a third indistinguishable case. Changing
    `classifier.model` did nothing until the gateway restarted.
    """

    @staticmethod
    def _dp():
        import importlib.util
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("dp_live_classifier",
                                                     root / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_model_the_classifier_dispatches_on_follows_a_hot_edit(
        self, monkeypatch,
    ):
        """Flip `classifier.model` BETWEEN building the handler and calling it."""
        dp = self._dp()
        dispatched = []

        class Llm:
            @staticmethod
            def complete(**kwargs):
                dispatched.append(kwargs["model"])
                return SimpleNamespace(text='{"tier": "T1", "confidence": "high"}')

        ctx = SimpleNamespace(llm=Llm())
        policy = {
            "enabled": True,
            "classifier": {"provider": "zai", "model": "first-model"},
            "rules": [],
            "default": {"action": "classify"},
            "tiers": {f"T{n}": {"model": f"m{n}", "provider": "p"} for n in range(1, 5)},
            "fail_safe": {"profile": "coder", "model": "fs", "provider": "p"},
        }
        monkeypatch.setattr(dp, "_load_router_config", lambda: policy)
        monkeypatch.setattr(dp, "_profile_exists", lambda _p: True)
        handler = dp._make_handler("parent", lambda _args: "inline", ctx=ctx)

        # The edit happens AFTER the handler exists — the whole point.
        policy["classifier"] = {"provider": "zai", "model": "second-model"}
        monkeypatch.setattr(dp, "_route_task", dp._route_task)  # keep the real seam
        handler({"prompt": "an entirely ambiguous request", "profile": "auto"})

        assert dispatched, "the classifier never ran"
        assert dispatched[-1] == "second-model", (
            f"the classifier dispatched on {dispatched[-1]!r} — a value frozen at "
            f"register time, not the live policy"
        )

    def test_a_host_with_no_llm_facade_still_yields_no_classifier(self, monkeypatch):
        """The one half that IS process-stable, and must stay so.

        Whether the host exposes `ctx.llm` cannot change without a new ctx, and
        asking per decision would cost a policy read on every turn that has no
        classifier at all.
        """
        dp = self._dp()
        monkeypatch.setattr(dp, "_profile_exists", lambda _p: True)
        monkeypatch.setattr(dp, "_load_router_config", lambda: {"enabled": True})
        seen = []

        def fake_route(goal, requested_model, classify_fn, *rest):
            seen.append(classify_fn)
            return None

        monkeypatch.setattr(dp, "_route_task", fake_route)
        handler = dp._make_handler("parent", lambda _args: "inline",
                                   ctx=SimpleNamespace())
        handler({"prompt": "task", "profile": "auto"})
        assert seen == [None], "a host with no llm facade must pass no classify_fn"

    def test_the_policy_going_off_mid_decision_fail_safes_instead_of_crashing(
        self, monkeypatch,
    ):
        """The race the per-decision resolve introduces, and its guard.

        `enabled` is hot, so it can flip between the moment the router decides to
        classify and the moment the classifier is resolved. The adapter's Stage-1
        handler already treats a raising classifier as "could not answer" and
        fail-safes, so the guard raises rather than returning a shape no caller
        expects — and the turn still routes.
        """
        dp = self._dp()
        reads = {"n": 0}
        base = {
            "enabled": True,
            "classifier": {"provider": "zai", "model": "m"},
            "rules": [],
            "default": {"action": "classify"},
            "tiers": {f"T{n}": {"model": f"m{n}", "provider": "p"} for n in range(1, 5)},
            "fail_safe": {"profile": "coder", "model": "fs-model", "provider": "p"},
        }

        def load():
            reads["n"] += 1
            # The FIRST read (the routing decision) sees the router on; the read the
            # classifier itself makes sees it off.
            if reads["n"] == 1:
                return dict(base)
            return dict(base, enabled=False)

        monkeypatch.setattr(dp, "_load_router_config", load)
        monkeypatch.setattr(dp, "_profile_exists", lambda _p: True)
        handler = dp._make_handler(
            "parent", lambda _args: "inline",
            ctx=SimpleNamespace(llm=SimpleNamespace(complete=lambda **k: None)),
        )
        answer = json.loads(handler({"prompt": "an entirely ambiguous request",
                                     "profile": "auto"}))
        # It answered at all — no traceback out of the handler — and the fail-safe
        # is what served the turn.
        assert answer.get("failure_kind") != "routing_error", answer
        assert reads["n"] >= 2, "the classifier must have re-read the live policy"
