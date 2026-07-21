"""Automated tests for the delegate_profile plugin.

Run:
    cd /home/rodrigo/hermes-delegate-profile
    python3 -m pytest tests/test_delegate_profile.py -v

These tests exercise the plugin's pure logic (validation, timeout ladder,
profile-existence checks) WITHOUT spawning real hermes subprocesses for the
negative paths. One opt-in integration test (gated on DELEGATE_PROFILE_E2E=1)
does a real cross-profile spawn.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Make the plugin importable: insert the repo root on sys.path so `import
# __init__` works despite the awkward module name. We import it under an alias.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "delegate_profile_plugin", REPO_ROOT / "__init__.py"
)
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)


# ---------------------------------------------------------------------------
# Arg validation
# ---------------------------------------------------------------------------
def test_missing_goal_returns_error():
    out = json.loads(dp.delegate_profile({"profile": "coder"}))
    assert out["error"] == "goal is required"
    assert "success" not in out or out.get("success") is False


def test_missing_profile_returns_error():
    out = json.loads(dp.delegate_profile({"goal": "do something"}))
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
    out = json.loads(
        dp.delegate_profile({"goal": "review code", "profile": "reviwer"})
    )
    assert out["error"] == (
        "Profile 'reviwer' does not exist. "
        "Create it with: hermes profile create reviwer"
    )
    assert out["profile"] == "reviwer"
    assert out["available_profiles"] == ["coder", "reviewer", "tester"]
    # The hint must list the real profiles so the caller can spot the typo.
    assert "coder" in out["hint"]


def test_nonexistent_profile_no_known_profiles(monkeypatch):
    monkeypatch.setattr(dp, "_profile_exists", lambda p: False)
    monkeypatch.setattr(dp, "_list_known_profiles", lambda: [])
    out = json.loads(
        dp.delegate_profile({"goal": "x", "profile": "ghost"})
    )
    assert "does not exist" in out["error"]
    assert out["available_profiles"] == []


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
# Hermes binary resolution
# ---------------------------------------------------------------------------
def test_resolve_hermes_bin_returns_string():
    bin_path = dp._resolve_hermes_bin()
    assert isinstance(bin_path, str) and len(bin_path) > 0


# ---------------------------------------------------------------------------
# post_tool_call hook
# ---------------------------------------------------------------------------
def test_hook_no_op_for_other_tools(caplog):
    dp._on_post_tool_call("read_file", {"profile": "x"}, "ok")
    # Should not warn for non-delegate_task tools.


def test_hook_warns_on_delegate_task_with_profile(caplog):
    caplog.set_level("WARNING")
    dp._on_post_tool_call(
        "delegate_task", {"profile": "coder", "goal": "x"}, "ok"
    )
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
    out = json.loads(
        dp.delegate_profile(
            {"goal": "Reply with exactly: PONG", "profile": target, "timeout": 120}
        )
    )
    assert out.get("success") is True, out
    assert out["profile"] == target
    assert "result" in out
    assert isinstance(out["elapsed_s"], (int, float))
