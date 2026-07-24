"""Hermes One Capability Router sidecar.

A stdlib-only loopback HTTP service consumed through Hermes One's consented
extension-sidecar proxy.  Its only state-changing credential is WebUI's
``token-v1`` secret: every route except ``/health`` requires the per-extension
``X-Hermes-Sidecar-Token`` header.  The service itself is read-only over the
router policy; it cannot edit rules, change providers, or mutate breaker state.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import platform
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from router.service import RouterService
from router.threshold import compute_model_thresholds, p_eff

EXTENSION_ID = "capability-router"
TOKEN_HEADER = "X-Hermes-Sidecar-Token"
_VERSION = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "[::1]", "localhost"}

# Context windows used by the existing dynamic-threshold policy. The sidecar
# only reports the derived values; it does not write these into Hermes config.
MODEL_WINDOWS = {
    "glm-4.5-flash": 272_000,
    "glm-4.7": 200_000,
    "gpt-5.6-terra": 1_000_000,
    "deepseek-v4-pro": 128_000,
}
SUMMARIZER_WINDOW = 272_000


@dataclass(frozen=True)
class TokenState:
    token: Optional[str]
    present: bool


def resolve_token_path(extension_id: str = EXTENSION_ID) -> Path:
    """Resolve the token-v1 file using the WebUI's documented precedence."""
    explicit = os.environ.get("HERMES_EXT_SIDECAR_TOKEN_FILE")
    if explicit:
        return Path(explicit)

    state_dir = os.environ.get("HERMES_WEBUI_STATE_DIR")
    if state_dir:
        return Path(state_dir) / "sidecar-auth" / f"{extension_id}.token"

    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home) / "webui" / "sidecar-auth" / f"{extension_id}.token"

    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "hermes" / "webui" / "sidecar-auth" / f"{extension_id}.token"
    return Path.home() / ".hermes" / "webui" / "sidecar-auth" / f"{extension_id}.token"


def read_expected_token(extension_id: str = EXTENSION_ID) -> TokenState:
    """Read the expected token. Missing/unreadable means sidecar unavailable."""
    path = resolve_token_path(extension_id)
    try:
        return TokenState(token=path.read_text(encoding="utf-8").strip(), present=True)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, OSError):
        return TokenState(token=None, present=False)


def _error(status: int, message: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"error": message}


class SidecarApp:
    """Authenticated read-only request dispatcher, independent of HTTP sockets."""

    def __init__(self, service: RouterService, token_path: Callable[[], Path] = resolve_token_path):
        self._service = service
        self._token_path = token_path

    def _expected_token(self) -> TokenState:
        try:
            raw = self._token_path().read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError, OSError):
            return TokenState(token=None, present=False)
        return TokenState(token=raw.strip(), present=True)

    def _authorize(self, headers: Dict[str, str]) -> Optional[Tuple[int, Dict[str, Any]]]:
        expected = self._expected_token()
        if not expected.present or not expected.token:
            return _error(503, "sidecar token not provisioned")
        supplied = next(
            (value for name, value in headers.items() if name.lower() == TOKEN_HEADER.lower()), None
        )
        if supplied is None or not hmac.compare_digest(supplied, expected.token):
            return _error(401, "invalid sidecar token")
        return None

    def dispatch(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        query: Optional[Dict[str, List[str]]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Serve an HTTP-shaped request without binding a socket."""
        query = query or {}
        if method != "GET":
            return _error(405, "method not allowed")
        if path == "/health":
            return 200, {"ok": True, "service": EXTENSION_ID, "version": _VERSION}

        denial = self._authorize(headers)
        if denial is not None:
            return denial
        if path == "/status":
            return 200, self._service.status()
        if path == "/policy":
            return 200, self._service.policy()
        if path == "/blocklist":
            return 200, self._service.blocklist()
        if path == "/liveness":
            return 200, self._service.liveness()
        if path == "/compaction":
            try:
                aggressiveness = int((query.get("aggr") or ["50"])[0])
            except (TypeError, ValueError):
                return _error(400, "aggr must be an integer between 0 and 100")
            if not 0 <= aggressiveness <= 100:
                return _error(400, "aggr must be an integer between 0 and 100")
            threshold_fraction = p_eff(SUMMARIZER_WINDOW, aggressiveness)
            threshold_tokens = int(SUMMARIZER_WINDOW * threshold_fraction)
            return 200, {
                "aggressiveness": aggressiveness,
                "model_thresholds": compute_model_thresholds(
                    MODEL_WINDOWS.items(), aggressiveness
                ),
                "summarizer_window": SUMMARIZER_WINDOW,
                "threshold_fraction": threshold_fraction,
                "threshold_tokens": threshold_tokens,
                "warning": threshold_tokens >= SUMMARIZER_WINDOW,
            }
        if path == "/lint":
            return 200, self._service.lint()
        if path == "/explain":
            task = ((query.get("task") or [""])[0]).strip()
            try:
                return 200, self._service.explain(task)
            except ValueError as exc:
                return _error(400, str(exc))
        return _error(404, "unknown route")


def _make_handler(app: SidecarApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _serve(self, method: str) -> None:
            parts = urlsplit(self.path)
            status, payload = app.dispatch(
                method,
                parts.path,
                {name: value for name, value in self.headers.items()},
                parse_qs(parts.query),
            )
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._serve("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._serve("POST")

    return Handler


def build_server(host: str, port: int, app: SidecarApp) -> ThreadingHTTPServer:
    """Build a server only on a loopback address; never expose the sidecar."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("sidecar host must be loopback")
    return ThreadingHTTPServer((host, port), _make_handler(app))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "router.yaml",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    app = SidecarApp(RouterService(args.config))
    server = build_server(args.host, args.port, app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
