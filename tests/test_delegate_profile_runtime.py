"""Hermetic runtime-path tests for the delegate_profile plugin.

These tests exercise subprocess orchestration with fakes. They never invoke the
Hermes CLI or spawn an OS process; process-tree behaviour itself lives in
``test_delegate_profile.py``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("delegate_profile_runtime", REPO_ROOT / "__init__.py")
assert _spec is not None and _spec.loader is not None
_dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dp)


def test_handler_returns_bad_args_when_router_cannot_resolve_profile(monkeypatch):
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: None)
    handler = _dp._make_handler("parent", lambda _args: "inline")
    result = json.loads(handler({"goal": "task"}))
    assert result == {"error": "profile is required", "failure_kind": "bad_args"}


def test_explicit_auto_profile_is_a_sentinel_not_a_profile_name(monkeypatch):
    """profile="auto" asks the router to choose; it is never a real profile.

    Before the fix the sentinel survived a router decline, reached
    _profile_exists("auto"), and produced "Profile 'auto' does not exist. Create it
    with: hermes profile create auto" - instructing the operator to create a
    profile that would then shadow the sentinel. The decline must read as the same
    bad_args the empty case gives.
    """
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: None)
    handler = _dp._make_handler("parent", lambda _args: "inline")
    result = json.loads(handler({"goal": "task", "profile": "auto"}))
    assert result == {"error": "profile is required", "failure_kind": "bad_args"}
    # And the router still gets to choose when it can.
    monkeypatch.setattr(_dp, "_route_task", lambda *_a: {"profile": "coder", "model": "m"})
    routed = json.loads(handler({"goal": "task", "profile": "auto"}))
    assert routed.get("error") != "profile is required"


def test_close_pipes_and_tail_bound_memory():
    class Pipe:
        def __init__(self, fail=False):
            self.fail = fail
            self.closed = False

        def close(self):
            self.closed = True
            if self.fail:
                raise OSError("already closed")

    class Proc:
        stdout = Pipe()
        stderr = Pipe(fail=True)

    proc = Proc()
    _dp._close_pipes(proc)
    assert proc.stdout.closed and proc.stderr.closed

    tail = _dp._Tail(cap=3)
    tail.append("abcd")
    tail.append("efgh")
    assert tail.text() == "fgh"


def test_kill_tree_windows_and_posix_lookup_error(monkeypatch):
    class Proc:
        pid = 99
        stdout = None
        stderr = None

        def poll(self):
            return None

        def wait(self, timeout):
            self.wait_timeout = timeout

    proc = Proc()
    called = []
    monkeypatch.setattr(_dp, "IS_WINDOWS", True)
    monkeypatch.setattr(_dp.subprocess, "run", lambda args, **kwargs: called.append((args, kwargs)))
    _dp._kill_tree(proc, None, 0.1)
    assert called[0][0][:3] == ["taskkill", "/F", "/T"]

    monkeypatch.setattr(_dp, "IS_WINDOWS", False)
    monkeypatch.setattr(_dp.os, "getpgid", lambda _pid: (_ for _ in ()).throw(ProcessLookupError()))
    _dp._kill_tree(Proc(), None, 0.1)


def test_get_pool_registers_once_and_pool_swallows_kill_error(monkeypatch):
    registrations = []
    monkeypatch.setattr(_dp, "_POOL", None)
    monkeypatch.setattr(_dp.atexit, "register", lambda fn: registrations.append(fn))
    pool = _dp._get_pool()
    assert pool is _dp._get_pool()
    assert registrations == [pool.kill_all]

    class Proc:
        pid = 1

    pool.register(Proc(), 1, {"profile": "child"})
    monkeypatch.setattr(_dp, "_kill_tree", lambda *_args: (_ for _ in ()).throw(RuntimeError("kill")))
    pool.kill_all()
    assert pool.snapshot() == []


def test_register_exposes_schema_and_hook(monkeypatch):
    class Ctx:
        def __init__(self):
            self.tools = []
            self.hooks = []

        def dispatch_tool(self, name, args):
            return f"{name}:{args['goal']}"

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_hook(self, *args, **kwargs):
            self.hooks.append((args, kwargs))

    monkeypatch.setattr(_dp, "_get_active_profile_name", lambda: "parent")
    ctx = Ctx()
    _dp.register(ctx)
    assert ctx.tools[0]["name"] == "delegate_profile"
    assert ctx.tools[0]["schema"]["parameters"]["required"] == ["goal"]
    assert ctx.hooks


def test_resolve_active_profile_and_profile_fallbacks(monkeypatch, tmp_path):
    import types

    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.get_active_profile_name = lambda: "active"
    profiles.profile_exists = lambda name: name == "real"
    cli_package = types.ModuleType("hermes_cli")
    cli_package.profiles = profiles
    monkeypatch.setitem(sys.modules, "hermes_cli", cli_package)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)
    assert _dp._get_active_profile_name() == "active"
    assert _dp._profile_exists("real")
    assert not _dp._profile_exists("missing")

    class Home:
        def __truediv__(self, _other):
            return tmp_path

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: Home()
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.delitem(sys.modules, "hermes_cli.profiles")
    monkeypatch.delitem(sys.modules, "hermes_cli")
    (tmp_path / "child").mkdir()
    assert _dp._profile_exists("child")
    assert not _dp._profile_exists("missing")


def test_resolve_hermes_bin_and_list_profiles_fallback(monkeypatch, tmp_path):
    expected = tmp_path / "hermes"
    monkeypatch.setattr(_dp.sys, "executable", str(tmp_path / "python"))
    expected.write_text("", encoding="utf-8")
    assert _dp._resolve_hermes_bin() == str(expected)

    import types
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "one").mkdir()
    assert _dp._list_known_profiles() == ["one"]


def test_spawn_windows_creation_flag(monkeypatch):
    captured = {}

    class Popen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(_dp, "IS_WINDOWS", True)
    monkeypatch.setattr(_dp.subprocess, "Popen", Popen)
    _dp._spawn(["cmd"], {"X": "1"})
    assert "creationflags" in captured["kwargs"]
    assert "start_new_session" not in captured["kwargs"]


def test_kill_tree_posix_escalates_and_tolerates_killpg_error(monkeypatch):
    class Proc:
        pid = 11
        stdout = None
        stderr = None
        calls = 0

        def poll(self):
            return None

        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise _dp.subprocess.TimeoutExpired("cmd", timeout)

    signals = []
    monkeypatch.setattr(_dp, "IS_WINDOWS", False)
    monkeypatch.setattr(_dp.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    _dp._kill_tree(Proc(), 11, 0.1)
    assert [sig for _, sig in signals] == [_dp.signal.SIGTERM, _dp.signal.SIGKILL]

    monkeypatch.setattr(_dp.os, "killpg", lambda *_args: (_ for _ in ()).throw(OSError("gone")))
    _dp._kill_tree(Proc(), 11, 0.1)

    monkeypatch.setattr(_dp.os, "killpg", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    _dp._kill_tree(Proc(), 11, 0.1)

    class TimeoutProc:
        pid = 12
        stdout = None
        stderr = None

        def poll(self):
            return None

        def wait(self, timeout):
            raise _dp.subprocess.TimeoutExpired("cmd", timeout)

    monkeypatch.setattr(_dp.os, "killpg", lambda *_args: None)
    _dp._kill_tree(TimeoutProc(), 12, 0.1)


def test_breaker_outcome_no_model_and_error_is_nonblocking(monkeypatch):
    _dp._record_breaker_outcome("child", "", "crash")
    monkeypatch.setattr(_dp, "_load_router_config", lambda: (_ for _ in ()).throw(RuntimeError("bad")))
    _dp._record_breaker_outcome("child", "m", "crash")


def test_register_dispatches_same_profile_through_context(monkeypatch):
    class Ctx:
        def __init__(self):
            self.tool = None

        def dispatch_tool(self, name, args):
            return json.dumps({"name": name, "goal": args["goal"]})

        def register_tool(self, **kwargs):
            self.tool = kwargs

        def register_hook(self, *_args):
            pass

    monkeypatch.setattr(_dp, "_get_active_profile_name", lambda: "parent")
    monkeypatch.setattr(_dp, "_profile_exists", lambda _profile: True)
    ctx = Ctx()
    _dp.register(ctx)
    assert ctx.tool is not None
    result = json.loads(ctx.tool["handler"]({"goal": "task", "profile": "parent"}))
    assert result == {"name": "delegate_task", "goal": "task"}


def test_list_known_profiles_and_windows_kill_failures(monkeypatch, tmp_path):
    import types

    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.list_profiles = lambda: []
    cli_package = types.ModuleType("hermes_cli")
    cli_package.profiles = profiles
    monkeypatch.setitem(sys.modules, "hermes_cli", cli_package)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)
    assert _dp._list_known_profiles() == []

    monkeypatch.delitem(sys.modules, "hermes_cli.profiles")
    monkeypatch.delitem(sys.modules, "hermes_cli")
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    assert _dp._list_known_profiles() == []

    class Proc:
        pid = 42
        stdout = None
        stderr = None

        def poll(self):
            return None

        def kill(self):
            raise RuntimeError("already gone")

        def wait(self, _timeout):
            raise RuntimeError("already gone")

    monkeypatch.setattr(_dp, "IS_WINDOWS", True)
    monkeypatch.setattr(_dp.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("taskkill")))
    _dp._kill_tree(Proc(), None, 0.1)


def test_run_watched_tolerates_closed_pipes_and_late_reap(monkeypatch):
    class Pipe:
        def readline(self):
            raise ValueError("closed")

        def close(self):
            pass

    class Proc:
        stdout = Pipe()
        stderr = Pipe()
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout):
            raise _dp.subprocess.TimeoutExpired("cmd", timeout)

    killed = []
    monkeypatch.setattr(_dp, "IS_WINDOWS", False)
    monkeypatch.setattr(_dp, "_kill_tree", lambda *_args: killed.append(True))
    result = _dp._run_watched(Proc(), 1, 0.1, 0.1, 0.1, 0.01)
    assert result[0] == "exited"
    assert killed == [True]

def test_cross_profile_survives_missing_pgid_and_breaker_scans_later_tier(monkeypatch):
    record_outcome = _dp._record_breaker_outcome
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    monkeypatch.setattr(_dp, "IS_WINDOWS", False)
    monkeypatch.setattr(_dp.os, "getpgid", lambda _pid: (_ for _ in ()).throw(OSError("gone")))
    assert json.loads(handler({"goal": "task", "profile": "child"}))["success"] is True

    calls = []

    class Blocklist:
        def __init__(self, _config):
            pass

        def record_success(self, *args):
            calls.append(args)

    import router.blocklist
    monkeypatch.setattr(_dp, "_record_breaker_outcome", record_outcome)
    monkeypatch.setattr(router.blocklist, "Blocklist", Blocklist)
    monkeypatch.setattr(
        _dp,
        "_load_router_config",
        lambda: {"tiers": {"T1": {"model": "other"}, "T2": {"model": "m", "provider": "later"}}},
    )
    _dp._record_breaker_outcome("child", "m", None)
    assert calls == [("m", "later")]

    monkeypatch.setattr(_dp, "_load_router_config", lambda: {"tiers": {}})
    _dp._record_breaker_outcome("child", "unknown", None)
    assert calls[-1] == ("unknown", "")


class FakeProcess:
    pid = 4321


class FakePool:
    def __init__(self, acquire_result: bool = True) -> None:
        self.acquire_result = acquire_result
        self.registered = []
        self.unregistered = []
        self.released = 0
        self.capacity = 4  # mirrors _Pool.capacity (used in at_capacity error text)

    def acquire(self, _wait: float) -> bool:
        return self.acquire_result

    def register(self, proc, pgid, meta) -> None:
        self.registered.append((proc, pgid, meta))

    def unregister(self, pid: int) -> None:
        self.unregistered.append(pid)

    def release(self) -> None:
        self.released += 1


def _cross_handler(monkeypatch, watched_result=("exited", 0, "result", ""), *, pool=None):
    """Build a cross-profile handler with all host/process seams faked."""
    test_pool = pool or FakePool()
    monkeypatch.setattr(_dp, "_profile_exists", lambda _profile: True)
    monkeypatch.setattr(_dp, "_resolve_hermes_bin", lambda: "hermes")
    monkeypatch.setattr(_dp, "_resolve_ladder", lambda _hard: (1.0, 2.0, 3.0, 0.1))
    monkeypatch.setattr(_dp, "_get_pool", lambda: test_pool)
    monkeypatch.setattr(_dp, "_spawn", lambda _cmd, _env: FakeProcess())
    monkeypatch.setattr(_dp.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(_dp, "_run_watched", lambda *_args: watched_result)
    monkeypatch.setattr(_dp, "_kill_tree", lambda *_args: None)
    monkeypatch.setattr(_dp, "_record_breaker_outcome", lambda *_args: None)
    return _dp._make_handler("parent", lambda _args: "inline"), test_pool


@pytest.mark.parametrize(
    ("watched_result", "expected_kind", "expected_fragment"),
    [
        (("hard_timeout", None, "", "diagnostic"), "hard_timeout", "Hard timeout"),
        (("ttfb_timeout", None, "partial", "diagnostic"), "ttfb_stall", "produced no output"),
        (("idle_timeout", None, "partial", "diagnostic"), "idle_stall", "went silent"),
        (("exited", -9, "", "diagnostic"), "crash_or_oom", "exited abnormally"),
        (("exited", 23, "", "diagnostic"), "nonzero_exit", "exited abnormally"),
    ],
)
def test_cross_profile_failure_envelopes(monkeypatch, watched_result, expected_kind, expected_fragment):
    handler, pool = _cross_handler(monkeypatch, watched_result)
    result = json.loads(handler({"goal": "task", "profile": "child", "model": "model-x"}))
    assert result["success"] is False
    assert result["failure_kind"] == expected_kind
    assert expected_fragment in result["error"]
    assert pool.released == 1
    assert pool.unregistered == [FakeProcess.pid]


def test_cross_profile_success_envelope_and_command(monkeypatch):
    captured = {}
    handler, pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))

    def spawn(cmd, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return FakeProcess()

    monkeypatch.setattr(_dp, "_spawn", spawn)
    result = json.loads(handler({"goal": "task", "context": "background", "profile": "child", "model": "model-x"}))
    assert result["success"] is True
    assert result["result"] == "done"
    assert captured["cmd"] == ["hermes", "-p", "child", "chat", "-q", "Context: background\n\nTask: task", "-m", "model-x"]
    assert captured["env"]["HERMES_PROFILE"] == "child"
    assert captured["env"]["HERMES_DELEGATE_PROFILE_DISABLE"] == "1"
    assert pool.released == 1


def test_cross_profile_treats_quota_exhaustion_with_zero_exit_as_retryable(monkeypatch):
    output = """Initializing agent...
