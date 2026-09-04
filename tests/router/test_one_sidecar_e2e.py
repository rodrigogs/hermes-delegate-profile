"""End-to-end tests that boot the real sidecar HTTP server on an ephemeral port.

These cover the server loop, the BaseHTTPRequestHandler dispatch, the CLI parser,
the loopback-host guard, and main()'s serve/shutdown path — the paths a pure
SidecarApp.dispatch() unit test cannot reach.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import router.one_sidecar as sidecar_mod
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

# Every local HTTP read and thread join in this file waits this long.
#
# It was a bare `timeout=5` in thirteen places. These are loopback requests to a
# ThreadingHTTPServer booted in-process, so five seconds is enormous when the machine is
# idle and NOT ENOUGH when it is not: runbook §23.3 trap 5 records this file's
# `test_console_gzip_is_cached_by_file_version` failing a full coverage run and leaving
# `one_sidecar.py`'s post-/console `return` uncovered, with the guidance "suspect a race in
# the e2e sidecar test rather than a real gap". Reproduced 2026-09-04 at load average 78
# and again at 96, always as `TimeoutError: timed out` inside `readline` — never as a
# wrong answer.
#
# Raised rather than retried, because a retry would hide a genuine hang. Thirty seconds
# still bounds one: nothing here legitimately takes longer than a few milliseconds, so a
# test that needs 30s has found a real deadlock and should fail. One constant, because
# thirteen copies of a number are thirteen chances to tune one and miss twelve.
_LOCAL_HTTP_TIMEOUT = 30


def _get(url: str, token: str | None = None, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    if token is not None:
        req.add_header(TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=_LOCAL_HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, (json.loads(body) if body else None)


def _boot_test_server(app):
    """Sobe o servidor de teste com teardown determinístico.

    O ThreadingHTTPServer de produção traz daemon_threads=True, e o
    `_Threads` do ThreadingMixIn recusa thread daemon — logo o server_close()
    NÃO espera as threads de atendimento, só o join do teste espera o
    serve_forever. Invertemos as duas marcas na instância para o
    server_close() fazer join de cada handler em voo.

    O build_server de PRODUÇÃO fica como está: lá o daemon é deliberado — um
    shutdown não deve esperar uma requisição pendurada.

    NOTA (medido em 2026-08-27): isto torna o teardown determinístico, mas
    NÃO é a causa do flake da linha 668. A causa medida foi o cliente fechar
    antes de ler o corpo do /console (595 KB): o write estourava
    ConnectionResetError e o _serve nunca chegava ao return — por isso os
    testes de /console leem o corpo inteiro (test_console_served_over_http_tokenless).
    """
    server = build_server("127.0.0.1", 0, app)
    server.daemon_threads = False
    server.block_on_close = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


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
    server, thread = _boot_test_server(app)
    host, port = server.server_address[0], server.server_address[1]
    base = f"http://{host}:{port}"
    try:
        yield base, token_file
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=_LOCAL_HTTP_TIMEOUT)


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
    server, thread = _boot_test_server(app)
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
        thread.join(timeout=_LOCAL_HTTP_TIMEOUT)


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
        with urllib.request.urlopen(req, timeout=_LOCAL_HTTP_TIMEOUT) as resp:
            head = {k.lower(): v for k, v in resp.headers.items()}
            # Drenar o corpo antes de fechar. Medido em 2026-08-27: um cliente
            # que fecha no meio do /console (595 KB) faz o write do handler
            # estourar ConnectionResetError e aborta o _serve antes do seu
            # return — a linha 668 sumia da cobertura e o gate reprovava em
            # 99,99% por sorteio. Navegador de verdade lê a página inteira;
            # o teste também.
            resp.read()
            return resp.status, head

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
        with urllib.request.urlopen(req, timeout=_LOCAL_HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, (json.loads(body) if body else None)


def test_console_served_over_http_tokenless(running_sidecar):
    """O console chega inteiro, não truncado.

    Lê o corpo COMPLETO (595 KB) e confere contra o Content-Length. Dois
    motivos, um de contrato e um de medição:
    - contrato: um console truncado quebra a tela; o painel depende do HTML
      todo.
    - medição (2026-08-27): um cliente que fecha antes do fim do write faz o
      handler estourar ConnectionResetError e aborta o _serve antes do seu
      return — a última linha do ramo /console sumia da cobertura e o gate
      reprovava em 99,99% por sorteio. Cliente que lê tudo não tem corrida.
    """
    base, _token = running_sidecar
    req = urllib.request.Request(f"{base}/console", method="GET")
    with urllib.request.urlopen(req, timeout=_LOCAL_HTTP_TIMEOUT) as resp:
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith("text/html")
        body = resp.read()
        assert int(resp.headers.get("Content-Length", "0")) == len(body)
        assert body[:9].lower() == b"<!doctype"


def test_sidecar_negotiates_gzip_for_console_and_json(running_sidecar):
    """Compression is opt-in, so the deployment byte comparison stays valid.

    The proxy enforces its response ceiling on the encoded body, while the browser
    must receive the exact console source after decoding. Test the wire bytes and
    source bytes separately: comparing the compressed bytes to the file would
    repeat the encoder rather than prove the consumer contract.
    """
    base, _token = running_sidecar
    source = (ROOT / "webui_extension" / EXTENSION_ID / "console.html").read_bytes()

    def response(path: str, headers: dict[str, str] | None = None):
        request = urllib.request.Request(f"{base}{path}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=_LOCAL_HTTP_TIMEOUT) as resp:
            return resp.status, {key.lower(): value for key, value in resp.headers.items()}, resp.read()

    status, headers, encoded = response("/console", {"Accept-Encoding": "gzip"})
    assert status == 200
    assert headers.get("content-encoding") == "gzip"
    assert headers.get("vary") == "Accept-Encoding"
    assert int(headers["content-length"]) == len(encoded)
    assert gzip.decompress(encoded) == source

    status, headers, raw = response("/console")
    assert status == 200
    assert "content-encoding" not in headers
    assert headers.get("vary") == "Accept-Encoding"
    assert int(headers["content-length"]) == len(raw)
    assert raw == source

    status, headers, encoded_json = response(
        "/status", {TOKEN_HEADER: "s3cret-token", "Accept-Encoding": "br, gzip"}
    )
    assert status == 200
    assert headers.get("content-encoding") == "gzip"
    assert headers.get("vary") == "Accept-Encoding"
    assert int(headers["content-length"]) == len(encoded_json)
    assert json.loads(gzip.decompress(encoded_json)) is not None


def test_console_gzip_is_cached_by_file_version(tmp_path, monkeypatch):
    """Two reads share one gzip result; a new mtime creates exactly one new one."""
    console = tmp_path / "console.html"
    console.write_bytes(b"<!doctype html><title>cached</title>")
    token = tmp_path / "token"
    token.write_text("test-token", encoding="utf-8")
    app = SidecarApp(
        RouterService(ROOT / "router.yaml"), token_path=lambda: token, console_path=console
    )
    calls = 0
    original_compress = gzip.compress

    def count_compress(body: bytes) -> bytes:
        nonlocal calls
        calls += 1
        return original_compress(body)

    monkeypatch.setattr(sidecar_mod.gzip, "compress", count_compress)
    server, thread = _boot_test_server(app)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for _ in range(2):
            request = urllib.request.Request(f"{base}/console")
            request.add_header("Accept-Encoding", "gzip")
            with urllib.request.urlopen(request, timeout=_LOCAL_HTTP_TIMEOUT) as response:
                assert gzip.decompress(response.read()) == console.read_bytes()
        assert calls == 1

        stat = console.stat()
        os.utime(console, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
        request = urllib.request.Request(f"{base}/console")
        request.add_header("Accept-Encoding", "gzip")
        with urllib.request.urlopen(request, timeout=_LOCAL_HTTP_TIMEOUT) as response:
            assert gzip.decompress(response.read()) == console.read_bytes()
        assert calls == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=_LOCAL_HTTP_TIMEOUT)


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
        urllib.request.urlopen(req, timeout=_LOCAL_HTTP_TIMEOUT)
        assert False, "expected HTTPError"
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_apply_revert_with_empty_body_over_http(running_sidecar):
    """A POST with no body parses to {} and reaches dispatch cleanly (do_POST path)."""
    base, _token = running_sidecar
    req = urllib.request.Request(f"{base}/apply/revert", data=b"", method="POST")
    req.add_header(TOKEN_HEADER, "s3cret-token")
    with urllib.request.urlopen(req, timeout=_LOCAL_HTTP_TIMEOUT) as resp:
        # No snapshot yet in a fresh sidecar -> ok:false, but a clean 200 body.
        assert resp.status == 200
        payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is False


def test_missing_token_file_fails_closed(tmp_path):
    missing = tmp_path / "absent.token"
    app = SidecarApp(RouterService(ROOT / "router.yaml"), token_path=lambda: missing)
    server, thread = _boot_test_server(app)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _get(f"{base}/status", token="anything")[0] == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=_LOCAL_HTTP_TIMEOUT)


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


def test_console_loads_without_a_token_and_refuses_a_wrong_method(tmp_path):
    """The two /console facts that only a real server can show.

    The token exemption is deliberate and now stated in the module docstring: the screen
    has to load in order to EXPLAIN a missing token, so a 401 would take the explanation
    down with the problem. And a wrong method is a 405 rather than a 404 — /console used
    to be in neither route set, which made a method error indistinguishable from a typo.
    """
    console = tmp_path / "console.html"
    console.write_bytes(b"<!doctype html><title>shell</title>")
    token = tmp_path / "token"
    token.write_text("tok", encoding="utf-8")
    app = SidecarApp(
        RouterService(ROOT / "router.yaml"), token_path=lambda: token, console_path=console
    )
    server, thread = _boot_test_server(app)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # No token at all, and it still serves the shell.
        with urllib.request.urlopen(f"{base}/console", timeout=_LOCAL_HTTP_TIMEOUT) as resp:
            assert resp.status == 200
            assert resp.read() == console.read_bytes()
        # Wrong method on a known route.
        req = urllib.request.Request(f"{base}/console", data=b"{}", method="POST")
        req.add_header("X-Hermes-Sidecar-Token", "tok")
        try:
            urllib.request.urlopen(req, timeout=_LOCAL_HTTP_TIMEOUT)
            raise AssertionError("POST /console must not succeed")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405, f"got {exc.code}, want 405 (not 404)"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=_LOCAL_HTTP_TIMEOUT)
