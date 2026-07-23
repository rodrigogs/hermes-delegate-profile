"""End-to-end tests that boot the real sidecar HTTP server on an ephemeral port.

These cover the server loop, the BaseHTTPRequestHandler dispatch, the CLI parser,
the loopback-host guard, and main()'s serve/shutdown path — the paths a pure
SidecarApp.dispatch() unit test cannot reach.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
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
def running_sidecar(tmp_path):
    token_file = tmp_path / "capability-router.token"
    token_file.write_text("s3cret-token", encoding="utf-8")
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
    assert payload == {"ok": True, "service": EXTENSION_ID, "version": 1}


def test_status_requires_valid_token(running_sidecar):
    base, _token = running_sidecar
    assert _get(f"{base}/status")[0] == 401
    assert _get(f"{base}/status", token="wrong")[0] == 401
    status, payload = _get(f"{base}/status", token="s3cret-token")
    assert status == 200
    assert "enabled" in payload


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