API call failed after 3 retries: HTTP 429 rate limited
Session: child-session
"""
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, output, ""))

    result = json.loads(
        handler({"goal": "task", "profile": "child", "model": "model-x"})
    )

    assert result["success"] is False
    assert result["failure_kind"] == "quota_exhausted"
    assert result["retryable"] is True
    assert "quota exhausted" in result["error"]
    assert "HTTP 429" in result["partial_output"]


def test_cross_profile_refuses_when_pool_is_at_capacity(monkeypatch):
    handler, pool = _cross_handler(monkeypatch, pool=FakePool(acquire_result=False))
    result = json.loads(handler({"goal": "task", "profile": "child", "timeout": 2}))
    assert result["failure_kind"] == "at_capacity"
    assert pool.released == 0


def test_cross_profile_queue_wait_from_config(monkeypatch):
    """queue_wait_seconds from config.yaml feeds the pool acquire wait (0 accepted)."""
    calls = []
    test_pool = FakePool()
    # Track the wait value passed to acquire.
    def _tracking_acquire(_wait: float) -> bool:
        calls.append(_wait)
        return True
    test_pool.acquire = _tracking_acquire
    handler, _ = _cross_handler(monkeypatch, pool=test_pool)
    # _cross_handler stubs _get_pool to return test_pool, but _watchdog_cfg is
    # the real one; stub it so queue_wait_seconds resolves from "config".
    monkeypatch.setattr(_dp, "_watchdog_cfg", lambda: {"queue_wait_seconds": 5}, raising=False)
    result = json.loads(handler({"goal": "task", "profile": "child"}))
    assert result["success"] is True
    assert calls == [5.0], f"acquire should see queue_wait from config, got {calls}"


def test_cross_profile_queue_wait_zero_means_up_to_hard_ceiling(monkeypatch):
    """queue_wait_seconds=0 must be honoured as 'wait up to the hard ceiling'."""
    calls = []
    test_pool = FakePool()
    def _tracking_acquire(_wait: float) -> bool:
        calls.append(_wait)
        return True
    test_pool.acquire = _tracking_acquire
    handler, _ = _cross_handler(monkeypatch, pool=test_pool)
    # _resolve_ladder is stubbed to (1.0, 2.0, 3.0, 0.1) in _cross_handler, so
    # hard=3.0: queue_wait=0 must pass hard (3.0) to acquire, not 0.
    monkeypatch.setattr(_dp, "_watchdog_cfg", lambda: {"queue_wait_seconds": 0}, raising=False)
    result = json.loads(handler({"goal": "task", "profile": "child"}))
    assert result["success"] is True
    assert calls == [3.0], f"acquire should see the hard ceiling when queue_wait=0, got {calls}"


def test_cross_profile_agent_error_on_zero_exit_with_failure_banner(monkeypatch):
    """A child that exits 0 but printed the CLI's post-retry error banner (and
    is NOT a quota exhaustion) is surfaced as a retryable agent_error, not a
    false success."""
    output = "Working…\nAPI call failed after 3 retries: provider returned garbage\n"
    handler, pool = _cross_handler(monkeypatch, ("exited", 0, output, ""))
    result = json.loads(handler({"goal": "task", "profile": "child", "model": "model-x"}))
    assert result["success"] is False
    assert result["failure_kind"] == "agent_error"
    assert result["retryable"] is True
    assert "exiting with code 0" in result["error"]


def test_cross_profile_preserves_existing_hermes_home(monkeypatch):
    """When HERMES_HOME is already set, the child inherits it unchanged (the
    resolve-and-inject branch is skipped)."""
    captured = {}
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    monkeypatch.setenv("HERMES_HOME", "/preset/hermes/home")
    monkeypatch.setattr(
        _dp, "_spawn",
        lambda _cmd, env: captured.setdefault("env", env) and FakeProcess() or FakeProcess(),
    )
    result = json.loads(handler({"goal": "task", "profile": "child"}))
    assert result["success"] is True
    assert captured["env"]["HERMES_HOME"] == "/preset/hermes/home"


@pytest.mark.parametrize(
    ("spawn_error", "expected_kind"),
    [(FileNotFoundError(), "binary_not_found"), (RuntimeError("boom"), "spawn_error")],
)
def test_cross_profile_spawn_errors_are_structured(monkeypatch, spawn_error, expected_kind):
    handler, pool = _cross_handler(monkeypatch)

    def fail_spawn(_cmd, _env):
        raise spawn_error

    monkeypatch.setattr(_dp, "_spawn", fail_spawn)
    result = json.loads(handler({"goal": "task", "profile": "child"}))
    assert result["failure_kind"] == expected_kind
    assert pool.released == 1


def test_handler_router_without_model_windows_group_and_hermes_home(monkeypatch, tmp_path):
    handler, pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: {"profile": "child"})
    routed = json.loads(handler({"goal": "task"}))
    assert routed["success"] is True
    assert pool.registered[0][1] == 4321

    handler, windows_pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    monkeypatch.setattr(_dp, "IS_WINDOWS", True)
    windows = json.loads(handler({"goal": "task", "profile": "child"}))
    assert windows["success"] is True
    assert windows_pool.registered[0][1] is None

    captured = {}
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    monkeypatch.setattr(_dp, "IS_WINDOWS", True)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    import types
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    monkeypatch.setattr(_dp, "_spawn", lambda _cmd, env: captured.setdefault("env", env) and FakeProcess())
    result = json.loads(handler({"goal": "task", "profile": "child"}))
    assert result["success"] is True
    assert captured["env"]["HERMES_HOME"] == str(tmp_path)

    failed_home = {}
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    monkeypatch.setattr(_dp, "IS_WINDOWS", True)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    constants.get_hermes_home = lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))

    def spawn_without_home(_cmd, env):
        failed_home["env"] = env
        return FakeProcess()

    monkeypatch.setattr(_dp, "_spawn", spawn_without_home)
    result = json.loads(handler({"goal": "task", "profile": "child"}))
    assert result["success"] is True
    assert "HERMES_HOME" not in failed_home["env"]


def test_load_router_config_handles_missing_and_invalid_yaml(monkeypatch, tmp_path):
    monkeypatch.setattr(_dp, "__file__", str(tmp_path / "plugin.py"))
    assert _dp._load_router_config() == {}
    (tmp_path / "router.yaml").write_text("not: [valid", encoding="utf-8")
    assert _dp._load_router_config() == {}


def test_classifier_rejects_non_object_json(monkeypatch):
    monkeypatch.setattr(
        _dp,
        "_load_router_config",
        lambda: {"enabled": True, "classifier": {"model": "m", "provider": "p"}},
    )

    class Result:
        text = "[]"

    class LLM:
        def complete(self, **_kwargs):
            return Result()

    class Ctx:
        llm = LLM()

    fn = _dp._make_classify_fn(Ctx())
    assert fn is not None
    with pytest.raises(ValueError, match="JSON object"):
        fn("task", {})


def test_route_task_rejects_incomplete_and_handles_adapter_exception(monkeypatch):
    monkeypatch.setattr(_dp, "_load_router_config", lambda: {"enabled": True})
    import router.adapter

    monkeypatch.setattr(router.adapter, "route", lambda **_kwargs: {"model": "m"})
    assert _dp._route_task("task", "", None) is None

    monkeypatch.setattr(router.adapter, "route", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _dp._route_task("task", "", None) is None


def test_route_task_persists_trace_and_survives_durable_log_failure(monkeypatch, tmp_path):
    """The live routing hook passes a DurableDecisionLog so real decisions are
    persisted for replay — and a broken durable log never breaks routing."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(_dp, "_load_router_config", lambda: {
        "enabled": True,
        "default": {"profile": "coder", "model": "T1"},
        "tiers": {"T1": {"model": "m1", "provider": "p1"}, "T2": {}, "T3": {}, "T4": {}},
        "rules": [{"id": "any", "when": {}, "then": {"profile": "coder", "model": "T1"}}],
    })
    # A concrete route → the durable log writes routes.jsonl.
    result = _dp._route_task("do a thing", "", None)
    assert result is not None and result["profile"] == "coder"
    import router.durable_decision_log as ddl
    trace = ddl.routes_path()
    assert trace.exists()
    assert trace.read_text(encoding="utf-8").strip()  # at least one line

    # If DurableDecisionLog construction itself blows up, routing still returns
    # (the whole hook is best-effort under try/except).
    monkeypatch.setattr(
        ddl, "DurableDecisionLog",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ctor boom")),
    )
    # Import path inside _route_task re-imports the module attribute, so patching
    # the class on the module is what the hook sees.
    result2 = _dp._route_task("do a thing", "", None)
    assert result2 is None or result2.get("profile") == "coder"


