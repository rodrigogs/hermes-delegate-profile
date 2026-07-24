"""Pure dispatcher and token-resolution tests for the Hermes One sidecar.

`test_one_sidecar_e2e.py` owns real loopback HTTP coverage. This file keeps the
fast no-socket cases: token gate outcomes, route dispatch and token precedence.
"""
from __future__ import annotations

from typing import Optional

import yaml

import router.one_sidecar as sidecar_mod
from router.one_sidecar import (
    SidecarApp,
    _default_restart_runner,
    parse_json_body,
    read_expected_token,
    resolve_core_config_path,
    resolve_token_path,
)
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
    # A GET-only data route hit with POST is 405 (wrong method), even with auth.
    assert app.dispatch("POST", "/status", _auth())[0] == 405
    # A write route hit with GET is likewise 405.
    assert app.dispatch("GET", "/plan", _auth())[0] == 405


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


def test_liveness_route_is_authenticated_and_returns_composed_states(tmp_path):
    app = _app(tmp_path)

    assert app.dispatch("GET", "/liveness", {})[0] == 401
    status, body = app.dispatch("GET", "/liveness", _auth())

    assert status == 200
    assert body["worst"] == "alive"
    assert {entry["state"] for entry in body["models"]} == {"alive"}


def test_compaction_route_reports_thresholds_and_summarizer_budget(tmp_path):
    status, body = _app(tmp_path).dispatch("GET", "/compaction", _auth(), {"aggr": ["50"]})

    assert status == 200
    assert body["aggressiveness"] == 50
    assert body["model_thresholds"]
    assert body["summarizer_window"] > 0
    assert body["threshold_fraction"] > 0
    assert body["threshold_tokens"] == int(body["summarizer_window"] * body["threshold_fraction"])
    assert body["threshold_tokens"] < body["summarizer_window"]
    assert isinstance(body["warning"], bool)


def test_console_is_served_tokenless_as_html(tmp_path):
    console = tmp_path / "console.html"
    console.write_text("<!DOCTYPE html><title>ok</title>", encoding="utf-8")
    app = SidecarApp(
        RouterService(_config_path(tmp_path)),
        token_path=lambda: tmp_path / "token",
        console_path=console,
    )
    status, body, content_type = app.render_console()
    assert status == 200
    assert body.startswith(b"<!DOCTYPE")
    assert content_type == "text/html; charset=utf-8"


def test_console_missing_file_degrades_to_404_json(tmp_path):
    app = SidecarApp(
        RouterService(_config_path(tmp_path)),
        token_path=lambda: tmp_path / "token",
        console_path=tmp_path / "absent.html",
    )
    status, _body, content_type = app.render_console()
    assert status == 404
    assert content_type == "application/json"


def test_write_routes_require_token(tmp_path):
    app = _app(tmp_path)
    assert app.dispatch("POST", "/plan", {}, body={"policy": {}})[0] == 401
    assert app.dispatch("POST", "/apply", {}, body={})[0] == 401


def test_plan_route_returns_base_hash(tmp_path):
    app = _app(tmp_path)
    status, body = app.dispatch(
        "POST", "/plan", _auth(), body={"policy": {"default": {"action": "T1"}}}
    )
    assert status == 200
    assert body["valid"] is True
    assert body["base_hash"]


def test_plan_route_requires_policy_object(tmp_path):
    app = _app(tmp_path)
    assert app.dispatch("POST", "/plan", _auth(), body={})[0] == 400
    # A JSON body that is not an object at all is a 400 too.
    assert app.dispatch("POST", "/plan", _auth(), body=None)[0] == 400


def test_apply_commits_then_confirm_and_revert(tmp_path):
    app = _app(tmp_path)
    plan = app.dispatch(
        "POST", "/plan", _auth(), body={"policy": {"default": {"action": "T2"}}}
    )[1]
    status, body = app.dispatch(
        "POST", "/apply", _auth(), body={"plan": plan, "policy": plan["policy"]}
    )
    assert status == 200
    assert body["ok"] is True
    # confirm re-commits against the (now advanced) on-disk hash: the plan's
    # base_hash is stale, so it is a clean 409 rather than a dead 404.
    confirm = app.dispatch(
        "POST", "/apply/confirm", _auth(), body={"plan": plan, "policy": plan["policy"]}
    )
    assert confirm[0] == 409
    assert app.dispatch("POST", "/apply/revert", _auth(), body={})[0] == 200


def test_apply_stale_hash_is_409(tmp_path):
    app = _app(tmp_path)
    stale = {"base_hash": "deadbeef" * 8, "policy": {"default": {"action": "T1"}}}
    status, body = app.dispatch(
        "POST", "/apply", _auth(), body={"plan": stale, "policy": stale["policy"]}
    )
    assert status == 409
    assert body["conflict"] is True


