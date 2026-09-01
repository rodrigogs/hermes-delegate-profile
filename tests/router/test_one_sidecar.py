"""Pure dispatcher and token-resolution tests for the Hermes One sidecar.

`test_one_sidecar_e2e.py` owns real loopback HTTP coverage. This file keeps the
fast no-socket cases: token gate outcomes, route dispatch and token precedence.
"""
from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timezone
from typing import Optional

import yaml

import router.one_sidecar as sidecar_mod
from router.one_sidecar import (
    SidecarApp,
    _accepts_gzip,
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


def _written_token(tmp_path, token: Optional[str] = _TOKEN):
    """The token file the sidecar reads, written unless ``token`` is None."""
    token_path = tmp_path / "token"
    if token is not None:
        token_path.write_text(token, encoding="utf-8")
    return token_path


def _app(tmp_path, token: Optional[str] = _TOKEN):
    token_path = _written_token(tmp_path, token)
    return SidecarApp(RouterService(_config_path(tmp_path)), token_path=lambda: token_path)


def _auth():
    return {"X-Hermes-Sidecar-Token": _TOKEN}


def test_accept_encoding_only_allows_gzip_when_it_is_permitted():
    assert _accepts_gzip(None) is False
    assert _accepts_gzip("br, identity") is False
    assert _accepts_gzip("GZip") is True
    assert _accepts_gzip("gzip; level=1") is True
    assert _accepts_gzip("gzip; q=0") is False
    assert _accepts_gzip("gzip; q=invalid") is False
    assert _accepts_gzip("br; q=1, gzip; q=0.5") is True


def test_health_is_open_and_mutating_methods_are_refused(tmp_path):
    app = _app(tmp_path)
    assert app.dispatch("GET", "/health", {}) == (
        200,
        {
            "ok": True,
            "service": "hermes-smart-router",
            "version": 1,
            # The one fact /health used to be structurally blind to (2026-08-26:
            # three hours of "ok" while every token-gated route answered 503).
            # Same authority as _authorize — _expected_token — never a second
            # read of the file.
            "token": "present",
        },
    )
    assert app.dispatch("POST", "/health", {})[0] == 405
    # A GET-only data route hit with POST is 405 (wrong method), even with auth.
    assert app.dispatch("POST", "/status", _auth())[0] == 405
    # A write route hit with GET is likewise 405.
    assert app.dispatch("GET", "/plan", _auth())[0] == 405


def test_health_reports_the_token_state_it_share_with_authorize(tmp_path, monkeypatch):
    """/health names the missing token, and stays 200 while doing it.

    The 200 is deliberate and load-bearing: this process also serves /console,
    and the screen must load to be able to EXPLAIN the failure — a 503 here
    would take the explanation down with the problem. Two failure shapes count
    as missing, mirroring _authorize exactly: no file at all, and a file whose
    stripped content is empty (a whitespace-only token authenticates nothing).
    """
    # Second-reader trap, same as the e2e fixture: an env pointer that disagrees
    # with the injected token_path. Any parallel resolution of the token (the
    # module-level read_expected_token, the env precedence ladder) says
    # "missing" here while the authority says "present" — deterministically,
    # on every machine, not only on boxes without an env-resolvable token.
    monkeypatch.setenv(
        "HERMES_EXT_SIDECAR_TOKEN_FILE", str(tmp_path / "no-such-env.token")
    )
    # The authority is present, and /health must agree with it...
    app = _app(tmp_path)
    status, body = app.dispatch("GET", "/health", {})
    assert status == 200
    assert body["token"] == "present"
    # ...and so must the gate it shares the authority with.
    assert app.dispatch("GET", "/status", _auth())[0] == 200

    # No token file at all: the pairing that was invisible for three hours —
    # /health names the missing token WHILE a gated route answers the 503 the
    # screen actually receives.
    bare = _app(tmp_path / "missing", token=None)
    status, body = bare.dispatch("GET", "/health", {})
    assert status == 200
    assert body["token"] == "missing"
    assert bare.dispatch("GET", "/status", _auth()) == (
        503,
        {"error": "sidecar token not provisioned"},
    )
    # A file that exists but strips to empty authenticates nothing — /health
    # must not call it present.
    empty = tmp_path / "empty-token"
    empty.write_text("   \n", encoding="utf-8")
    app_empty = SidecarApp(
        RouterService(_config_path(tmp_path)), token_path=lambda: empty
    )
    assert app_empty.dispatch("GET", "/health", {})[1]["token"] == "missing"


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
    assert body == {"valid": True, "errors": [], "error_targets": []}


def test_lint_and_status_carry_the_shadowed_jump_target(tmp_path):
    """A shadowed policy: /lint and /status both name the dead row's coordinates.

    The console builds "Ver regra N" from ``error_targets``; if the two read
    routes disagreed about the pairing, the button could point at a rule the
    write gate never reported.
    """
    config = _config_path(tmp_path)
    config.write_text(
        yaml.safe_dump(
            {
                "enabled": True,
                "rules": [
                    {
                        "id": "broad",
                        "when": {"has_code": {"eq": True}},
                        "then": {"model": "T2"},
                    },
                    {
                        "id": "narrow",
                        "when": {"has_code": {"eq": True}},
                        "then": {"model": "T1"},
                    },
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
    app = SidecarApp(RouterService(config), token_path=lambda: tmp_path / "token")
    (tmp_path / "token").write_text(_TOKEN, encoding="utf-8")

    status, body = app.dispatch("GET", "/lint", _auth())
    assert status == 200
    assert body["valid"] is False
    assert body["errors"] == ["rule 'narrow' is shadowed by earlier rule 'broad'"]
    assert body["error_targets"] == [{
        "code": "shadowed",
        "later_index": 1,
        "later_id": "narrow",
        "earlier_index": 0,
        "earlier_id": "broad",
        "message": "rule 'narrow' is shadowed by earlier rule 'broad'",
    }]

    status, body = app.dispatch("GET", "/status", _auth())
    assert status == 200
    assert body["validation_errors"] == [
        "rule 'narrow' is shadowed by earlier rule 'broad'"
    ]
    assert body["error_targets"] == [{
        "code": "shadowed",
        "later_index": 1,
        "later_id": "narrow",
        "earlier_index": 0,
        "earlier_id": "broad",
        "message": "rule 'narrow' is shadowed by earlier rule 'broad'",
    }]


def test_liveness_route_is_authenticated_and_returns_composed_states(tmp_path):
    app = _app(tmp_path)

    assert app.dispatch("GET", "/liveness", {})[0] == 401
    status, body = app.dispatch("GET", "/liveness", _auth())

    assert status == 200
    assert body["worst"] == "alive"
    assert {entry["state"] for entry in body["models"]} == {"alive"}


def test_compaction_route_reports_thresholds_and_summarizer_budget(tmp_path):
    app = SidecarApp(
        RouterService(_registry_config_path(tmp_path)),
        token_path=lambda: _written_token(tmp_path),
    )
    status, body = app.dispatch("GET", "/compaction", _auth(), {"aggr": ["50"]})

    assert status == 200
    assert body["aggressiveness"] == 50
    assert body["model_thresholds"]
    assert body["summarizer_window"] > 0
    assert body["threshold_fraction"] > 0
    assert body["threshold_tokens"] == int(body["summarizer_window"] * body["threshold_fraction"])
    assert body["threshold_tokens"] < body["summarizer_window"]
    assert isinstance(body["warning"], bool)


def test_the_served_compaction_windows_come_from_the_registry(tmp_path):
    """The window mirror, asserted equal to its one authority.

    This module used to declare its own MODEL_WINDOWS dict, and three of its four
    entries disagreed with `capabilities.MODEL_CAPABILITIES` — including the shipped
    `compaction.model`, whose window was 272,000 against the registry's 131,072, so
    the summarizer cap the RESTART-class apply wrote into Hermes' config.yaml was
    computed from 2.07x the real window. The repo's rule is that an unavoidable
    mirror is asserted equal by a test; the honest fix was to delete the mirror, and
    this is the test that keeps it deleted.

    Asserted as agreement between two producers rather than against literals, so a
    registry revision cannot make it pass for the wrong reason.
    """
    from router.capabilities import MODEL_CAPABILITIES

    service = RouterService(_registry_config_path(tmp_path))
    summarizer_window, model_windows = service.compaction_windows()

    # Every served window IS the registry's, for every model the policy can route.
    assert model_windows == {
        model: MODEL_CAPABILITIES[model]["context_window"]
        for model in sorted(set(_REAL_TIER_MODELS.values()) | {_REAL_COMPACTION_MODEL})
    }
    # And the summarizer's budget is the window of the model that COMPACTS, not of
    # whatever the largest rail happens to be.
    assert summarizer_window == (
        MODEL_CAPABILITIES[_REAL_COMPACTION_MODEL]["context_window"]
    )
    assert summarizer_window != max(model_windows.values()), (
        "this assertion is only meaningful while the compaction model is not also "
        "the widest rail — otherwise it cannot tell the two rules apart"
    )


def test_a_policy_of_unknown_elos_serves_no_per_model_thresholds(tmp_path):
    """The documented degrade: no invented windows.

    A window cannot be guessed — `p_base(0)` is a math-domain error and a fabricated
    small window compacts far too early — so a policy naming elos the registry
    cannot describe yields an EMPTY threshold map rather than a wrong one. The route
    still answers 200: it is a read path the console opens alongside a broken config.
    """
    status, body = _app(tmp_path).dispatch(
        "GET", "/compaction", _auth(), {"aggr": ["50"]},
    )

    assert status == 200
    assert body["model_thresholds"] == {}
    # The curve still needs a window to answer with, and says which one it used.
    assert body["summarizer_window"] == 128_000
    assert body["threshold_fraction"] > 0


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
    status, body, content_type = app.render_console()
    assert status == 404
    assert content_type == "application/json"
    encoded, headers = app.encode_response(body, "gzip", is_console=True)
    assert headers == {"Vary": "Accept-Encoding", "Content-Encoding": "gzip"}
    assert gzip.decompress(encoded) == body


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


#: Elos the CAPABILITY REGISTRY can describe, for the compaction tests.
#
# The default `_config_path` policy names tiny/small/medium/strong, and nothing in
# the registry knows those — which is fine for every other route but not for the
# threshold curve: per-model thresholds are now derived from the registry's
# `context_window`, so a policy of unknown ids legitimately produces an EMPTY map.
# That degrade has its own test below; these tests are about the curve, so they need
# a policy whose models exist.
_REAL_TIER_MODELS = {
    "T1": "glm-5.3-flash",
    "T2": "gpt-5.6-luna",
    "T3": "gpt-5.6-terra",
    "T4": "gpt-5.5",
}
_REAL_COMPACTION_MODEL = "glm-4.5-flash"


def _registry_config_path(tmp_path):
    """``_config_path``'s policy with tier models the registry can describe."""
    path = _config_path(tmp_path)
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    for tier, model in _REAL_TIER_MODELS.items():
        config["tiers"][tier]["model"] = model
    config["compaction"] = {
        "provider": "zai", "model": _REAL_COMPACTION_MODEL,
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _compaction_app(tmp_path, runner, core_yaml=None, config_path=None):
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
        RouterService(config_path or _config_path(tmp_path)),
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

    # A policy the registry can describe, or there are no per-model thresholds to
    # recompute — see test_a_policy_of_unknown_elos_serves_no_per_model_thresholds.
    app, _core = _compaction_app(
        tmp_path, runner, config_path=_registry_config_path(tmp_path),
    )
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

    # The candidate the launcher receives carries the thresholds the SCREEN shows.
    # Read and apply used to take different sources — the module constants and the
    # injectable instance attributes — so the operator could confirm a number this
    # write would not produce.
    _status, shown = app.dispatch(
        "GET", "/compaction", _auth(), {"aggr": ["100"]},
    )
    assert reloaded["compression"]["model_thresholds"] == shown["model_thresholds"]


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


def _compaction_policy_app(tmp_path, runner, compaction_block, core_yaml=None):
    """A sidecar whose router.yaml carries a real T1 tier and a compaction block,
    wired with a stubbed restart runner + a fake core config.yaml so the
    RESTART-class path never actually restarts anything in a test."""
    token_path = tmp_path / "token"
    token_path.write_text(_TOKEN, encoding="utf-8")
    core = tmp_path / "config.yaml"
    core.write_text(
        core_yaml if core_yaml is not None else yaml.safe_dump(
            {"compression": {"enabled": True, "aggressiveness": 50}}, sort_keys=False
        ),
        encoding="utf-8",
    )
    config = {
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
            "T1": {
                "model": "glm-4.7",
                "provider": "zai",
                "fallback": [
                    {"model": "gpt-5.6-luna", "provider": "openai-codex"},
                    {"model": "mimo-v2.5", "provider": "xiaomi"},
                ],
            },
            "T2": {"model": "small", "provider": "cheap"},
            "T3": {"model": "medium", "provider": "strong"},
            "T4": {"model": "strong", "provider": "strong"},
        },
    }
    if compaction_block is not None:
        config["compaction"] = compaction_block
    policy_path = tmp_path / "router.yaml"
    policy_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return SidecarApp(
        RouterService(policy_path),
        token_path=lambda: token_path,
        core_config_path=lambda: core,
        restart_runner=runner,
    ), core


def test_compaction_apply_writes_auxiliary_compression_from_the_policy(tmp_path):
    captured = {}

    def runner(candidate_path):
        captured["yaml"] = candidate_path.read_text(encoding="utf-8")
        return {"ok": True, "restart": "scheduled"}

    app, _core = _compaction_policy_app(
        tmp_path,
        runner,
        {"provider": "zai", "model": "glm-4.5-flash", "fallback_mode": "tier:T1"},
    )
    status, _body = app.dispatch(
        "POST", "/apply", _auth(),
        body={"action": "compaction", "confirm": "COMPACT", "aggressiveness": 100},
    )
    assert status == 202
    reloaded = yaml.safe_load(captured["yaml"])
    compression = reloaded["auxiliary"]["compression"]
    assert compression["provider"] == "zai"
    assert compression["model"] == "glm-4.5-flash"
    assert compression["fallback_chain"] == [
        {"provider": "zai", "model": "glm-4.7"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
        {"provider": "xiaomi", "model": "mimo-v2.5"},
    ]
    # The dynamic-threshold pass still runs alongside the choice.
    assert reloaded["compression"]["aggressiveness"] == 100


def test_compaction_apply_preserves_sibling_auxiliary_and_api_key(tmp_path):
    captured = {}

    def runner(candidate_path):
        captured["yaml"] = candidate_path.read_text(encoding="utf-8")
        return {"ok": True}

    app, _core = _compaction_policy_app(
        tmp_path,
        runner,
        {"provider": "zai", "model": "glm-4.5-flash", "fallback_mode": "tier:T1"},
        core_yaml=yaml.safe_dump(
            {
                "model": {"default": "gpt-5.6-terra"},
                "auxiliary": {
                    "vision": {"provider": "openai", "model": "gpt-5.6-luna"},
                    "compression": {"api_key": "s3cr3t", "timeout": 90},
                },
            },
            sort_keys=False,
        ),
    )
    status, _body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 202
    reloaded = yaml.safe_load(captured["yaml"])
    aux = reloaded["auxiliary"]
    assert aux["vision"] == {"provider": "openai", "model": "gpt-5.6-luna"}
    assert aux["compression"]["api_key"] == "s3cr3t"
    assert aux["compression"]["timeout"] == 90
    assert aux["compression"]["model"] == "glm-4.5-flash"
    assert reloaded["model"] == {"default": "gpt-5.6-terra"}


def test_compaction_apply_refuses_scalar_auxiliary_in_core_config(tmp_path):
    calls = []
    app, _core = _compaction_policy_app(
        tmp_path,
        lambda p: calls.append(p) or {"ok": True},
        {"provider": "zai", "model": "glm-4.5-flash", "fallback_mode": "tier:T1"},
        core_yaml="auxiliary: not-a-mapping\n",
    )
    status, body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 400
    assert "auxiliary in core config must be a mapping" in body["error"]
    assert calls == []


def test_compaction_apply_refuses_scalar_compression_in_core_config(tmp_path):
    calls = []
    app, _core = _compaction_policy_app(
        tmp_path,
        lambda p: calls.append(p) or {"ok": True},
        {"provider": "zai", "model": "glm-4.5-flash", "fallback_mode": "tier:T1"},
        core_yaml="auxiliary:\n  compression: not-a-mapping\n",
    )
    status, body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 400
    assert "auxiliary.compression in core config must be a mapping" in body["error"]
    assert calls == []


def test_compaction_apply_refuses_an_unknown_model(tmp_path):
    calls = []
    app, _core = _compaction_policy_app(
        tmp_path,
        lambda p: calls.append(p) or {"ok": True},
        {"provider": "zai", "model": "no-such-model", "fallback_mode": "tier:T1"},
    )
    status, body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 400
    assert "no-such-model" in body["error"]
    assert calls == []  # the restart runner was never invoked


def test_compaction_apply_refuses_a_missing_tier_by_name(tmp_path):
    calls = []
    app, _core = _compaction_policy_app(
        tmp_path,
        lambda p: calls.append(p) or {"ok": True},
        {"provider": "zai", "model": "glm-4.5-flash", "fallback_mode": "tier:T9"},
    )
    status, body = app.dispatch(
        "POST", "/apply", _auth(), body={"action": "compaction", "confirm": "COMPACT"}
    )
    assert status == 400
    assert "T9" in body["error"]
    assert calls == []


def test_compaction_get_route_reports_the_resolved_choice(tmp_path):
    app, _core = _compaction_policy_app(
        tmp_path,
        lambda p: {"ok": True},
        {"provider": "zai", "model": "glm-4.5-flash", "fallback_mode": "tier:T1"},
    )
    status, body = app.dispatch("GET", "/compaction", _auth(), {"aggr": ["50"]})
    assert status == 200
    assert body["compaction"]["model"] == "glm-4.5-flash"
    assert body["compaction"]["fallback_chain"][0] == {
        "provider": "zai",
        "model": "glm-4.7",
    }
    assert body["compaction_errors"] == []


def test_compaction_get_route_reports_the_refusal_instead_of_a_400(tmp_path):
    app, _core = _compaction_policy_app(
        tmp_path,
        lambda p: {"ok": True},
        {"provider": "zai", "model": "no-such-model", "fallback_mode": "tier:T1"},
    )
    status, body = app.dispatch("GET", "/compaction", _auth(), {"aggr": ["50"]})
    assert status == 200
    assert body["compaction"] is None
    assert any("no-such-model" in e for e in body["compaction_errors"])


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


def test_routes_endpoint_lists_and_fetches_traces(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from router.durable_decision_log import routes_path
    rp = routes_path()
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(
        json.dumps({"ts": 1.0, "cause": "hard_rule", "task": "a", "output": {"model": "m1"},
                    "steps": [{"stage": "blocklist"}]}) + "\n",
        encoding="utf-8",
    )
    app = _app(tmp_path)
    # List requires a token.
    assert app.dispatch("GET", "/routes", {})[0] == 401
    status, body = app.dispatch("GET", "/routes", _auth())
    assert status == 200
    assert body["count"] == 1
    assert body["trace_path"].endswith("routes.jsonl")
    rid = body["routes"][0]["id"]
    # Fetch one full trace by id.
    status, full = app.dispatch("GET", "/routes", _auth(), {"id": [rid]})
    assert status == 200
    assert full["steps"][0]["stage"] == "blocklist"
    # Unknown id → 404.
    assert app.dispatch("GET", "/routes", _auth(), {"id": ["nope"]})[0] == 404
    # POST /routes → 405 (method guard intact for the new route).
    assert app.dispatch("POST", "/routes", _auth(), body={})[0] == 405


def test_routes_endpoint_bad_limit_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    app = _app(tmp_path)
    # Non-numeric limit must not crash; empty state → count 0.
    status, body = app.dispatch("GET", "/routes", _auth(), {"limit": ["oops"]})
    assert status == 200
    assert body["count"] == 0


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
    # With no HERMES_PROFILE set, resolve the ROOT profile (~/.hermes/config.yaml).
    # Falling back to a literal profile name pointed at a directory that does not
    # exist on a root-profile-only install.
    assert resolve_core_config_path() == tmp_path / ".hermes" / "config.yaml"
    # An explicit profile still resolves under profiles/<name>.
    monkeypatch.setenv("HERMES_PROFILE", "bob")
    assert resolve_core_config_path() == tmp_path / ".hermes" / "profiles" / "bob" / "config.yaml"


def test_resolve_core_config_path_already_profile_scoped(monkeypatch, tmp_path):
    """A HERMES_HOME that already ends in profiles/<name> resolves in place."""
    scoped = tmp_path / "h" / "profiles" / "alice"
    scoped.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(scoped))
    assert resolve_core_config_path() == scoped / "config.yaml"


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
    state_token = state_dir / "sidecar-auth" / "hermes-smart-router.token"
    state_token.parent.mkdir(parents=True)
    state_token.write_text("state", encoding="utf-8")
    assert read_expected_token().token == "state"

    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    home_token = home / "webui" / "sidecar-auth" / "hermes-smart-router.token"
    home_token.parent.mkdir(parents=True)
    home_token.write_text("home", encoding="utf-8")
    assert read_expected_token().token == "home"


def test_missing_default_token_is_reported_unprovisioned(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_EXT_SIDECAR_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
    assert read_expected_token().present is False


# ---------------------------------------------------------------------------
# GET /capabilities — the route the console's price audit reads
# ---------------------------------------------------------------------------

# Every path the console fetches, as it writes them: call('/x'), call(`/x?y`).
# call(path, ...) — the one dynamic form — carries no literal and is skipped.
_CONSOLE_CALL = re.compile(r"""call\(\s*['"`](/[^'"`?\s]*)""")


def _real_number(value):
    """A price only counts as published when it is a real number; bool is not one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def test_capabilities_route_serves_the_catalogue_the_console_asks_for(tmp_path):
    """The console calls GET /capabilities on every load, so it has to exist.

    While it did not, that call 404'd — and the price panel did not render blank,
    it rendered FALSE: every elo capability-unverified and every rail publishing
    no per-token price, including the metered ones that publish one.
    """
    app = _app(tmp_path)

    # Token-gated like every other data route (only /health is exempt).
    assert app.dispatch("GET", "/capabilities", {})[0] == 401
    status, body = app.dispatch("GET", "/capabilities", _auth())
    assert status == 200
    assert body["registry_available"] is True
    assert body["models"]
    # Read-only: a write method on a KNOWN route is a 405, never a 404.
    assert app.dispatch("POST", "/capabilities", _auth())[0] == 405
    # A model catalogue carries no credential, now or after the registry grows.
    serialized = json.dumps(body).lower()
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_capabilities_route_serves_exactly_what_the_service_reads(tmp_path):
    """The HTTP surface adds nothing and drops nothing on the way out."""
    app = _app(tmp_path)
    service = RouterService(_config_path(tmp_path))

    status, body = app.dispatch("GET", "/capabilities", _auth())

    assert status == 200
    assert body == service.capabilities()
    # It survives the JSON round trip the console actually receives it through:
    # an unpublished price has to arrive as null, never as 0.
    assert json.loads(json.dumps(body)) == body
    unpriced = [
        model for model, entry in body["models"].items()
        if not entry["price_published"]
    ]
    assert unpriced
    for model in unpriced:
        entry = json.loads(json.dumps(body["models"][model]))
        assert not (
            _real_number(entry.get("price_in"))
            and _real_number(entry.get("price_out"))
        ), model
    # The fixture's elos are unknown to the registry: flagged, never fabricated.
    assert "tiny" in body["unknown_models"]
    assert "tiny" not in body["models"]


def test_every_route_the_console_calls_is_a_route_that_answers(tmp_path):
    """The console and the dispatcher must agree on the route list.

    This is the check that was missing. GET /capabilities was wired into the
    console's load and into no route table, so it 404'd on every load, and a
    404'd audit panel renders a false answer rather than a blank one. Asserting
    the AGREEMENT is the only form of this test that holds — asserting either
    side alone is precisely what let the two drift apart.
    """
    console = sidecar_mod._CONSOLE_PATH.read_text(encoding="utf-8")
    called = {match.group(1) for match in _CONSOLE_CALL.finditer(console)}
    known = sidecar_mod._GET_ROUTES | sidecar_mod._POST_ROUTES

    assert "/capabilities" in called, "the console still asks for the catalogue"
    assert called
    assert not called - known

    # Named is not the same as answering: every GET the console makes must reach
    # a handler rather than fall through to the unknown-route arm.
    app = _app(tmp_path)
    for path in sorted(called & sidecar_mod._GET_ROUTES):
        _status, body = app.dispatch("GET", path, _auth())
        assert body.get("error") != "unknown route", path


def test_explain_accepts_prompt_text_on_both_get_and_post(tmp_path):
    """Both /explain surfaces take the parameter, and take it the same way.

    GET is the link-shaped probe; POST is the wider pipe for a context too large
    for an HTTP request line. Same name, same meaning, same answer — asserted as
    an agreement between the two, since a preview that sized the goal line while
    production sized the composed turn is the failure the parameter exists for.
    """
    app = _app(tmp_path)
    task = "Debug a race condition"
    prompt = ("context line\n" * 4000) + task
    at = "2026-08-17T07:00:00Z"

    get_status, get_body = app.dispatch(
        "GET", "/explain", _auth(),
        {"task": [task], "at": [at], "prompt_text": [prompt]},
    )
    post_status, post_body = app.dispatch(
        "POST", "/explain", _auth(),
        body={"task": task, "at": at, "prompt_text": prompt},
    )

    assert (get_status, post_status) == (200, 200)
    # The clock is pinned, so the two responses are comparable byte for byte.
    assert get_body == post_body

    # Not merely accepted — MEASURED. The preview names the text it sized from,
    # and the goal line alone measures a fraction of it.
    assert get_body["preview"]["sized_from"] == "prompt_text"
    assert get_body["preview"]["prompt_chars"] == len(prompt)
    goal_only = app.dispatch("GET", "/explain", _auth(),
                             {"task": [task], "at": [at]})[1]
    assert goal_only["preview"]["sized_from"] == "task"
    assert (
        get_body["features"]["est_input_tokens"]
        > goal_only["features"]["est_input_tokens"]
    )

    # Fail-CLOSED on an unusable value, identically on both surfaces: a JSON
    # number is not a prompt, and coercing one would size a turn from "0".
    assert app.dispatch("POST", "/explain", _auth(),
                        body={"task": task, "prompt_text": 0})[0] == 400


# ── provenance (three ages on /status) ────────────────────────────────────
# The sidecar stamps boot provenance so /status can report which source is
# stale. `_code_mtime` must be captured once at boot, not per request — the
# whole point is that a process that booted before the last edit keeps saying
# what it loaded.

class _FakeStat:
    def __init__(self, mtime: float):
        self.st_mtime = mtime


class _FakeFile:
    def __init__(self, payload):
        self._payload = payload  # float mtime, or OSError to raise on stat

    def stat(self):
        if isinstance(self._payload, OSError):
            raise self._payload
        return _FakeStat(self._payload)


class _FakeRouterDir:
    def __init__(self, files, dir_mtime: float = 0.0, dir_error=None):
        self._files = files
        self._dir_mtime = dir_mtime
        self._dir_error = dir_error

    def glob(self, pattern: str):
        assert pattern == "*.py", pattern
        return list(self._files)

    def stat(self):
        if self._dir_error is not None:
            raise self._dir_error
        return _FakeStat(self._dir_mtime)


class _FakePath:
    """Path stand-in: ``Path(__file__).resolve().parent`` yields the fake dir."""

    def __init__(self, router_dir):
        self._router_dir = router_dir

    def resolve(self):
        return self

    @property
    def parent(self):
        return self._router_dir


def _patch_router_dir(monkeypatch, router_dir):
    monkeypatch.setattr(sidecar_mod, "Path", lambda *_: _FakePath(router_dir))


def test_code_mtime_is_the_newest_module_mtime(monkeypatch):
    files = [_FakeFile(1000.0), _FakeFile(500.0), _FakeFile(2000.0)]
    _patch_router_dir(monkeypatch, _FakeRouterDir(files))

    out = sidecar_mod._code_mtime()
    assert out == datetime.fromtimestamp(2000.0, tz=timezone.utc).isoformat()


def test_code_mtime_skips_unreadable_modules(monkeypatch):
    files = [_FakeFile(OSError("gone")), _FakeFile(1500.0)]
    _patch_router_dir(monkeypatch, _FakeRouterDir(files))

    out = sidecar_mod._code_mtime()
    assert out == datetime.fromtimestamp(1500.0, tz=timezone.utc).isoformat()


def test_code_mtime_falls_back_to_dir_mtime_when_no_modules(monkeypatch):
    _patch_router_dir(monkeypatch, _FakeRouterDir([], dir_mtime=3000.0))

    out = sidecar_mod._code_mtime()
    assert out == datetime.fromtimestamp(3000.0, tz=timezone.utc).isoformat()


def test_code_mtime_falls_back_to_boot_when_dir_unreadable(monkeypatch):
    _patch_router_dir(monkeypatch, _FakeRouterDir([], dir_error=OSError("no dir")))

    out = sidecar_mod._code_mtime()
    assert out == sidecar_mod._PROCESS_STARTED_AT


def test_sidecar_app_stamps_provenance_onto_service(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text(_TOKEN, encoding="utf-8")
    service = RouterService(_config_path(tmp_path))

    SidecarApp(
        service,
        token_path=lambda: token_path,
        process_started_at="2026-08-18T23:40:44+00:00",
        code_mtime="2026-08-18T19:39:42+00:00",
    )

    assert service._process_started_at == "2026-08-18T23:40:44+00:00"
    assert service._code_mtime == "2026-08-18T19:39:42+00:00"


def test_sidecar_app_without_provenance_leaves_service_blank(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text(_TOKEN, encoding="utf-8")
    service = RouterService(_config_path(tmp_path))

    SidecarApp(
        service,
        token_path=lambda: token_path,
        process_started_at=None,
        code_mtime=None,
    )

    assert service._process_started_at is None
    assert service._code_mtime is None