# ---------------------------------------------------------------------------
# The executor must attempt the router's PLANNED chain
# ---------------------------------------------------------------------------
#
# The declared order is what the executor used to rebuild, which left the
# capability filter and `fallback_strategy` with no effect on real traffic: a
# vision turn was sent to a tier primary that cannot see images while the
# console showed the filtered chain. These tests pin the plan as the authority.

_BLIND_PRIMARY = {"model": "glm-5.3", "provider": "zai"}
_DECLARED_FALLBACK = [
    {"model": "gpt-5.6-luna", "provider": "openai-codex"},
    {"model": "deepseek-v4-flash", "provider": "deepseek"},
]


def _spawn_recorder(monkeypatch):
    """Capture every spawned argv, returning the list."""
    cmds: list = []

    def spawn(cmd, _env):
        cmds.append(list(cmd))
        return FakeProcess()

    monkeypatch.setattr(_dp, "_spawn", spawn)
    return cmds


def _models_attempted(cmds):
    return [cmd[cmd.index("-m") + 1] for cmd in cmds if "-m" in cmd]


def _providers_attempted(cmds):
    return [cmd[cmd.index("--provider") + 1] for cmd in cmds if "--provider" in cmd]


def test_executor_attempts_the_planned_chain_not_the_declared_order(monkeypatch):
    """The plan drops the blind primary; production must not attempt it at all."""
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    cmds = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: {
        "profile": "child", **_BLIND_PRIMARY,
        "fallback": list(_DECLARED_FALLBACK),
        "chain": [{"model": "gpt-5.6-luna", "provider": "openai-codex"}],
    })

    result = json.loads(handler({"goal": "look at this screenshot"}))

    assert result["success"] is True
    assert result["model"] == "gpt-5.6-luna"
    assert _models_attempted(cmds) == ["gpt-5.6-luna"]
    assert _providers_attempted(cmds) == ["openai-codex"]