def test_apply_missing_plan_is_400(tmp_path):
    app = _app(tmp_path)
    assert app.dispatch("POST", "/apply", _auth(), body={"policy": {}})[0] == 400


def _compaction_app(tmp_path, runner, core_yaml=None):
    """A sidecar wired with a stubbed restart runner + a fake core config.yaml
    so the compaction path never actually restarts anything in a test."""
    token_path = tmp_path / "token"
    token_path.write_text(_TOKEN, encoding="utf-8")
    core = tmp_path / "config.yaml"
    core.write_text(
        core_yaml if core_yaml is not None else yaml.safe_dump(
            {"compression": {"enabled": True, "aggressiveness": 50}}, sort_keys=False
        ),
        encoding="utf-8",
    )
    return SidecarApp(
        RouterService(_config_path(tmp_path)),
        token_path=lambda: token_path,
        core_config_path=lambda: core,
        restart_runner=runner,
    ), core


def test_compaction_requires_exact_confirm(tmp_path):
    calls = []
    app, _core = _compaction_app(tmp_path, lambda p: calls.append(p) or {"ok": True})
    # Missing / wrong confirm -> 400, and the restart runner is NEVER invoked.
    assert app.dispatch("POST", "/apply", _auth(), body={"action": "compaction"})[0] == 400
    assert app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "compact"}
    )[0] == 400
    assert calls == []


def test_compaction_rejects_out_of_range_aggressiveness(tmp_path):
    app, _core = _compaction_app(tmp_path, lambda p: {"ok": True})
    status, _body = app.dispatch(
        "POST", "/apply", _auth(),
        body={"action": "compaction", "confirm": "COMPACT", "aggressiveness": 500},
    )
    assert status == 400


def test_compaction_schedules_restart_with_recomputed_candidate(tmp_path):
    captured = {}

    def runner(candidate_path):
        # The launcher receives a fully-formed candidate config with recomputed
        # thresholds; capture it to assert the dynamic-threshold pass ran.
        captured["yaml"] = candidate_path.read_text(encoding="utf-8")
        return {"ok": True, "restart": "scheduled"}

    app, _core = _compaction_app(tmp_path, runner)
    status, body = app.dispatch(
        "POST", "/apply", _auth(),
        body={"action": "compaction", "confirm": "COMPACT", "aggressiveness": 100},
    )
    assert status == 202
    assert body["restart"] == "scheduled"
    assert body["aggressiveness"] == 100
    reloaded = yaml.safe_load(captured["yaml"])
    assert reloaded["compression"]["aggressiveness"] == 100
    assert reloaded["compression"]["model_thresholds"]      # recomputed
    assert reloaded["compression"]["threshold_tokens"]      # summarizer cap


def test_compaction_reports_unreadable_core_config(tmp_path):
    app, core = _compaction_app(tmp_path, lambda p: {"ok": True})
    core.unlink()  # remove the core config after wiring
    status, _body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 400


def test_compaction_scalar_core_config_is_rejected(tmp_path):
    app, _core = _compaction_app(tmp_path, lambda p: {"ok": True}, core_yaml="just-a-scalar")
    status, _body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 400


def test_compaction_surfaces_restart_failure_as_502(tmp_path):
    app, _core = _compaction_app(
        tmp_path, lambda p: {"ok": False, "error": "launcher missing"}
    )
    status, body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 502
    assert body["ok"] is False


def test_post_body_must_be_object(tmp_path):
    app = _app(tmp_path)
    # A non-object JSON body (list) to a write route is a 400.
    assert app.dispatch("POST", "/apply", _auth(), body=["nope"])[0] == 400


def test_apply_requires_base_hash_and_policy_shapes(tmp_path):
    app = _app(tmp_path)
    # plan present but base_hash missing.
    assert app.dispatch("POST", "/apply", _auth(), body={"plan": {}})[0] == 400
    # base_hash present but policy is not an object.
    assert app.dispatch(
        "POST", "/apply", _auth(),
        body={"plan": {"base_hash": "x"}, "policy": "not-a-dict"},
    )[0] == 400


def test_apply_lint_invalid_is_400(tmp_path):
    """A plan that fails lint returns ok:false -> HTTP 400 (not 200/409)."""
    app = _app(tmp_path)
    plan = app.dispatch("POST", "/plan", _auth(), body={"policy": {
        "rules": [{"id": "b", "when": {"verb_class": {"eq": "hard"}}, "then": {"model": "T9"}}]
    }})[1]
    status, _body = app.dispatch(
        "POST", "/apply", _auth(), body={"plan": plan, "policy": plan["policy"]}
    )
    assert status == 400


