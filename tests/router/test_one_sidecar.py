"""Pure dispatcher and token-resolution tests for the Hermes One sidecar.

`test_one_sidecar_e2e.py` owns real loopback HTTP coverage. This file keeps the
fast no-socket cases: token gate outcomes, route dispatch and token precedence.
"""
from __future__ import annotations

from typing import Optional

import yaml

from router.one_sidecar import SidecarApp, read_expected_token, resolve_token_path
from router.service import RouterService

_TOKEN = "s3cr3t-token-value"


def _config_path(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "router.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "classifier": {"model": "judge", "provider": "judge-rail"},
                "fail_safe": {"profile": "coder", "model": "strong", "provider": "safe"},
                "blocklist": {"manual_ban": [], "fallback_chain": [], "auto_breaker": {"enabled": False}},
                "rules": [
                    {
                        "id": "hard-verbs",
                        "status": "stable",
                        "when": {"verb_class": {"eq": "hard"}},
                        "then": {"profile": "coder", "model": "T4"},
                    }
                ],
                "default": {"action": "classify"},
                "tiers": {
                    "T1": {"model": "tiny", "provider": "cheap"},
                    "T2": {"model": "small", "provider": "cheap"},
                    "T3": {"model": "medium", "provider": "strong"},
                    "T4": {"model": "strong", "provider": "strong"},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _app(tmp_path, token: Optional[str] = _TOKEN):
    token_path = tmp_path / "token"
    if token is not None:
        token_path.write_text(token, encoding="utf-8")
    return SidecarApp(RouterService(_config_path(tmp_path)), token_path=lambda: token_path)


def _auth():
    return {"X-Hermes-Sidecar-Token": _TOKEN}


def test_health_is_open_and_mutating_methods_are_refused(tmp_path):
    app = _app(tmp_path)
    assert app.dispatch("GET", "/health", {}) == (
        200,
        {"ok": True, "service": "capability-router", "version": 1},
    )
    assert app.dispatch("POST", "/health", {})[0] == 405


def test_token_gate_distinguishes_wrong_and_unprovisioned(tmp_path):
    app = _app(tmp_path)
    assert app.dispatch("GET", "/status", {})[0] == 401
    assert app.dispatch("GET", "/status", {"X-Hermes-Sidecar-Token": "wrong"})[0] == 401
    assert _app(tmp_path / "missing", token=None).dispatch("GET", "/status", _auth())[0] == 503


def test_token_header_is_case_insensitive(tmp_path):
    status, body = _app(tmp_path).dispatch(
        "GET", "/status", {"x-hermes-sidecar-token": _TOKEN}
    )
    assert status == 200
    assert body["enabled"] is True


def test_read_only_routes_and_deterministic_explain(tmp_path):
    app = _app(tmp_path)
    assert app.dispatch("GET", "/policy", _auth())[1]["rules"][0]["id"] == "hard-verbs"
    assert app.dispatch("GET", "/blocklist", _auth())[1]["breaker_enabled"] is False
    status, body = app.dispatch(
        "GET", "/explain", _auth(), {"task": ["Debug a race condition"]}
    )
    assert status == 200
    assert body["mode"] == "deterministic_dry_run"
    assert body["decision"]["output"]["model"] == "strong"


def test_explain_requires_task_and_unknown_route_is_404(tmp_path):
    app = _app(tmp_path)
    assert app.dispatch("GET", "/explain", _auth())[0] == 400
    assert app.dispatch("GET", "/nope", _auth())[0] == 404


def test_lint_route(tmp_path):
    status, body = _app(tmp_path).dispatch("GET", "/lint", _auth())
    assert status == 200
    assert body == {"valid": True, "errors": []}


def test_token_resolver_prefers_explicit_env(monkeypatch, tmp_path):
    token = tmp_path / "explicit.token"
    token.write_text("abc", encoding="utf-8")
    monkeypatch.setenv("HERMES_EXT_SIDECAR_TOKEN_FILE", str(token))
    assert resolve_token_path() == token
    assert read_expected_token().token == "abc"


def test_token_resolver_uses_state_dir_then_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_EXT_SIDECAR_TOKEN_FILE", raising=False)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state_dir))
    state_token = state_dir / "sidecar-auth" / "capability-router.token"
    state_token.parent.mkdir(parents=True)
    state_token.write_text("state", encoding="utf-8")
    assert read_expected_token().token == "state"

    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    home_token = home / "webui" / "sidecar-auth" / "capability-router.token"
    home_token.parent.mkdir(parents=True)
    home_token.write_text("home", encoding="utf-8")
    assert read_expected_token().token == "home"


def test_missing_default_token_is_reported_unprovisioned(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_EXT_SIDECAR_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
    assert read_expected_token().present is False