def test_executor_fails_over_along_the_planned_order(monkeypatch):
    """A retryable failure walks the PLAN's order, not the declared one."""
    handler, _pool = _cross_handler(monkeypatch)
    cmds = _spawn_recorder(monkeypatch)
    outcomes = iter([("exited", -9, "", "boom"), ("exited", 0, "done", "")])
    monkeypatch.setattr(_dp, "_run_watched",
                        lambda *_args: next(outcomes, ("exited", 0, "done", "")))
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: {
        "profile": "child", **_BLIND_PRIMARY,
        "fallback": list(_DECLARED_FALLBACK),
        # A shuffled plan: neither hop is the declared primary, and the order is
        # the reverse of the declared tail.
        "chain": [{"model": "deepseek-v4-flash", "provider": "deepseek"},
                  {"model": "gpt-5.6-luna", "provider": "openai-codex"}],
    })

    result = json.loads(handler({"goal": "task"}))

    assert result["success"] is True
    assert _models_attempted(cmds) == ["deepseek-v4-flash", "gpt-5.6-luna"]
    assert [a["model"] for a in result["attempts"]] == ["deepseek-v4-flash",
                                                       "gpt-5.6-luna"]


def test_executor_uses_the_declared_order_when_there_is_no_plan(monkeypatch):
    """Back-compat: a decision without a chain still fails over as it always did."""
    handler, _pool = _cross_handler(monkeypatch)
    cmds = _spawn_recorder(monkeypatch)
    outcomes = iter([("exited", -9, "", "boom"), ("exited", 0, "done", "")])
    monkeypatch.setattr(_dp, "_run_watched",
                        lambda *_args: next(outcomes, ("exited", 0, "done", "")))
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: {
        "profile": "child", **_BLIND_PRIMARY, "fallback": list(_DECLARED_FALLBACK),
    })

    result = json.loads(handler({"goal": "task"}))

    assert result["success"] is True
    assert _models_attempted(cmds) == ["glm-5.3", "gpt-5.6-luna"]