def test_plan_value_error_maps_to_400(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(
        "router.service.RouterService.plan",
        lambda _self, _changes: (_ for _ in ()).throw(ValueError("boom")),
    )
    status, body = app.dispatch("POST", "/plan", _auth(), body={"policy": {}})
    assert status == 400
    assert "boom" in body["error"]


def test_apply_value_error_maps_to_400(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(
        "router.service.RouterService.apply",
        lambda _self, _bh, _c: (_ for _ in ()).throw(ValueError("bad")),
    )
    status, body = app.dispatch(
        "POST", "/apply", _auth(), body={"plan": {"base_hash": "x"}, "policy": {}}
    )
    assert status == 400
    assert "bad" in body["error"]


def test_compaction_rejects_bad_aggr(tmp_path):
    app = _app(tmp_path)
    # Non-integer aggr.
    assert app.dispatch("GET", "/compaction", _auth(), {"aggr": ["abc"]})[0] == 400
    # Out-of-range aggr.
    assert app.dispatch("GET", "/compaction", _auth(), {"aggr": ["500"]})[0] == 400


def test_dispatch_unknown_method_on_no_route_is_404(tmp_path):
    app = _app(tmp_path)
    # A method that is neither GET nor POST, on a route in no set: the method
    # guard only fires for known routes, so this falls through to 404.
    assert app.dispatch("DELETE", "/whatever", _auth())[0] == 404


def test_parse_json_body_edges():
    # Valid JSON object.
    assert parse_json_body("13", lambda _n: b'{"policy": 1}') == ({"policy": 1}, True)
    # Missing / zero / non-numeric length all yield an empty object, ok.
    assert parse_json_body(None, lambda _n: b"") == ({}, True)
    assert parse_json_body("0", lambda _n: b"") == ({}, True)
    assert parse_json_body("not-a-number", lambda _n: b"") == ({}, True)
    # Positive length but the reader returns nothing -> empty object, ok.
    assert parse_json_body("5", lambda _n: b"") == ({}, True)
    # Malformed JSON -> (None, False).
    assert parse_json_body("9", lambda _n: b"{not json") == (None, False)


def test_resolve_core_config_path_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CORE_CONFIG_FILE", str(tmp_path / "explicit.yaml"))
    assert resolve_core_config_path() == tmp_path / "explicit.yaml"
    monkeypatch.delenv("HERMES_CORE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    assert resolve_core_config_path() == tmp_path / "h" / "profiles" / "alice" / "config.yaml"
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(sidecar_mod.Path, "home", classmethod(lambda cls: tmp_path))
    assert resolve_core_config_path() == tmp_path / ".hermes" / "profiles" / "rodrigo" / "config.yaml"


def test_default_restart_runner_missing_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar_mod, "_SAFE_RESTART", tmp_path / "absent.sh")
    result = _default_restart_runner(tmp_path / "cand.yaml")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_default_restart_runner_success_and_failure(monkeypatch, tmp_path):
    launcher = tmp_path / "hermes-safe-restart.sh"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(sidecar_mod, "_SAFE_RESTART", launcher)

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(sidecar_mod.subprocess, "run", lambda *a, **k: _OK())
    ok = _default_restart_runner(tmp_path / "cand.yaml")
    assert ok == {"ok": True, "restart": "scheduled"}

    class _Bad:
        returncode = 1
        stdout = ""
        stderr = "validation failed"

    monkeypatch.setattr(sidecar_mod.subprocess, "run", lambda *a, **k: _Bad())
    bad = _default_restart_runner(tmp_path / "cand.yaml")
    assert bad["ok"] is False
    assert "validation failed" in bad["detail"]


def test_default_restart_runner_handles_subprocess_error(monkeypatch, tmp_path):
    launcher = tmp_path / "hermes-safe-restart.sh"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sidecar_mod, "_SAFE_RESTART", launcher)
    monkeypatch.setattr(
        sidecar_mod.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no bash")),
    )
    result = _default_restart_runner(tmp_path / "cand.yaml")
    assert result["ok"] is False
    assert "invocation failed" in result["error"]


def test_compaction_staging_failure_is_500(tmp_path, monkeypatch):
    """If writing the candidate temp file fails, the endpoint reports 500 and
    the runner is never reached."""
    calls = []
    app, _core = _compaction_app(tmp_path, lambda p: calls.append(p) or {"ok": True})
    monkeypatch.setattr(
        sidecar_mod.os, "fdopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    # The temp-file cleanup ALSO fails: the endpoint must still return 500, not
    # leak the unlink error (covers the best-effort cleanup swallow).
    monkeypatch.setattr(
        sidecar_mod.os, "unlink",
        lambda *a: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    status, _body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 500
    assert calls == []


def test_unknown_post_route_is_404(tmp_path):
    app = _app(tmp_path)
    # /apply is a known POST route, but a nonexistent POST subpath that still
    # passes the (frozenset) method guard should not exist. Use a route in
    # neither set: it is treated as unknown -> 404 after auth.
    assert app.dispatch("POST", "/nope", _auth(), body={})[0] == 404


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
