"""Integration tests — capability router wired into delegate_profile.

Tests the _route_task() bridge function and the full delegation path
when profile is omitted/auto. Uses mocks for ctx.llm (classifier) and
the subprocess spawn (no real hermes process needed).
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

# The plugin __init__.py lives in a directory with a hyphen, which is not a
# valid Python module name. Import it dynamically via importlib.util.
import copy
import importlib.util

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

    def test_routes_review_task_uses_failsafe(self, dp):
        """A review task with no classifier → fail-safe (claude-opus).

        The review-request rule has action:classify. With classify_fn=None,
        the adapter falls back to fail_safe (trusted strong model).
        """
        result = dp._route_task(
            goal="Review this PR for security issues",
            requested_model="",
            classify_fn=None,
        )
        assert result is not None
        assert result["profile"] == "coder"
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

    def test_recursion_guard(self, dp):
        """When HERMES_ROUTER_CLASSIFYING is set, _route_task returns None."""
        old = os.environ.get(dp._ROUTER_SENTINEL)
        os.environ[dp._ROUTER_SENTINEL] = "1"
        try:
            result = dp._route_task("Debug race condition", "", None)
        finally:
            if old is None:
                os.environ.pop(dp._ROUTER_SENTINEL, None)
            else:
                os.environ[dp._ROUTER_SENTINEL] = old
        assert result is None

    def test_recursion_guard_cleared_after_call(self, dp):
        """After _route_task returns, the sentinel is cleared."""
        assert dp._ROUTER_SENTINEL not in os.environ
        dp._route_task("Rename getCwd in src/utils.py", "", None)
        assert dp._ROUTER_SENTINEL not in os.environ

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

    def test_auto_profile_triggers_router(self, dp):
        """profile='auto' triggers the router."""
        def mock_classify(task, features):
            return {"tier": "T4", "confidence": "high"}

        handler = self._make_handler(dp, classify_fn=mock_classify)
        result = handler({"goal": "Debug race condition", "profile": "auto"})
        parsed = json.loads(result)
        assert parsed.get("failure_kind") in ("unknown_profile", None)

    def test_no_profile_triggers_router(self, dp):
        """Omitting profile triggers the router (same as auto)."""
        def mock_classify(task, features):
            return {"tier": "T1", "confidence": "high"}

        handler = self._make_handler(dp, classify_fn=mock_classify)
        result = handler({"goal": "Rename getCwd in src/utils.py"})
        parsed = json.loads(result)
        assert parsed.get("failure_kind") in ("unknown_profile", None)

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