def test_an_explicitly_requested_model_stays_the_first_attempt(monkeypatch):
    """An explicit model overrides the routing decision; the plan is its tail."""
    handler, _pool = _cross_handler(monkeypatch)
    cmds = _spawn_recorder(monkeypatch)
    outcomes = iter([("exited", -9, "", "boom"), ("exited", 0, "done", "")])
    monkeypatch.setattr(_dp, "_run_watched",
                        lambda *_args: next(outcomes, ("exited", 0, "done", "")))
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: {
        "profile": "child", **_BLIND_PRIMARY,
        "chain": [{"model": "gpt-5.6-luna", "provider": "openai-codex"}],
    })

    result = json.loads(handler({"goal": "task", "model": "operator-choice"}))

    assert result["success"] is True
    assert _models_attempted(cmds) == ["operator-choice", "gpt-5.6-luna"]


def test_a_target_is_never_attempted_twice(monkeypatch):
    """A repeat target is a wasted subprocess and a second breaker strike."""
    handler, _pool = _cross_handler(monkeypatch)
    cmds = _spawn_recorder(monkeypatch)
    # Retryable on purpose: without the dedupe the loop WOULD try again.
    monkeypatch.setattr(_dp, "_run_watched", lambda *_args: ("exited", -9, "", "boom"))
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: {
        "profile": "child", "model": "gpt-5.6-luna", "provider": "openai-codex",
        "chain": [{"model": "gpt-5.6-luna", "provider": "openai-codex"},
                  {"model": "gpt-5.6-luna", "provider": "openai-codex"}],
    })

    json.loads(handler({"goal": "task", "model": "gpt-5.6-luna"}))

    assert _models_attempted(cmds) == ["gpt-5.6-luna"]


