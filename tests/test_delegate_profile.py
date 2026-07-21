"""Automated tests for the delegate_profile plugin.

Run:
    cd /home/rodrigo/hermes-delegate-profile
    /usr/local/lib/hermes-agent/venv/bin/python -m pytest tests/ -v

These tests exercise the plugin's pure logic (validation, timeout ladder,
profile-existence checks) WITHOUT spawning real hermes subprocesses for the
negative paths. One opt-in integration test (gated on DELEGATE_PROFILE_E2E=1)
does a real cross-profile spawn.

The handler is built by ``_make_handler(current_profile, dispatch_delegate)``
at register time, so these tests construct it the same way, passing a fake
``dispatch_delegate`` (records calls) and a fixed ``current_profile``.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Make the plugin importable despite the awkward ``__init__.py`` module name.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "delegate_profile_plugin", REPO_ROOT / "__init__.py"
)
assert _spec is not None and _spec.loader is not None, "could not load plugin spec"
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)


def _make_handler(current_profile="default", captured_dispatch=None):
    """Build the real handler against a recording dispatch_delegate.

    ``captured_dispatch`` (a list) accumulates the args dicts handed to the
    inline delegate_task path, so tests can assert on them.
    """
    def dispatch_delegate(dt_args):
        if captured_dispatch is not None:
            captured_dispatch.append(dt_args)
        return json.dumps({"success": True, "results": [{"goal": dt_args.get("goal")}]})

    return dp._make_handler(current_profile, dispatch_delegate)


# ---------------------------------------------------------------------------
# Arg validation
# ---------------------------------------------------------------------------
def test_missing_goal_returns_error():
    h = _make_handler()
    out = json.loads(h({"profile": "coder"}))
    assert out["error"] == "goal is required"


def test_missing_profile_returns_error():
    h = _make_handler()
    out = json.loads(h({"goal": "do something"}))
    assert out["error"] == "profile is required"


# ---------------------------------------------------------------------------
# Profile existence validation
# ---------------------------------------------------------------------------
def test_nonexistent_profile_gives_clear_error(monkeypatch):
    """A typo'd profile name must produce an instant, actionable error — not
    a confusing subprocess failure."""
    monkeypatch.setattr(dp, "_profile_exists", lambda p: False)
    monkeypatch.setattr(
        dp, "_list_known_profiles", lambda: ["coder", "reviewer", "tester"]
    )
    h = _make_handler(current_profile="default")
    out = json.loads(h({"goal": "review code", "profile": "reviwer"}))
    assert out["error"] == (
        "Profile 'reviwer' does not exist. "
        "Create it with: hermes profile create reviwer"
    )
    assert out["profile"] == "reviwer"
    assert out["available_profiles"] == ["coder", "reviewer", "tester"]
    # The hint must list the real profiles so the caller can spot the typo.
    assert "coder" in out["hint"]


def test_nonexistent_profile_validated_before_sameprofile_shortcut(monkeypatch):
    """Even if the typo coincidentally matches current_profile, the existence
    check runs first — so 'default' on a system with no such dir still errors
    cleanly (except the real default, which always exists)."""
    monkeypatch.setattr(dp, "_profile_exists", lambda p: False)
    monkeypatch.setattr(dp, "_list_known_profiles", lambda: ["coder"])
    h = _make_handler(current_profile="defualt")
    out = json.loads(h({"goal": "x", "profile": "defualt"}))
    assert "does not exist" in out["error"]


def test_nonexistent_profile_no_known_profiles(monkeypatch):
    monkeypatch.setattr(dp, "_profile_exists", lambda p: False)
    monkeypatch.setattr(dp, "_list_known_profiles", lambda: [])
    h = _make_handler()
    out = json.loads(h({"goal": "x", "profile": "ghost"}))
    assert "does not exist" in out["error"]
    assert out["available_profiles"] == []
    assert "hermes profile list" in out["hint"]


# ---------------------------------------------------------------------------
# Same-profile inline path
# ---------------------------------------------------------------------------
def test_same_profile_routes_to_inline_dispatch(monkeypatch):
    """When profile == current, we must NOT spawn; we route to delegate_task."""
    monkeypatch.setattr(dp, "_profile_exists", lambda p: True)
    captured = []
    h = _make_handler(current_profile="default", captured_dispatch=captured)
    out = json.loads(h({"goal": "g", "context": "c", "profile": "default", "model": "m"}))
    assert out["success"] is True
    # delegate_task was called once with forwarded goal/context/model.
    assert captured == [{"goal": "g", "context": "c", "model": "m"}]


def test_inline_dispatch_failure_returns_error(monkeypatch):
    monkeypatch.setattr(dp, "_profile_exists", lambda p: True)

    def boom(_args):
        raise RuntimeError("parent agent not available")

    h = dp._make_handler("default", boom)
    out = json.loads(h({"goal": "g", "profile": "default"}))
    assert "Inline delegation failed" in out["error"]


# ---------------------------------------------------------------------------
# Timeout resolution ladder
# ---------------------------------------------------------------------------
def test_timeout_default():
    assert dp._resolve_timeout(None) == 300
    assert dp._resolve_timeout("") == 300


def test_timeout_explicit_arg_wins(monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_TIMEOUT", "999")
    assert dp._resolve_timeout(120) == 120


def test_timeout_env_used_when_no_arg(monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_TIMEOUT", "600")
    assert dp._resolve_timeout(None) == 600


def test_timeout_invalid_arg_falls_through(monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_TIMEOUT", "200")
    assert dp._resolve_timeout("not-a-number") == 200


def test_timeout_invalid_env_falls_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_TIMEOUT", "garbage")
    assert dp._resolve_timeout(None) == 300


def test_timeout_zero_or_negative_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_TIMEOUT", "50")
    assert dp._resolve_timeout(0) == 50
    assert dp._resolve_timeout(-5) == 50


# ---------------------------------------------------------------------------
# Profile existence helpers (real environment)
# ---------------------------------------------------------------------------
def test_default_profile_always_exists():
    """The 'default' profile is special and must always resolve."""
    assert dp._profile_exists("default") is True


def test_real_profile_detected():
    """Whatever profiles actually exist on disk, listing + existence agree."""
    known = dp._list_known_profiles()
    if not known:
        pytest.skip("no profiles on disk in this environment")
    for name in known:
        assert dp._profile_exists(name), f"{name} listed but _profile_exists=False"


# ---------------------------------------------------------------------------
# Hermes binary + active profile resolution
# ---------------------------------------------------------------------------
def test_resolve_hermes_bin_returns_string():
    bin_path = dp._resolve_hermes_bin()
    assert isinstance(bin_path, str) and len(bin_path) > 0


def test_get_active_profile_name_returns_string():
    name = dp._get_active_profile_name()
    assert isinstance(name, str) and len(name) > 0


# ---------------------------------------------------------------------------
# post_tool_call hook
# ---------------------------------------------------------------------------
def test_hook_no_op_for_other_tools(caplog):
    dp._on_post_tool_call("read_file", {"profile": "x"}, "ok")
    # Should not warn for non-delegate_task tools.


def test_hook_warns_on_delegate_task_with_profile(caplog):
    caplog.set_level("WARNING")
    dp._on_post_tool_call("delegate_task", {"profile": "coder", "goal": "x"}, "ok")
    assert any("delegate_profile" in r.message for r in caplog.records)


def test_hook_silent_for_delegate_task_without_profile(caplog):
    caplog.set_level("WARNING")
    dp._on_post_tool_call("delegate_task", {"goal": "x"}, "ok")
    assert not caplog.records


# ---------------------------------------------------------------------------
# Opt-in E2E: real cross-profile spawn
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    os.environ.get("DELEGATE_PROFILE_E2E") != "1",
    reason="set DELEGATE_PROFILE_E2E=1 to run a real cross-profile spawn",
)
def test_e2e_cross_profile_spawn():
    """Real spawn into a profile that exists. Requires the profile + a working
    model for that profile. Skipped unless DELEGATE_PROFILE_E2E=1."""
    target = os.environ.get("DELEGATE_PROFILE_E2E_PROFILE", "tester")
    if not dp._profile_exists(target):
        pytest.skip(f"profile {target!r} does not exist")
    # current_profile intentionally differs from target to force the spawn path.
    h = dp._make_handler("default", lambda a: json.dumps({}))
    out = json.loads(
        h({"goal": "Reply with exactly: PONG", "profile": target, "timeout": 120})
    )
    assert out.get("success") is True, out
    assert out["profile"] == target
    assert "result" in out
    assert isinstance(out["elapsed_s"], (int, float))


# ===========================================================================
# Hardening: process-tree lifecycle, watchdog ladder, classification, pool
# (appended block — see top of file for imports/plugin loading)
# ===========================================================================
import signal
import subprocess
import time as _time


# ---------------------------------------------------------------------------
# Timeout-ladder resolution + ordering invariant
# ---------------------------------------------------------------------------
def test_ladder_defaults():
    ttfb, idle, hard, grace = dp._resolve_ladder(300)
    assert (ttfb, idle, hard, grace) == (60.0, 180.0, 300.0, 10.0)


def test_ladder_clamps_under_hard_ceiling():
    """idle/ttfb can never exceed the hard ceiling — a 30s ceiling shrinks both."""
    ttfb, idle, hard, _ = dp._resolve_ladder(30)
    assert hard == 30.0
    assert idle <= hard
    assert ttfb <= idle


def test_ladder_env_overrides(monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_TTFB", "5")
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_IDLE", "20")
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_KILL_GRACE", "3")
    ttfb, idle, hard, grace = dp._resolve_ladder(300)
    assert (ttfb, idle, grace) == (5.0, 20.0, 3.0)


def test_ladder_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATE_PROFILE_IDLE", "not-a-number")
    _, idle, _, _ = dp._resolve_ladder(300)
    assert idle == 180.0


# ---------------------------------------------------------------------------
# Failure classification: (reason, returncode) -> (failure_kind, retryable)
# ---------------------------------------------------------------------------
def test_classify_success():
    assert dp._classify("exited", 0) == (None, False)


def test_classify_nonzero_exit_not_retryable():
    assert dp._classify("exited", 1) == ("nonzero_exit", False)


def test_classify_hard_timeout_retryable():
    assert dp._classify("hard_timeout", None) == ("hard_timeout", True)


def test_classify_stalls_retryable():
    assert dp._classify("ttfb_timeout", None) == ("ttfb_stall", True)
    assert dp._classify("idle_timeout", None) == ("idle_stall", True)


def test_classify_signal_death():
    # POSIX: killed-by-signal shows as negative returncode.
    assert dp._classify("exited", -signal.SIGKILL) == ("crash_or_oom", True)
    assert dp._classify("exited", -signal.SIGSEGV) == ("crash", True)


# ---------------------------------------------------------------------------
# Process-tree kill: the CORE fix — grandchildren must not be orphaned.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group test")
def test_kill_tree_reaps_grandchild():
    """Spawn parent -> grandchild(sleep 60). _kill_tree must kill BOTH.

    This is the regression guard for the orphan bug: a plain proc.kill()
    would leave the grandchild alive (reparented to init). We assert the
    grandchild PID is gone after _kill_tree.
    """
    # Parent prints its grandchild's PID, then both sleep.
    script = "sleep 60 & echo $! ; sleep 60"
    proc = dp._spawn(["bash", "-c", script], dict(os.environ))
    pgid = os.getpgid(proc.pid)
    # Read the grandchild PID the parent just printed.
    grandchild_pid = int(proc.stdout.readline().strip())
    # Confirm it's actually alive before we kill.
    os.kill(grandchild_pid, 0)  # raises if not running

    dp._kill_tree(proc, pgid, grace=3.0)

    # Give the kernel a beat to tear the tree down.
    deadline = _time.monotonic() + 5
    alive = True
    while _time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
            _time.sleep(0.1)
        except ProcessLookupError:
            alive = False
            break
    assert not alive, f"grandchild {grandchild_pid} orphaned — tree-kill failed"
    assert proc.poll() is not None, "leader not reaped"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group test")
def test_kill_tree_idempotent_on_dead_proc():
    """Killing an already-exited process must not raise."""
    proc = dp._spawn(["bash", "-c", "true"], dict(os.environ))
    proc.wait(timeout=5)
    dp._kill_tree(proc, None, grace=1.0)  # should be a no-op, not an error


# ---------------------------------------------------------------------------
# Watchdog: TTFB / idle / hard all fire and tree-kill.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watchdog test")
def test_watched_ttfb_timeout():
    """No output at all -> ttfb_timeout kills it fast (well under idle/hard)."""
    proc = dp._spawn(["bash", "-c", "sleep 30"], dict(os.environ))
    pgid = os.getpgid(proc.pid)
    t0 = _time.monotonic()
    reason, rc, out, err = dp._run_watched(
        proc, pgid, ttfb=1.0, idle=10.0, hard=20.0, grace=2.0
    )
    assert reason == "ttfb_timeout"
    assert _time.monotonic() - t0 < 5, "ttfb watchdog too slow"
    assert proc.poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watchdog test")
def test_watched_idle_timeout():
    """Emits one line (clears TTFB), then goes silent -> idle_timeout."""
    proc = dp._spawn(["bash", "-c", "echo hello; sleep 30"], dict(os.environ))
    pgid = os.getpgid(proc.pid)
    reason, rc, out, err = dp._run_watched(
        proc, pgid, ttfb=10.0, idle=1.0, hard=20.0, grace=2.0
    )
    assert reason == "idle_timeout"
    assert "hello" in out  # partial output preserved
    assert proc.poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watchdog test")
def test_watched_hard_timeout_despite_activity():
    """Chatty child that NEVER idles must still hit the hard ceiling."""
    # Prints every 0.2s forever -> idle never trips, only the hard ceiling.
    proc = dp._spawn(
        ["bash", "-c", "while true; do echo tick; sleep 0.2; done"],
        dict(os.environ),
    )
    pgid = os.getpgid(proc.pid)
    reason, rc, out, err = dp._run_watched(
        proc, pgid, ttfb=5.0, idle=5.0, hard=1.0, grace=2.0
    )
    assert reason == "hard_timeout"
    assert proc.poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watchdog test")
def test_watched_clean_exit_captures_output():
    """A fast, well-behaved child exits cleanly with full output captured."""
    proc = dp._spawn(["bash", "-c", "echo line1; echo line2"], dict(os.environ))
    pgid = os.getpgid(proc.pid)
    reason, rc, out, err = dp._run_watched(
        proc, pgid, ttfb=5.0, idle=5.0, hard=10.0, grace=2.0
    )
    assert reason == "exited"
    assert rc == 0
    assert "line1" in out and "line2" in out


# ---------------------------------------------------------------------------
# Bounded-concurrency pool + live-child registry
# ---------------------------------------------------------------------------
def test_pool_caps_concurrency():
    pool = dp._Pool(max_concurrent=2)
    assert pool.acquire(0) is True
    assert pool.acquire(0) is True
    # Third acquire with a short bounded wait must fail (cap reached).
    assert pool.acquire(0.2) is False
    pool.release()
    assert pool.acquire(0.2) is True


def test_pool_release_is_bounded():
    """Over-release must not silently inflate the cap (BoundedSemaphore)."""
    pool = dp._Pool(max_concurrent=1)
    pool.release()  # extra release — must be swallowed, not raise
    assert pool.acquire(0) is True
    assert pool.acquire(0.2) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group test")
def test_pool_kill_all_reaps_registered_children():
    pool = dp._Pool(max_concurrent=4)
    procs = []
    for _ in range(2):
        p = dp._spawn(["bash", "-c", "sleep 30"], dict(os.environ))
        pool.register(p, os.getpgid(p.pid), {"subagent_id": "t", "profile": "x"})
        procs.append(p)
    assert len(pool.snapshot()) == 2
    pool.kill_all(grace=2.0)
    for p in procs:
        assert p.poll() is not None, "kill_all left a child alive"
    assert pool.snapshot() == []


def test_pool_snapshot_shape():
    pool = dp._Pool(max_concurrent=2)

    class _Fake:
        pid = 4242

    pool.register(_Fake(), 4242, {"subagent_id": "dp_x", "profile": "coder"})
    snap = pool.snapshot()
    assert snap and snap[0]["pid"] == 4242
    assert snap[0]["profile"] == "coder"
    pool.unregister(4242)
    assert pool.snapshot() == []
