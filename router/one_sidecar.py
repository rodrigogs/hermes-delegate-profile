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
import subprocess
import tempfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from urllib.parse import parse_qs, urlsplit

from router.service import RouterService
from router.threshold import apply_dynamic_thresholds, compute_model_thresholds, p_eff

EXTENSION_ID = "capability-router"
TOKEN_HEADER = "X-Hermes-Sidecar-Token"
_VERSION = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "[::1]", "localhost"}

# The impeccable console ships beside the extension; the sidecar serves it as a
# static, same-origin HTML shell (auth-exempt like /health). All data it renders
# still flows through the token-gated JSON routes.
_CONSOLE_PATH = (
    Path(__file__).resolve().parent.parent
    / "webui_extension"
    / "capability-router"
    / "console.html"
)

# Routes grouped by the single HTTP method each accepts. A known route hit with
# the wrong method is a 405 (before auth, matching the historical POST/health
# contract); a route in neither set is a 404 (after auth).
_GET_ROUTES = frozenset(
    {"/health", "/status", "/policy", "/blocklist", "/liveness",
     "/compaction", "/lint", "/explain"}
)
_POST_ROUTES = frozenset({"/plan", "/apply", "/apply/confirm", "/apply/revert"})

# Context windows used by the existing dynamic-threshold policy. The sidecar
# only reports the derived values; it does not write these into Hermes config.
MODEL_WINDOWS = {
    "glm-4.5-flash": 272_000,
    "glm-4.7": 200_000,
    "gpt-5.6-terra": 1_000_000,
    "deepseek-v4-pro": 128_000,
}
SUMMARIZER_WINDOW = 272_000

# The exact phrase an operator must echo to arm the RESTART-class compaction
# apply, mirrored server-side (the console gates it client-side too).
_COMPACTION_CONFIRM = "COMPACT"

# The proven dead-man switch: validate -> backup -> detached(apply + restart +
# health-poll -> auto-revert). It owns the config.yaml mutation and recovery; the
# sidecar only hands it a fully-formed candidate config and returns immediately.
_SAFE_RESTART = Path.home() / "bin" / "hermes-safe-restart.sh"


def resolve_core_config_path() -> Path:
    """Resolve the Hermes core (profile) config.yaml — the compaction target.

    RESTART-class: unlike router.yaml this is not hot-reloaded, so edits here go
    exclusively through the safe-restart dead-man switch.
    """
    explicit = os.environ.get("HERMES_CORE_CONFIG_FILE")
    if explicit:
        return Path(explicit)
    profile = os.environ.get("HERMES_PROFILE", "rodrigo")
    home = os.environ.get("HERMES_HOME")
    base = Path(home) if home else Path.home() / ".hermes"
    return base / "profiles" / profile / "config.yaml"


