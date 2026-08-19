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

Exit code is always 0: a poll that found nothing wrong is not a failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path("/home/rodrigo/.hermes/plugins/delegate-profile")
ROUTER_DIR = PLUGIN_DIR / "router"
# The unit sets HERMES_HOME to the real home (not a profile-scoped one); the
# same fallback the smoke script uses.
TOKEN_PATH = (
    Path(os.environ.get("HERMES_HOME", "/home/rodrigo/.hermes"))
    / "webui/sidecar-auth/hermes-one-capability-router.token"
)
SIDECAR = "http://127.0.0.1:8791/status"
UNIT = "hermes-router-sidecar.service"


def newest_module_mtime() -> float:
    """Newest mtime among router/*.py, or 0.0 when none are readable."""
    newest = 0.0
    for py_file in ROUTER_DIR.glob("*.py"):
        try:
            newest = max(newest, py_file.stat().st_mtime)
        except OSError:
            continue
    return newest


def process_started_at() -> str | None:
    """ISO process_started_at from /status, or None when the sidecar is down."""
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        import urllib.request

        req = urllib.request.Request(SIDECAR, headers={"X-Hermes-Sidecar-Token": token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("process_started_at")
    except (OSError, ValueError):
        return None


def restart_sidecar() -> None:
    subprocess.run(
        ["systemctl", "--user", "restart", UNIT],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    started = process_started_at()
    if started is None:
        # Sidecar down: systemd's Restart=always owns that problem; do not pile
        # on a restart race from the poller.
        print("sidecar-stale-check: /status unreachable, leaving Restart=always to it")
        return 0
    started_ts = datetime.fromisoformat(started).timestamp()
    disk = newest_module_mtime()
    if disk > started_ts:
        restart_sidecar()
        age_h = (disk - started_ts) / 3600
        print(f"sidecar-stale-check: code {age_h:.1f}h newer than process; restarted {UNIT}")
    else:
        print(f"sidecar-stale-check: fresh (code {max(0.0, started_ts - disk) / 3600:.1f}h older)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