# ---------------------------------------------------------------------------
# The router sizes the turn from the prompt the child actually receives
# ---------------------------------------------------------------------------

def test_router_is_given_the_composed_prompt_not_the_goal_alone(monkeypatch):
    """est_input_tokens has to see the context; the child is sent context+goal."""
    seen: list = []
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    cmds = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(
        _dp, "_route_task",
        lambda *args: seen.append(args) or {"profile": "child"},
    )

    handler({"goal": "fix the failing test", "context": "log line\n" * 2000})

    assert seen[0][0] == "fix the failing test", "the goal stays the goal"
    assert seen[0][3] == _dp._compose_prompt("fix the failing test",
                                             ("log line\n" * 2000).strip())
    # The routed text is byte-identical to the prompt the child was handed.
    assert seen[0][3] == cmds[0][cmds[0].index("-q") + 1]


def test_a_big_context_reaches_the_context_rule_end_to_end(monkeypatch, tmp_path):
    """The F6 defect, at the level it was reported: a trivial goal plus a huge
    context routed as if it were six tokens."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(_dp, "_load_router_config", lambda: {
        "enabled": True,
        "fail_safe": {"profile": "child", "model": "small-rail", "provider": "cheap"},
        "rules": [{"id": "huge-context-read",
                   "when": {"est_input_tokens": {"gt": 20000}},
                   "then": {"profile": "child", "model": "T3"}}],
        "default": {"action": "classify"},
        "tiers": {"T1": {"model": "tiny", "provider": "cheap"},
                  "T2": {"model": "small", "provider": "cheap"},
                  "T3": {"model": "gpt-5.6-terra", "provider": "openai-codex"},
                  "T4": {"model": "gpt-5.5", "provider": "openai-codex"}},
    })
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, "done", ""))
    cmds = _spawn_recorder(monkeypatch)

    handler({"goal": "fix the failing test",
             "context": "WARN retry scheduled for the nightly job\n" * 2000})
    assert _models_attempted(cmds) == ["gpt-5.6-terra"]

    # Same goal, no context: nothing in Table 1 matches and it falls to
    # fail_safe — which is exactly what the big-context turn used to do.
    cmds.clear()
    handler({"goal": "fix the failing test"})
    assert _models_attempted(cmds) == ["small-rail"]


def test_the_live_hook_persists_the_chain_plan_for_replay(monkeypatch, tmp_path):
    """routes.jsonl is the console replay panel's only data source.

    Before this, every live entry omitted chain_plan, so the panel showed its
    empty default forever and no operator could see what the filter dropped.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(_dp, "_load_router_config", lambda: {
        "enabled": True,
        "default": {"action": "classify"},
        "rules": [{"id": "vision-required", "when": {"needs_vision": {"eq": True}},
                   "then": {"profile": "coder", "model": "T2"}}],
        "tiers": {
            "T1": {"model": "glm-4.7", "provider": "zai"},
            "T2": {"model": "glm-5.3", "provider": "zai",
                   "fallback": [{"model": "gpt-5.6-luna", "provider": "openai-codex"},
                                {"model": "deepseek-v4-flash", "provider": "deepseek"}]},
            "T3": {"model": "gpt-5.6-terra", "provider": "openai-codex"},
            "T4": {"model": "gpt-5.5", "provider": "openai-codex"},
        },
    })

    routed = _dp._route_task("look at this screenshot of the ui", "", None)
    assert _dp._routed_targets(routed) == [("gpt-5.6-luna", "openai-codex")]

    import router.durable_decision_log as ddl
    entry = json.loads(
        [l for l in ddl.routes_path().read_text(encoding="utf-8").splitlines() if l.strip()][-1]
    )
    plan = entry["chain_plan"]
    assert [hop["model"] for hop in plan["chain"]] == ["gpt-5.6-luna"]
    assert {hop["reject_reason"] for hop in plan["rejected"]} == {"no_vision"}
    assert isinstance(entry["steps"][1]["in"]["seed"], int), \
        "the ordering seed must be replayable from the persisted trace"