def _default_restart_runner(candidate_path: Path) -> Dict[str, Any]:
    """Invoke the safe-restart script on a candidate config, returning promptly.

    The script backgrounds the apply+restart+health-poll+auto-revert via
    systemd-run and returns in well under a second, so a short timeout only trips
    on a genuinely missing/broken launcher, never on the restart itself.
    """
    if not _SAFE_RESTART.exists():
        return {"ok": False, "error": f"safe-restart launcher not found at {_SAFE_RESTART}"}
    try:
        proc = subprocess.run(
            ["bash", str(_SAFE_RESTART), str(candidate_path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"safe-restart invocation failed: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": "safe-restart rejected the candidate config",
                "detail": (proc.stderr or proc.stdout or "").strip()[-400:]}
    return {"ok": True, "restart": "scheduled"}


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


def parse_json_body(
    content_length: Optional[str], reader: Callable[[int], bytes]
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Parse a POST body into ``(value, ok)``.

    ``ok`` is False only on malformed JSON. A missing/zero/invalid length or an
    empty body yields ``({}, True)`` so a no-payload POST (e.g. revert) still
    reaches dispatch cleanly. Pure over an injected reader so it is unit-testable
    without binding a socket.
    """
    try:
        length = int(content_length or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return {}, True
    raw = reader(length)
    if not raw:
        return {}, True
    try:
        return json.loads(raw.decode("utf-8")), True
    except (ValueError, UnicodeDecodeError):
        return None, False


class SidecarApp:
    """Authenticated read-only request dispatcher, independent of HTTP sockets."""

    def __init__(
        self,
        service: RouterService,
        token_path: Callable[[], Path] = resolve_token_path,
        console_path: Path = _CONSOLE_PATH,
        core_config_path: Optional[Callable[[], Path]] = None,
        restart_runner: Callable[[Path], Dict[str, Any]] = _default_restart_runner,
        model_windows: Optional[Dict[str, int]] = None,
        summarizer_window: int = SUMMARIZER_WINDOW,
    ):
        self._service = service
        self._token_path = token_path
        self._console_path = console_path
        self._core_config_path = core_config_path or resolve_core_config_path
        self._restart_runner = restart_runner
        self._model_windows = model_windows or dict(MODEL_WINDOWS)
        self._summarizer_window = summarizer_window

    def render_console(self) -> Tuple[int, bytes, str]:
        """Return the console HTML shell as ``(status, body, content_type)``.

        Read-only and auth-exempt: it is the container the browser loads, and
        every datum it shows is fetched afterwards through the token-gated JSON
        routes. A missing file degrades to a JSON 404, never a traceback.
        """
        try:
            return 200, self._console_path.read_bytes(), "text/html; charset=utf-8"
        except OSError:
            return 404, b'{"error":"console not found"}', "application/json"

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
        body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Serve an HTTP-shaped request without binding a socket."""
        query = query or {}

        # Method-per-route check runs before auth so a wrong-method hit on a
        # known route is a 405 whether or not a token was supplied (preserves
        # the historical POST /health -> 405 and POST /status -> 405 contract).
        known = path in _GET_ROUTES or path in _POST_ROUTES
        if known:
            allowed = "GET" if path in _GET_ROUTES else "POST"
            if method != allowed:
                return _error(405, "method not allowed")

        # /health is the only auth-exempt data route.
        if path == "/health":
            return 200, {"ok": True, "service": EXTENSION_ID, "version": _VERSION}

        denial = self._authorize(headers)
        if denial is not None:
            return denial

        if method == "GET":
            return self._dispatch_get(path, query)
        if method == "POST":
            return self._dispatch_post(path, body)
        # Unknown route with an unmodelled method: method check above only fired
        # for known routes, so this is genuinely not found.
        return _error(404, "unknown route")

    def _dispatch_get(
        self, path: str, query: Dict[str, List[str]]
    ) -> Tuple[int, Dict[str, Any]]:
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

    def _dispatch_post(
        self, path: str, body: Optional[Dict[str, Any]]
    ) -> Tuple[int, Dict[str, Any]]:
        if not isinstance(body, dict):
            return _error(400, "request body must be a JSON object")

        if path == "/plan":
            policy = body.get("policy", body.get("changes"))
            if not isinstance(policy, dict):
                return _error(400, "plan requires a 'policy' object")
            try:
                return 200, self._service.plan(policy)
            except ValueError as exc:
                return _error(400, str(exc))

        if path == "/apply":
            # The console overloads /apply for the RESTART-class compaction
            # action, disambiguated only by body.action.
            if body.get("action") == "compaction":
                return self._apply_compaction(body)
            return self._commit_policy(body)

        # confirm is the console's second-stage commit button; it commits the
        # same way apply does, so a click after an interrupted apply reconciles
        # against the current on-disk hash (a drift returns a clean 409) rather
        # than dead-ending on a 404.
        if path == "/apply/confirm":
            return self._commit_policy(body)

        if path == "/apply/revert":
            return 200, self._service.apply_revert()

        return _error(404, "unknown route")

    def _commit_policy(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Shared commit for /apply and /apply/confirm: hash-checked write."""
        plan = body.get("plan")
        if not isinstance(plan, dict):
            return _error(400, "apply requires the 'plan' returned by /plan")
        base_hash = plan.get("base_hash")
        policy = body.get("policy", plan.get("policy"))
        if not isinstance(base_hash, str) or not base_hash:
            return _error(400, "apply requires plan.base_hash")
        if not isinstance(policy, dict):
            return _error(400, "apply requires a 'policy' object")
        try:
            result = self._service.apply(base_hash, policy)
        except ValueError as exc:
            return _error(400, str(exc))
        if result.get("conflict"):
            # Optimistic-concurrency drift: someone wrote router.yaml since the
            # plan was computed. 409 lets the UI re-plan against fresh state.
            return 409, result
        if not result.get("ok"):
            return 400, result
        return 200, result

    def _apply_compaction(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """RESTART-class: recompute compaction thresholds and hand a candidate
        config.yaml to the safe-restart dead-man switch.

        This does NOT ride RouterService.apply (that is the router.yaml hot
        path) and never writes config.yaml inline — the dead-man switch owns the
        mutation and the health-gated auto-revert. Requires a server-side
        type-to-confirm mirroring the console's client gate.
        """
        if body.get("confirm") != _COMPACTION_CONFIRM:
            return _error(400, f"compaction requires confirm={_COMPACTION_CONFIRM}")
        aggressiveness = body.get("aggressiveness", 50)
        if not isinstance(aggressiveness, int) or not 0 <= aggressiveness <= 100:
            return _error(400, "aggressiveness must be an integer between 0 and 100")

        core_path = self._core_config_path()
        try:
            current = yaml.safe_load(core_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            return _error(400, f"could not read core config: {exc}")
        if not isinstance(current, dict):
            return _error(400, "core config root must be a mapping")

        candidate = apply_dynamic_thresholds(
            current, aggressiveness, self._summarizer_window, self._model_windows
        )
        # Write the candidate to a temp file for the launcher to validate + apply.
        fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="compaction-candidate-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(candidate, handle, sort_keys=False)
            result = self._restart_runner(Path(tmp_path))
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return _error(500, f"could not stage candidate config: {exc}")
        if not result.get("ok"):
            return 502, result
        # 202 Accepted: the restart is scheduled and health-gated; it has not
        # necessarily completed when this returns.
        return 202, {**result, "aggressiveness": aggressiveness}


def _make_handler(app: SidecarApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _write(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve(self, method: str, body: Optional[Dict[str, Any]] = None) -> None:
            parts = urlsplit(self.path)
            # The console is a static HTML shell, served outside the JSON path.
            if method == "GET" and parts.path == "/console":
                status, html, content_type = app.render_console()
                self._write(status, html, content_type)
                return
            status, payload = app.dispatch(
                method,
                parts.path,
                {name: value for name, value in self.headers.items()},
                parse_qs(parts.query),
                body,
            )
            self._write(status, json.dumps(payload).encode("utf-8"), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            self._serve("GET")

        def do_POST(self) -> None:  # noqa: N802
            parsed, ok = parse_json_body(
                self.headers.get("Content-Length"), self.rfile.read
            )
            if not ok:
                self._write(
                    400,
                    json.dumps({"error": "request body is not valid JSON"}).encode("utf-8"),
                    "application/json",
                )
                return
            self._serve("POST", parsed)

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
