"""Hermetic runtime-path tests for the delegate_profile plugin.

These tests exercise subprocess orchestration with fakes. They never invoke the
Hermes CLI or spawn an OS process; process-tree behaviour itself lives in
``test_delegate_profile.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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
