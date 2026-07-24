"""Tests for DurableDecisionLog — atomic, bounded, fail-safe route-trace persistence."""

from __future__ import annotations

import json

import pytest

import router.durable_decision_log as ddl
from router.durable_decision_log import DurableDecisionLog, routes_path


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    """Point _state_dir() at a temp HERMES_HOME so nothing touches the real box."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_record_appends_one_parseable_jsonl_line(state_home):
    log = DurableDecisionLog()
    log.record("hard_rule", {"model": "T4"}, task_preview="fix a bug",
               steps=[{"stage": "blocklist", "out": {"blocked": False}}])
    path = routes_path()
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["cause"] == "hard_rule"
    assert entry["steps"][0]["stage"] == "blocklist"
    # In-memory list is still populated (base behavior preserved).
    assert log.entries()[0]["cause"] == "hard_rule"


def test_multiple_records_append(state_home):
    log = DurableDecisionLog()
    for i in range(3):
        log.record("classifier", {"model": f"m{i}"}, task_preview=f"t{i}")
    lines = routes_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(l)["output"]["model"] for l in lines] == ["m0", "m1", "m2"]


def test_rotation_caps_disk_and_prunes_oldest(state_home, monkeypatch):
    # Tiny cap so a couple of records force rotation deterministically.
    monkeypatch.setattr(ddl, "_TRACE_MAX_BYTES", 200)
    monkeypatch.setattr(ddl, "_TRACE_BACKUPS", 2)
    log = DurableDecisionLog()
    # Each record is well over 200B once padded, so every write rotates.
    for i in range(6):
        log.record("classifier", {"model": "m", "pad": "x" * 300}, task_preview=f"t{i}")
    base = routes_path()
    # Current file exists; backups bounded to .1/.2; .3 must never appear.
    assert base.exists()
    assert base.with_suffix(base.suffix + ".1").exists()
    assert base.with_suffix(base.suffix + ".2").exists()
    assert not base.with_suffix(base.suffix + ".3").exists()


def test_persist_swallows_oserror(state_home, monkeypatch):
    log = DurableDecisionLog()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(ddl, "open", boom, raising=False)
    # Must NOT raise into routing.
    log.record("classifier", {"model": "m"})
    # In-memory record still succeeded.
    assert log.entries()[0]["output"]["model"] == "m"


def test_persist_skips_non_serializable_entry(state_home):
    log = DurableDecisionLog()

    class Unserializable:
        pass

    # A non-JSON output value is skipped on disk, not raised.
    log.record("classifier", {"obj": Unserializable()})
    # No line written (json.dumps failed before the file open).
    assert not routes_path().exists() or routes_path().read_text() == ""


def test_rotate_tolerates_missing_backups(state_home, monkeypatch):
    monkeypatch.setattr(ddl, "_TRACE_MAX_BYTES", 50)
    monkeypatch.setattr(ddl, "_TRACE_BACKUPS", 3)
    log = DurableDecisionLog()
    # First write creates the file; second (over cap) rotates with NO existing
    # backups — exercises the unlink-absent + skip-missing-src branches.
    log.record("classifier", {"model": "a" * 100})
    log.record("classifier", {"model": "b" * 100})
    assert routes_path().with_suffix(routes_path().suffix + ".1").exists()