def test_compose_prompt_is_the_single_definition_of_the_child_prompt():
    """Two copies of this composition is how the context signal went blind."""
    assert _dp._compose_prompt("goal", "ctx") == "Context: ctx\n\nTask: goal"
    assert _dp._compose_prompt("goal", "") == "goal"


def test_routed_targets_degrades_and_dedupes():
    """The executor's target derivation, on the shapes it really receives."""
    planned = {"model": "declared", "provider": "a",
               "fallback": [{"model": "hop", "provider": "b"}],
               "chain": [{"model": "planned", "provider": "c"}]}
    assert _dp._routed_targets(planned) == [("planned", "c")]
    assert _dp._routed_targets({"model": "declared", "provider": "a",
                                "fallback": [{"model": "hop", "provider": "b"}]}) == [
        ("declared", "a"), ("hop", "b")]
    assert _dp._routed_targets({"profile": "child"}) == []
    assert _dp._routed_targets({"model": "m", "chain": "not-a-list"}) == [("m", "")]
    assert _dp._routed_targets({"model": "m", "chain": [{"provider": "no-model"}]}) == [
        ("m", "")]
    assert _dp._routed_targets("not a mapping") == []
    assert _dp._dedupe_targets([("a", "x"), ("a", "x"), ("b", "y")]) == [
        ("a", "x"), ("b", "y")]


def test_record_breaker_outcome_dispatches_success_and_failure(monkeypatch):
    calls = []

    class Blocklist:
        def __init__(self, _config):
            pass

        def record_failure(self, *args):
            calls.append(("failure", args))

        def record_success(self, *args):
            calls.append(("success", args))

    import router.blocklist

    monkeypatch.setattr(router.blocklist, "Blocklist", Blocklist)
    monkeypatch.setattr(
        _dp,
        "_load_router_config",
        lambda: {"tiers": {"T1": {"model": "m", "provider": "p"}}},
    )
    _dp._record_breaker_outcome("child", "m", "ttfb_stall")
    _dp._record_breaker_outcome("child", "m", None)
    assert calls == [("failure", ("m", "p", "ttfb_stall")), ("success", ("m", "p"))]

    # The ATTEMPTED provider wins over the policy scan: the hop that ran is the
    # one the breaker must be keyed against, even when policy declares the same
    # model elsewhere under a different provider.
    calls.clear()
    _dp._record_breaker_outcome("child", "m", "quota_exhausted", "attempted")
    assert calls == [("failure", ("m", "attempted", "quota_exhausted"))]

    # And a hop that is ONLY ever a fallback (never a tier primary) is still
    # resolvable when a caller cannot name it — a primaries-only scan returned ""
    # for exactly the elos that only appear in a chain tail.
    calls.clear()
    monkeypatch.setattr(
        _dp,
        "_load_router_config",
        lambda: {"tiers": {"T2": {"model": "m", "provider": "p",
                                 "fallback": [{"model": "hop", "provider": "hp"}]}}},
    )
    _dp._record_breaker_outcome("child", "hop", "quota_exhausted")
    assert calls == [("failure", ("hop", "hp", "quota_exhausted"))]


