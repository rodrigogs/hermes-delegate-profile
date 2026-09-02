#!/usr/bin/env python3
"""Restart the sidecar when it serves code older than what is on disk.

The .path-unit approach (inotify on router/) was tried first and does not fire
on this WSL box: systemd 255 arms the watch registration but never opens an
inotify fd (verified 2026-08-19: /proc/<manager>/fd had zero inotify fds while
the path unit reported Paths= active). A poller cannot miss an edit the way a
silently-dead watch does, so this is the mechanism.

Staleness is disk-vs-process, NOT the sidecar's own report: /status carries the
mtimes the sidecar captured at boot, so a running sidecar cannot see the edit
that happened after it started. The check reads the process start from /status
and stats the newest router/*.py itself; when disk is newer, it restarts the
unit (which the .path trigger cannot do either — its restart is a no-op on a
manually-started service).

EXIT CODES. 0 when the poll found nothing wrong or fixed what it found; 1 when a
restart it announced did not happen. This used to be "always 0", which sounds
conservative and was the opposite: a safety net that reports success while doing
nothing is worse than no safety net, because it is also a monitoring signal.

Four things were wrong with the old version, one cause — every one of them made it
report a state it had not verified:

  * ``restart_sidecar`` ran ``systemctl --user restart`` with ``check=False``,
    ``capture_output=True`` and DISCARDED the result, then main() unconditionally
    printed "restarted <unit>" and returned 0. systemd's own error text was
    captured and thrown away, so a masked, failed or missing unit read as a
    successful restart.
  * ``PLUGIN_DIR`` was the literal ``/home/rodrigo/.hermes/plugins/hermes-smart-router``.
    Anywhere else, ``newest_module_mtime()`` globbed an empty directory, returned
    0.0, and the poller printed an affirmatively false "fresh" forever — the exact
    stale-code condition it exists to catch.
  * the token path hard-coded rung 3 of ``one_sidecar.resolve_token_path``'s
    four-rung ladder, while the sidecar's own unit sets
    ``HERMES_WEBUI_STATE_DIR``. Reading the wrong file yields None, which was
    indistinguishable from "the sidecar is down".
  * all three ``None`` causes printed the same line, blaming ``Restart=always``
    for what is usually a misconfiguration it cannot fix.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

#: This file lives at <plugin>/scripts/, so the plugin root is two parents up.
#: DERIVED, never a literal: a hard-coded path made every other install silently
#: report "fresh".
PLUGIN_DIR = Path(__file__).resolve().parent.parent
ROUTER_DIR = PLUGIN_DIR / "router"
SIDECAR = "http://127.0.0.1:8791/status"
UNIT = "hermes-router-sidecar.service"


def _fallback_token_path() -> Path:
    """The token path from the environment alone, for an unimportable router.

    Mirrors the top two rungs of ``one_sidecar.resolve_token_path`` — the two the
    sidecar's own unit actually sets — and is used only when that module cannot be
    imported. Named and commented as a fallback so it is not mistaken for the
    authority.
    """
    explicit = os.environ.get("HERMES_EXT_SIDECAR_TOKEN_FILE")
    if explicit:
        return Path(explicit)
    state_dir = os.environ.get("HERMES_WEBUI_STATE_DIR")
    base = Path(state_dir) if state_dir else (
        Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "webui"
    )
    return base / "sidecar-auth" / "hermes-smart-router.token"


def token_path() -> Path:
    """Where the WebUI minted this extension's token.

    Delegates to ``one_sidecar.resolve_token_path`` — the SAME resolver the sidecar
    authorises with — so the poller cannot look in a different place than the
    service it polls. That is the whole failure this fixes: rung 3 hard-coded here
    against a unit that sets the variable rung 2 reads.

    The import is lazy and guarded because this runs as a systemd oneshot: the
    module pulls in PyYAML and RouterService, and a poller that dies on an import
    is a poller that stops watching. ``sys.path`` needs the plugin root explicitly —
    ``ExecStart=<python> scripts/...py`` puts ``sys.path[0]`` at ``scripts/``, and
    ``WorkingDirectory`` alone does not help (reproduced: ModuleNotFoundError).
    """
    try:
        if str(PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(PLUGIN_DIR))
        from router.one_sidecar import resolve_token_path
        return resolve_token_path()
    except Exception:
        return _fallback_token_path()


#: Module-level for the tests that monkeypatch it; resolved lazily in main() so an
#: env change between import and call is honoured.
TOKEN_PATH = None


def newest_module_mtime() -> float:
    """Newest mtime among router/*.py, or 0.0 when none are readable."""
    newest = 0.0
    for py_file in ROUTER_DIR.glob("*.py"):
        try:
            newest = max(newest, py_file.stat().st_mtime)
        except OSError:
            continue
    return newest


def process_started_at() -> tuple[str | None, str]:
    """``(iso_started_at, reason)``. ``reason`` is "" on success.

    The three failure causes are DISTINGUISHED, because they need different
    actions and only one of them is systemd's problem: an unreadable token is a
    misconfiguration that ``Restart=always`` will never fix, a 401 means the token
    is stale, and a refused connection means the service really is down.
    """
    path = TOKEN_PATH if TOKEN_PATH is not None else token_path()
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"token unreadable at {path} ({exc.strerror}) — POLLER DISABLED"
    if not token:
        return None, f"token file {path} is empty — POLLER DISABLED"
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(SIDECAR, headers={"X-Hermes-Sidecar-Token": token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("process_started_at"), ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 503):
            return None, (
                f"/status answered {exc.code} — the token at {path} is not the one "
                f"the sidecar expects"
            )
        return None, f"/status answered {exc.code}"
    except (OSError, ValueError) as exc:
        return None, f"/status unreachable ({exc}) — leaving Restart=always to it"


def restart_sidecar() -> str | None:
    """Restart the unit. None on success, else systemd's own last error line.

    The result used to be captured and discarded. Returning it is what lets main()
    stop announcing a restart that did not happen — and the three tests that
    monkeypatch this with a ``lambda`` returning None keep working unchanged.
    """
    result = subprocess.run(
        ["systemctl", "--user", "restart", UNIT],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return detail[-1] if detail else f"systemctl exited {result.returncode}"


def main() -> int:
    started, reason = process_started_at()
    if started is None:
        print(f"sidecar-stale-check: {reason}")
        return 0
    started_ts = datetime.fromisoformat(started).timestamp()
    disk = newest_module_mtime()
    if disk > started_ts:
        error = restart_sidecar()
        age_h = (disk - started_ts) / 3600
        if error:
            print(
                f"sidecar-stale-check: code {age_h:.1f}h newer than process and the "
                f"restart FAILED: {error}",
                file=sys.stderr,
            )
            return 1
        print(f"sidecar-stale-check: code {age_h:.1f}h newer than process; restarted {UNIT}")
    else:
        print(f"sidecar-stale-check: fresh (code {max(0.0, started_ts - disk) / 3600:.1f}h older)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
