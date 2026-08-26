"""End-to-end tests that boot the real sidecar HTTP server on an ephemeral port.

These cover the server loop, the BaseHTTPRequestHandler dispatch, the CLI parser,
the loopback-host guard, and main()'s serve/shutdown path — the paths a pure
SidecarApp.dispatch() unit test cannot reach.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from router.one_sidecar import (
    EXTENSION_ID,
    TOKEN_HEADER,
    SidecarApp,
    build_parser,
    build_server,
    main,
    resolve_token_path,
)
from router.service import RouterService


ROOT = Path(__file__).resolve().parent.parent.parent


def _get(url: str, token: str | None = None, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    if token is not None:
        req.add_header(TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, (json.loads(body) if body else None)


@pytest.fixture()
def running_sidecar(tmp_path, monkeypatch):
    token_file = tmp_path / "hermes-smart-router.token"
    token_file.write_text("s3cret-token", encoding="utf-8")
    # The second-reader trap: if /health ever resolves the token through the
    # ENVIRONMENT instead of the same injected path _authorize reads, this
    # disagreeing pointer makes it say "missing" while the truth is present.
    # Without it, the trap only fires on machines that happen to lack an
    # env-resolvable token — this shell is one, the operator's box is not.
    monkeypatch.setenv(
        "HERMES_EXT_SIDECAR_TOKEN_FILE", str(tmp_path / "no-such-env.token")
    )
    app = SidecarApp(RouterService(ROOT / "router.yaml"), token_path=lambda: token_file)
    server = build_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    base = f"http://{host}:{port}"
    try:
        yield base, token_file
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_health_is_tokenless_and_cors_open(running_sidecar):
    base, _token = running_sidecar
    status, payload = _get(f"{base}/health")
    assert status == 200
    assert payload == {
        "ok": True,
        "service": EXTENSION_ID,
        "version": 1,
        "token": "present",
    }


def test_health_says_missing_while_gated_routes_answer_503(tmp_path):
    """The pairing that was invisible for three hours on 2026-08-26.

    The incident: /health responded 200 ok with no token file, /status and
    every other screen-fed route answered 503, and nothing on the operator's
    side could see the difference. This boots the real HTTP server with NO
    token file and asserts BOTH sides of the incident over the wire: /health
    200 naming the missing token, /status 503 with the exact error the screen
    receives. The 200 on /health is deliberate — this process also serves
    /console, which has to load to explain the failure.
    """
    missing = tmp_path / "absent.token"
    app = SidecarApp(RouterService(ROOT / "router.yaml"), token_path=lambda: missing)
    server = build_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload = _get(f"{base}/health")
        assert status == 200
        assert payload is not None and payload["token"] == "missing"
        assert payload["ok"] is True
        # A token-gated route simultaneously refuses with the 503 the screen
        # receives — even with a token supplied, because none is provisioned.
        status, payload = _get(f"{base}/status", token="anything")
        assert (status, payload) == (503, {"error": "sidecar token not provisioned"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_status_requires_valid_token(running_sidecar):
    base, _token = running_sidecar
    assert _get(f"{base}/status")[0] == 401
    assert _get(f"{base}/status", token="wrong")[0] == 401
    status, payload = _get(f"{base}/status", token="s3cret-token")
    assert status == 200
    assert "enabled" in payload


def test_status_reports_real_boot_provenance(running_sidecar):
    """The production stamp path: the sidecar captures the three ages at boot
    (module import) and /status reports them. What this can assert without
    depending on who wrote the config file first is the SHAPE — all three
    fields present and ISO 8601 UTC parseable. The staleness inequality is
    proven over injected values in test_status_provenance_obeys_injected_stamps;
    asserting it here would re-introduce the import-order fragility this test
    used to have (a seed writing router.yaml after the boot stamp broke it on
    every clean checkout)."""
    base, _token = running_sidecar
    _status, payload = _get(f"{base}/status", token="s3cret-token")
    for field in ("process_started_at", "code_mtime", "config_mtime"):
        assert field in payload, f"/status must report {field}"
        stamp = datetime.fromisoformat(payload[field])
        assert stamp.tzinfo is not None, f"{field} must carry a UTC offset"


def test_status_provenance_obeys_injected_stamps(tmp_path):
    """The staleness invariant, proven over values the test controls.

    SidecarApp accepts process_started_at/code_mtime, and the config lives in
    tmp_path (copied from router.example.yaml) with its mtime pinned by the
    test. The three ages are therefore known BEFORE the assertion: code and
    config are both older than the process start, and the /status payload
    must say exactly that — the same two inequalities a stale service
    violates. No time tolerance, no ordering dependence on the suite's own
    files."""
    config = tmp_path / "router.yaml"
    config.write_text(
        (ROOT / "router.example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # Pin the config mtime to a fixed past instant so the inequality holds by
    # construction, independent of wall-clock timing between write and assert.
    config_mtime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    os.utime(config, (config_mtime.timestamp(), config_mtime.timestamp()))
    # The process start and the code on disk are BOTH after the config mtime.
    started_at = datetime(2026, 1, 2, tzinfo=timezone.utc)

    token_file = tmp_path / "hermes-smart-router.token"
    token_file.write_text("s3cret-token", encoding="utf-8")
    app = SidecarApp(
        RouterService(config),
        token_path=lambda: token_file,
        process_started_at=started_at.isoformat(),
        code_mtime=started_at.isoformat(),
    )

    status, payload = app.dispatch(
        "GET", "/status", {"X-Hermes-Sidecar-Token": "s3cret-token"}
    )
    assert status == 200
    for field in ("process_started_at", "code_mtime", "config_mtime"):
        assert field in payload, f"/status must report {field}"
    assert payload["code_mtime"] <= payload["process_started_at"]
    assert payload["config_mtime"] <= payload["process_started_at"]
    assert payload["config_mtime"] < payload["process_started_at"]


def test_no_response_is_cacheable(running_sidecar):
    """Nem o console nem uma rota JSON podem ser cacheados pelo navegador.

    Regressão de 2026-08-26: o console era servido com 200 e nenhum cabeçalho de
    cache, o painel o buscava com a política default do fetch, e depois de um
    deploy verificado o operador continuava vendo a tela antiga. O invariante é
    do `_write`, que serve as duas famílias de resposta, então as duas são
    afirmadas aqui — uma cópia cacheada de /status seria a mesma mentira que a
    tela evita em toda parte: estado velho apresentado como agora.
    """
    base, _token = running_sidecar

    def headers_of(path: str, token: str | None = None):
        req = urllib.request.Request(f"{base}{path}")
        if token is not None:
            req.add_header(TOKEN_HEADER, token)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}

    status, head = headers_of("/console")
    assert status == 200
    assert head.get("cache-control") == "no-store", head

    status, head = headers_of("/status", token="s3cret-token")
    assert status == 200
    assert head.get("cache-control") == "no-store", head

    # E o cabeçalho não pode ter custado o que já existia.
    assert head.get("content-type") == "application/json"


def test_policy_explain_and_unknown_route(running_sidecar):
    base, _token = running_sidecar
    assert _get(f"{base}/policy", token="s3cret-token")[0] == 200
    assert _get(f"{base}/blocklist", token="s3cret-token")[0] == 200
    ok, payload = _get(f"{base}/explain?task=Debug+a+race+condition", token="s3cret-token")
    assert ok == 200
    assert payload["decision"]["cause"] == "hard_rule"
    assert _get(f"{base}/explain?task=", token="s3cret-token")[0] == 400
    assert _get(f"{base}/nope", token="s3cret-token")[0] == 404


def test_mutating_methods_are_rejected(running_sidecar):
    base, _token = running_sidecar
    assert _get(f"{base}/status", token="s3cret-token", method="POST")[0] == 405


def _post(url: str, token: str | None, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header(TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, (json.loads(body) if body else None)


def test_console_served_over_http_tokenless(running_sidecar):
    base, _token = running_sidecar
    req = urllib.request.Request(f"{base}/console", method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith("text/html")
        assert resp.read(9).lower() == b"<!doctype"


def test_plan_route_happy_path_over_http(running_sidecar):
    base, _token = running_sidecar
    status, body = _post(
        f"{base}/plan", "s3cret-token", {"policy": {"default": {"action": "T1"}}}
    )
    assert status == 200
    assert body["base_hash"]
    # Malformed JSON body is a clean 400, not a 500.
    req = urllib.request.Request(
        f"{base}/plan", data=b"{not json", method="POST"
    )
    req.add_header(TOKEN_HEADER, "s3cret-token")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_apply_revert_with_empty_body_over_http(running_sidecar):
    """A POST with no body parses to {} and reaches dispatch cleanly (do_POST path)."""
    base, _token = running_sidecar
    req = urllib.request.Request(f"{base}/apply/revert", data=b"", method="POST")
    req.add_header(TOKEN_HEADER, "s3cret-token")
    with urllib.request.urlopen(req, timeout=5) as resp:
        # No snapshot yet in a fresh sidecar -> ok:false, but a clean 200 body.
        assert resp.status == 200
        payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is False


def test_missing_token_file_fails_closed(tmp_path):
    missing = tmp_path / "absent.token"
    app = SidecarApp(RouterService(ROOT / "router.yaml"), token_path=lambda: missing)
    server = build_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _get(f"{base}/status", token="anything")[0] == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_build_server_rejects_non_loopback_host():
    app = SidecarApp(RouterService(ROOT / "router.yaml"), token_path=lambda: Path("/dev/null"))
    with pytest.raises(ValueError, match="loopback"):
        build_server("0.0.0.0", 0, app)


def test_build_parser_defaults():
    args = build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8791
    assert args.config.name == "router.yaml"


def test_main_serves_then_shuts_down_cleanly(monkeypatch):
    served = {"forever": 0, "closed": 0}

    class FakeServer:
        def serve_forever(self):
            served["forever"] += 1
            raise KeyboardInterrupt

        def server_close(self):
            served["closed"] += 1

    monkeypatch.setattr("router.one_sidecar.build_server", lambda host, port, app: FakeServer())
    rc = main(["--host", "127.0.0.1", "--port", "0", "--config", str(ROOT / "router.yaml")])
    assert rc == 0
    assert served == {"forever": 1, "closed": 1}


def test_resolve_token_path_platform_default(monkeypatch, tmp_path):
    for var in ("HERMES_EXT_SIDECAR_TOKEN_FILE", "HERMES_WEBUI_STATE_DIR", "HERMES_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("router.one_sidecar.os.name", "posix")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    resolved = resolve_token_path()
    assert resolved == tmp_path / ".hermes" / "webui" / "sidecar-auth" / f"{EXTENSION_ID}.token"


def test_resolve_token_path_honours_state_dir_and_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_EXT_SIDECAR_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
    assert resolve_token_path() == tmp_path / "state" / "sidecar-auth" / f"{EXTENSION_ID}.token"
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert resolve_token_path() == tmp_path / "home" / "webui" / "sidecar-auth" / f"{EXTENSION_ID}.token"
    monkeypatch.setenv("HERMES_EXT_SIDECAR_TOKEN_FILE", str(tmp_path / "explicit.token"))
    assert resolve_token_path() == tmp_path / "explicit.token"


def test_resolve_token_path_windows_local_app_data(monkeypatch, tmp_path):
    for var in ("HERMES_EXT_SIDECAR_TOKEN_FILE", "HERMES_WEBUI_STATE_DIR", "HERMES_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("router.one_sidecar.platform.system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    assert resolve_token_path() == (
        tmp_path / "local-app-data" / "hermes" / "webui" / "sidecar-auth" / f"{EXTENSION_ID}.token"
    )


def test_main_returns_zero_when_server_stops_normally(monkeypatch):
    served = {"forever": 0, "closed": 0}

    class FakeServer:
        def serve_forever(self):
            served["forever"] += 1

        def server_close(self):
            served["closed"] += 1

    monkeypatch.setattr("router.one_sidecar.build_server", lambda host, port, app: FakeServer())
    assert main(["--config", str(ROOT / "router.yaml")]) == 0
    assert served == {"forever": 1, "closed": 1}