# ---------------------------------------------------------------------------
# The breaker must bind under the key the routing path actually reads
# ---------------------------------------------------------------------------
#
# Blocklist keys breaker state as ``model@provider`` and routing asks
# ``is_blocked(model, provider)``; the console's liveness panel looks the same key
# up. The executor recorded outcomes with the attempted MODEL only, and the
# provider was re-derived by scanning tier PRIMARIES — so every fallback hop was
# recorded under a bare model key that neither surface reads. A quota-exhausted
# rail therefore kept being attempted while its breaker sat tripped in a cell
# nothing consulted: the recurring defect, with the DISPLAY (a tripped breaker in
# `hermes router blocklist`) disagreeing with the path that RUNS.

# gpt-5.6-luna is reachable on TWO rails: as T2's fallback hop on openai-codex
# (the one the executor attempts) and as T4's primary on openai-direct. No scan of
# the policy can tell which one just failed — only the attempt knows — so this
# shape is what forces the attempted provider to be passed through rather than
# re-derived, and it also pins that one rail's breaker never takes out its twin.
_TWO_RAIL_POLICY = {
    "enabled": True,
    "blocklist": {
        "auto_breaker": {
            "enabled": True,
            "threshold": 5,
            "window_seconds": 600,
            "base_cooldown_seconds": 60,
            "max_cooldown_seconds": 900,
            "backoff_multiplier": 2.0,
        }
    },
    "tiers": {
        "T1": {"model": "glm-4.7", "provider": "zai"},
        "T2": {"model": "glm-5.3", "provider": "zai",
               "fallback": [{"model": "gpt-5.6-luna", "provider": "openai-codex"}]},
        "T3": {"model": "mimo-v2.5", "provider": "xiaomi"},
        "T4": {"model": "gpt-5.6-luna", "provider": "openai-direct"},
    },
}

_QUOTA_OUTPUT = "API call failed after 3 retries: HTTP 429 rate limited\n"


def test_a_failing_fallback_hop_binds_the_breaker_every_surface_reads(
    monkeypatch, tmp_path
):
    """One failed hop, three surfaces, one key — the agreement IS the assertion.

    The executor records the outcome, the routing path asks
    ``is_blocked(model, provider)``, and ``RouterService.liveness`` renders the
    same ``model@provider``. Asserting only that the breaker "tripped" would have
    passed while the trip was filed under a key neither reads; asserting only the
    display would have passed too. So all three are read back for the hop that
    actually ran.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    real_record = _dp._record_breaker_outcome
    handler, _pool = _cross_handler(monkeypatch, ("exited", 0, _QUOTA_OUTPUT, ""))
    # _cross_handler stubs the breaker seam out; this test is about it.
    monkeypatch.setattr(_dp, "_record_breaker_outcome", real_record)
    monkeypatch.setattr(_dp, "_load_router_config",
                        lambda: copy.deepcopy(_TWO_RAIL_POLICY))
    monkeypatch.setattr(_dp, "_route_task", lambda *_args: {
        "profile": "child", **_BLIND_PRIMARY,
        # A vision turn: the blind primary is filtered out, so the ONLY eligible
        # target is the fallback hop — precisely the elo whose breaker never bound.
        "chain": [{"model": "gpt-5.6-luna", "provider": "openai-codex"}],
    })

    result = json.loads(handler({"goal": "look at this screenshot"}))
    assert result["model"] == "gpt-5.6-luna" and result["provider"] == "openai-codex"
    assert result["failure_kind"] == "quota_exhausted"

    from router.blocklist import Blocklist
    blocklist = Blocklist(copy.deepcopy(_TWO_RAIL_POLICY))

    # 1. The RUNNING path: what routing asks before attempting a target.
    assert blocklist.is_blocked("gpt-5.6-luna", "openai-codex") is True, (
        "the breaker must bind for the (model, provider) that actually failed"
    )
    # 2. The recorded key is the one both surfaces read — and it is the rail that
    #    failed, not the other rail serving the same elo, whose quota is intact.
    assert [entry["model_key"] for entry in blocklist.breaker_status()] == [
        "gpt-5.6-luna@openai-codex"
    ]
    assert blocklist.is_blocked("gpt-5.6-luna", "openai-direct") is False, (
        "one rail's breaker must not take out a healthy rail for the same elo"
    )
    # 3. The REPORTING surface: the console's liveness panel resolves the same key.
    policy_path = tmp_path / "router.yaml"
    policy_path.write_text(
        yaml.safe_dump(_TWO_RAIL_POLICY, sort_keys=False), encoding="utf-8"
    )
    from router.service import RouterService
    panel = {
        entry["model_key"]: entry
        for entry in RouterService(policy_path).liveness()["models"]
    }
    hop = panel["gpt-5.6-luna@openai-codex"]
    # The panel must land on the SAME breaker entry the routing path binds on.
    # Which non-alive label it picks (``degraded`` vs ``quota_exhausted``) is the
    # breaker module's own reporting, so the agreement asserted here is the key
    # and the OPEN state — not the wording.
    assert hop["breaker"].get("state") == "OPEN", (
        "the panel and the routing path must describe one breaker, not two"
    )
    assert hop["state"] != "alive"
    for healthy in ("glm-5.3@zai", "gpt-5.6-luna@openai-direct"):
        assert panel[healthy]["state"] == "alive", (
            "only the rail that actually ran may be marked"
        )
