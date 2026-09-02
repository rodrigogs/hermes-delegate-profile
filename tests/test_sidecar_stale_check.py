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

# The suite above uses `chk`; the class below reads better as `checker`. Same module.
checker = chk


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


class TestThePollerReportsWhatItActuallyDid:
    """A safety net that reports success while doing nothing is also a monitoring lie."""

    def test_a_failed_restart_returns_1_and_names_systemds_error(self, monkeypatch):
        """`restart_sidecar` captured systemd's error text and threw it away.

        `main()` then unconditionally printed "restarted <unit>" and returned 0, so a
        masked, failed or missing unit read as a successful restart — while the code
        on disk stayed stale.
        """
        monkeypatch.setattr(checker, "process_started_at",
                            lambda: ("2020-01-01T00:00:00+00:00", ""))
        monkeypatch.setattr(checker, "newest_module_mtime", lambda: 4_000_000_000.0)
        monkeypatch.setattr(
            checker, "restart_sidecar",
            lambda: "Unit hermes-router-sidecar.service is masked.",
        )
        assert checker.main() == 1

    def test_a_successful_restart_still_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(checker, "process_started_at",
                            lambda: ("2020-01-01T00:00:00+00:00", ""))
        monkeypatch.setattr(checker, "newest_module_mtime", lambda: 4_000_000_000.0)
        monkeypatch.setattr(checker, "restart_sidecar", lambda: None)
        assert checker.main() == 0
        assert "restarted" in capsys.readouterr().out

    def test_restart_sidecar_surfaces_systemds_last_line(self, monkeypatch):
        class _Result:
            returncode = 1
            stdout = ""
            stderr = "Failed to restart x.service: Unit x.service is masked.\n"

        monkeypatch.setattr(checker.subprocess, "run", lambda *a, **k: _Result())
        assert "masked" in checker.restart_sidecar()

    def test_restart_sidecar_returns_none_on_success(self, monkeypatch):
        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(checker.subprocess, "run", lambda *a, **k: _Result())
        assert checker.restart_sidecar() is None

    def test_a_nonzero_exit_with_no_output_still_reports_something(self, monkeypatch):
        class _Result:
            returncode = 3
            stdout = ""
            stderr = "   \n"

        monkeypatch.setattr(checker.subprocess, "run", lambda *a, **k: _Result())
        assert "exited 3" in checker.restart_sidecar()

    def test_the_plugin_dir_is_derived_not_hard_coded(self):
        """It was the literal `/home/rodrigo/.hermes/plugins/hermes-smart-router`.

        Anywhere else, `newest_module_mtime()` globbed an empty directory, returned
        0.0, and the poller printed an affirmatively false "fresh" forever — the
        exact stale-code condition it exists to catch.
        """
        from pathlib import Path

        assert checker.PLUGIN_DIR == Path(checker.__file__).resolve().parent.parent
        assert "/home/rodrigo" not in str(checker.PLUGIN_DIR)
        # And it really finds this repo's router modules.
        assert checker.newest_module_mtime() > 0.0

    def test_the_three_none_causes_are_distinguished(self, monkeypatch, tmp_path):
        """They printed one line, blaming Restart=always for a misconfiguration."""
        # 1. token unreadable — systemd will never fix this
        monkeypatch.setattr(checker, "TOKEN_PATH", tmp_path / "no-such.token")
        started, reason = checker.process_started_at()
        assert started is None
        assert "POLLER DISABLED" in reason and "no-such.token" in reason

        # 2. token present but empty — authenticates nothing
        empty = tmp_path / "empty.token"
        empty.write_text("  \n", encoding="utf-8")
        monkeypatch.setattr(checker, "TOKEN_PATH", empty)
        _started, reason = checker.process_started_at()
        assert "is empty" in reason and "POLLER DISABLED" in reason

        # 3. a real token, but the service refuses it
        import urllib.error
        good = tmp_path / "good.token"
        good.write_text("tok", encoding="utf-8")
        monkeypatch.setattr(checker, "TOKEN_PATH", good)

        def _401(*_a, **_k):
            raise urllib.error.HTTPError(checker.SIDECAR, 401, "no", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", _401)
        _started, reason = checker.process_started_at()
        assert "401" in reason and "not the one" in reason

        # 4. genuinely unreachable — THIS is the one systemd owns
        def _refused(*_a, **_k):
            raise OSError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", _refused)
        _started, reason = checker.process_started_at()
        assert "Restart=always" in reason

    def test_an_unexpected_http_status_is_reported_verbatim(
        self, monkeypatch, tmp_path,
    ):
        import urllib.error
        good = tmp_path / "t.token"
        good.write_text("tok", encoding="utf-8")
        monkeypatch.setattr(checker, "TOKEN_PATH", good)

        def _500(*_a, **_k):
            raise urllib.error.HTTPError(checker.SIDECAR, 500, "boom", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", _500)
        _started, reason = checker.process_started_at()
        assert "500" in reason

    def test_the_token_path_falls_back_to_the_env_ladder(self, monkeypatch, tmp_path):
        """The fallback exists so an unimportable router cannot stop the poller."""
        monkeypatch.setenv("HERMES_EXT_SIDECAR_TOKEN_FILE", str(tmp_path / "x.token"))
        assert checker._fallback_token_path() == tmp_path / "x.token"
        monkeypatch.delenv("HERMES_EXT_SIDECAR_TOKEN_FILE")
        monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
        assert checker._fallback_token_path() == (
            tmp_path / "state" / "sidecar-auth" / "hermes-smart-router.token"
        )
        monkeypatch.delenv("HERMES_WEBUI_STATE_DIR")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        assert checker._fallback_token_path() == (
            tmp_path / "home" / "webui" / "sidecar-auth" / "hermes-smart-router.token"
        )

    def test_the_token_path_prefers_the_sidecars_own_resolver(self, monkeypatch, tmp_path):
        """One authority: the poller must look where the SERVICE authorises from."""
        from router.one_sidecar import resolve_token_path

        monkeypatch.setenv("HERMES_EXT_SIDECAR_TOKEN_FILE", str(tmp_path / "auth.token"))
        assert checker.token_path() == resolve_token_path()
