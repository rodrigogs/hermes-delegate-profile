"""Tests for the sidecar stale-code poller (scripts/sidecar-stale-check.py).

The poller is the replacement for the .path unit: it compares the newest
router/*.py mtime on DISK against the sidecar's own process_started_at (via
/status) and restarts the unit when disk is newer. The sidecar cannot see edits
that landed after its boot, so the disk-vs-process comparison is the whole
point — never the sidecar's own reported code_mtime.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import scripts.sidecar_stale_check as chk


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fresh_env(monkeypatch, tmp_path, process_started_at: str | None = None):
    """Point the poller at a tmp router dir + token, return the router dir."""
    router_dir = tmp_path / "router"
    router_dir.mkdir()
    (router_dir / "service.py").write_text("x", encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("tok", encoding="utf-8")
    monkeypatch.setattr(chk, "ROUTER_DIR", router_dir)
    monkeypatch.setattr(chk, "TOKEN_PATH", token)

    if process_started_at is None:
        process_started_at = (
            time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _req, timeout=5: _FakeResponse({"process_started_at": process_started_at}),
    )
    return router_dir


def test_stale_code_restarts_the_unit(monkeypatch, tmp_path):
    router_dir = _fresh_env(monkeypatch, tmp_path, process_started_at="2026-08-19T00:00:00+00:00")
    # The module file's mtime is now (touch) — well after the 00:00 process start.
    (router_dir / "service.py").touch()

    restarted = []
    monkeypatch.setattr(chk, "restart_sidecar", lambda: restarted.append(True))

    assert chk.main() == 0
    assert restarted == [True], "stale code must restart the unit"


def test_fresh_code_does_not_restart(monkeypatch, tmp_path):
    router_dir = _fresh_env(monkeypatch, tmp_path, process_started_at="2026-08-19T01:00:00+00:00")
    (router_dir / "service.py").touch()

    # Make disk OLDER than the process start: backdate the module file. mktime
    # would interpret the literal in LOCAL time; process_started_at is UTC, so
    # build the epoch from an explicit UTC datetime.
    import os as _os
    from datetime import datetime, timezone

    old = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc).timestamp()
    _os.utime(router_dir / "service.py", (old, old))

    restarted = []
    monkeypatch.setattr(chk, "restart_sidecar", lambda: restarted.append(True))

    assert chk.main() == 0
    assert restarted == [], "fresh code must not restart the unit"


def test_offline_sidecar_does_not_restart(monkeypatch, tmp_path):
    router_dir = _fresh_env(monkeypatch, tmp_path)
    (router_dir / "service.py").touch()

    def _raise(_req, timeout=5):
        raise OSError("sidecar down")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    restarted = []
    monkeypatch.setattr(chk, "restart_sidecar", lambda: restarted.append(True))

    assert chk.main() == 0
    assert restarted == [], "a down sidecar belongs to Restart=always, not the poller"
