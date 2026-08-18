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


def test_routes_path_is_profile_independent(monkeypatch, tmp_path):
    """Writer (profile-scoped HERMES_HOME) and reader (another profile) MUST
    resolve the SAME trace file, else replay silently shows nothing."""
    root = tmp_path
    canonical = root / "delegate-profile" / "state" / "routes.jsonl"
    monkeypatch.delenv("HERMES_ROUTE_TRACE_FILE", raising=False)
    # Writer runs under profiles/coder ...
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "coder"))
    assert routes_path() == canonical
    # ... reader runs under profiles/rodrigo — same canonical file.
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "rodrigo"))
    assert routes_path() == canonical
    # Bare root (no profile) also converges.
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert routes_path() == canonical


def test_routes_path_honors_explicit_override(monkeypatch, tmp_path):
    override = tmp_path / "custom" / "traces.jsonl"
    monkeypatch.setenv("HERMES_ROUTE_TRACE_FILE", str(override))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "x"))
    assert routes_path() == override


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


def test_the_disk_ceiling_the_module_advertises_is_the_one_it_keeps(
    state_home, monkeypatch,
):
    """``(_TRACE_BACKUPS + 1) * _TRACE_MAX_BYTES``, measured in bytes on real files.

    The bound is a stated safety property of this module, not a rule of thumb: the
    trace shares a disk with the breaker state, the profiles and whatever the agent
    is working on. It was not being kept. Rotating only once the current file was
    ALREADY at the cap let every one of the (backups + 1) files overshoot by a
    whole entry — measured here at 1252 bytes against an advertised 600 on a
    200-byte cap, i.e. the ceiling was wrong by (backups + 1) entries and would
    have grown with the entry size. Counting the incoming line in the size test is
    what makes the sentence in the docstring true, and this asserts the sentence
    rather than the file count: ``.3 does not exist`` was all the rotation tests
    checked, and file names are not bytes.
    """
    monkeypatch.setattr(ddl, "_TRACE_MAX_BYTES", 1024)
    monkeypatch.setattr(ddl, "_TRACE_BACKUPS", 2)
    log = DurableDecisionLog()
    for i in range(40):
        log.record("classifier", {"model": "m", "pad": "x" * 200}, task_preview=f"t{i}")

    base = routes_path()
    sizes = {}
    for n in range(0, ddl._TRACE_BACKUPS + 2):
        path = base if n == 0 else base.with_suffix(base.suffix + f".{n}")
        if path.exists():
            sizes[path.name] = path.stat().st_size

    ceiling = (ddl._TRACE_BACKUPS + 1) * ddl._TRACE_MAX_BYTES
    assert sum(sizes.values()) <= ceiling, sizes
    assert max(sizes.values()) <= ddl._TRACE_MAX_BYTES, sizes
    # Non-vacuity: 40 records really did rotate, really filled every file the cap
    # allows, and each ROTATED file really is pressing against the cap — a writer
    # that persisted nothing would satisfy an upper bound perfectly. The current
    # file is excluded because it is mid-fill by definition.
    assert len(sizes) == ddl._TRACE_BACKUPS + 1, sizes
    rotated = [size for name, size in sizes.items() if name != base.name]
    assert min(rotated) > ddl._TRACE_MAX_BYTES // 2, sizes
    # Bounded means the OLDEST decisions age out, and nothing else: what survives
    # is the newest contiguous run, in order, through the reader an operator uses.
    tasks = [entry["task"] for entry in ddl.read_entries()]
    assert tasks == [f"t{i}" for i in range(40 - len(tasks), 40)]


def test_an_entry_bigger_than_the_whole_cap_is_persisted_whole_not_dropped(
    state_home, monkeypatch,
):
    """The one documented exception to the ceiling — and the trap underneath it.

    A JSON line cannot be split without corrupting it (a torn line is a line both
    readers skip), so an entry larger than the cap goes to disk whole, in a file of
    its own. Which means the size test must not rotate an EMPTY file: ``_rotate``
    ends in ``os.replace(path, .1)``, an OSError for a path that does not exist
    yet, and ``_persist`` swallows OSError — so "rotate whenever the line would
    cross the cap" applied to a fresh install would log a warning and silently drop
    the first entry, which is the state every install starts in.
    """
    monkeypatch.setattr(ddl, "_TRACE_MAX_BYTES", 64)
    log = DurableDecisionLog()
    log.record("classifier", {"model": "m", "pad": "x" * 500}, task_preview="huge")

    assert routes_path().stat().st_size > ddl._TRACE_MAX_BYTES, "not the oversized case"
    entries = ddl.read_entries()
    assert [entry["task"] for entry in entries] == ["huge"]
    assert entries[0]["output"]["pad"] == "x" * 500, "persisted truncated, not whole"
    # ...and the next entry rotates that file out of the way instead of growing it.
    log.record("classifier", {"model": "m"}, task_preview="next")
    assert routes_path().read_text(encoding="utf-8").count("\n") == 1
    assert [entry["task"] for entry in ddl.read_entries()] == ["huge", "next"]


def test_persist_swallows_a_real_oserror_and_routing_carries_on(monkeypatch, tmp_path):
    """Two unwritable trace paths a box really produces, no patched builtins.

    The guard exists for a full disk, for permissions, and for the state dir
    someone reorganised by hand — so it is provoked with the filesystem rather
    than by replacing ``open``. Patching the raise would prove the ``except``
    clause is spelled ``OSError`` and nothing else; these two prove it catches
    what an operator can actually create, on the real code path, with the real
    ``open``. A trace is best-effort by design: routing must not notice, the
    in-memory log must still hold the decision, and both readers must agree that
    the file has nothing in it rather than raising in the console.
    """
    from router.service import RouterService

    directory_where_the_file_goes = tmp_path / "as-a-dir" / "routes.jsonl"
    directory_where_the_file_goes.mkdir(parents=True)
    file_where_the_dir_goes = tmp_path / "as-a-file"
    file_where_the_dir_goes.write_text("not a directory", encoding="utf-8")

    for unwritable in (directory_where_the_file_goes,
                       file_where_the_dir_goes / "state" / "routes.jsonl"):
        monkeypatch.setenv("HERMES_ROUTE_TRACE_FILE", str(unwritable))
        log = DurableDecisionLog()
        log.record("classifier", {"model": "m"}, task_preview="unwritable")

        assert log.entries()[0]["output"]["model"] == "m", unwritable
        assert ddl.read_entries() == [], unwritable
        assert ddl.read_entries() == RouterService(
            tmp_path / "router.yaml"
        )._read_trace_entries(), unwritable


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


# ---------------------------------------------------------------------------
# ONE FILE, TWO READERS — and they have to say the same thing
# ---------------------------------------------------------------------------
#
# routes.jsonl has two readers in this tree: ``read_entries`` here, and
# ``RouterService._read_trace_entries``, which serves the Decisions surface. They
# read the same bytes for the same purpose, so every test below asserts the two
# AGREE rather than checking either alone — asserting one side is how they came to
# differ. Measured before the fix: on a trace carrying one torn multi-byte write
# (the writer appends from another process, so that is a thing that happens, not a
# hypothesis) the service served every readable entry while THIS reader returned
# an empty list — whole-file ``read_text`` raises UnicodeDecodeError for the entire
# file over one bad byte, and catching it here silently dropped up to 5 MiB of
# traces including everything written after the damage.


def _damaged_trace(path):
    """Write a trace holding one of every corruption a live file really carries.

    Returns the tasks of the entries a reader must still serve. The good entry
    AFTER the damage is the load-bearing one: it is what an operator opening the
    tab following an incident came for.
    """
    def line(**fields):
        return json.dumps(fields).encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n".join([
        line(ts=1.0, cause="hard_rule", task="before", output={"model": "m1"}),
        b"",                                    # blank line
        b"   ",                                 # whitespace only
        b"{ this is not json",                  # truncated/corrupt JSON
        json.dumps("a-string-not-a-dict").encode("utf-8"),   # JSON, not a mapping
        json.dumps([1, 2, 3]).encode("utf-8"),               # JSON, not a mapping
        b'{"ts": 2.0, "cause": "classifier", "task": "\xff\xfe torn"}',  # torn bytes
        line(ts=3.0, cause="classifier", task="after", output={"model": "m3"}),
    ]) + b"\n")
    return ["before", "after"]


def test_both_readers_of_routes_jsonl_survive_every_corruption_a_live_file_has(
    state_home, tmp_path,
):
    """The two readers agree, line for line, on a file with every kind of junk."""
    from router.service import RouterService

    expected = _damaged_trace(routes_path())
    service = RouterService(tmp_path / "router.yaml")  # never read: a file reader

    assert [e["task"] for e in ddl.read_entries()] == expected
    assert ddl.read_entries() == service._read_trace_entries()
    # Non-vacuity: two empty lists would also "agree".
    assert len(expected) == 2


def test_both_readers_name_the_same_files(state_home, tmp_path, monkeypatch):
    """The file SET is a pair too — the service reads the durable log's bound.

    ``_trace_files`` is a second walk of the same cascade, so it is asked for the
    backup count rather than holding its own: a service that kept a copy of
    ``_TRACE_BACKUPS`` would stop reading a file the writer still rotates into,
    and the missing traces would look like traces that were never written.
    """
    from router.service import RouterService

    assert RouterService._trace_files() == ddl.trace_files()
    monkeypatch.setattr(ddl, "_TRACE_BACKUPS", 1)
    assert RouterService._trace_files() == ddl.trace_files()
    assert len(RouterService._trace_files()) == 2  # current + .1, and nothing else


def test_a_rotated_trace_reads_back_whole_and_in_order_through_both_readers(
    state_home, monkeypatch,
):
    """Rotation must not reorder or lose a decision an operator can still see.

    Driven through real records against a real cap rather than by planting files:
    rotation is the path where the writer and the readers have to agree about
    which file holds the OLDEST entry, and ``os.replace`` cascades make that easy
    to get backwards.

    The cap is DERIVED from a measured entry rather than written as a number, and
    says what it means: two entries per file, never three. Six records then fill
    three of the four files, so every one is still readable and a missing entry
    means rotation lost it. A literal cap encodes an assumption about entry size
    AND about when the writer rotates — this test held a literal 400, and when the
    writer started counting the incoming line (which is what makes its advertised
    ceiling true) that quietly became one entry per file, so two of the six aged
    out of the cascade and the test failed for a reason that was not the property.

    2.5x rather than exactly 2x because entries are not all the same size: ``ts``
    is a float, and its repr is a byte or two longer some seconds than others. At
    exactly twice the first entry a later entry one byte longer rotates after ONE
    entry instead of two, which is a test that fails on the clock — measured here
    before the slack went in.
    """
    from router.service import RouterService

    monkeypatch.setattr(ddl, "_TRACE_BACKUPS", 3)
    log = DurableDecisionLog()
    log.record("classifier", {"model": "m", "pad": "x" * 200}, task_preview="t0")
    one_entry = routes_path().stat().st_size
    monkeypatch.setattr(ddl, "_TRACE_MAX_BYTES", one_entry * 5 // 2)
    for i in range(1, 6):
        log.record("classifier", {"model": "m", "pad": "x" * 200}, task_preview=f"t{i}")

    base = routes_path()
    assert base.with_suffix(base.suffix + ".1").exists(), "no rotation, nothing proven"

    entries = ddl.read_entries()
    assert [e["task"] for e in entries] == [f"t{i}" for i in range(6)]  # oldest→newest
    assert entries == RouterService(base.parent / "router.yaml")._read_trace_entries()


def test_read_entries_limit_takes_the_newest_and_degrades_on_a_nonsense_limit(
    state_home,
):
    """``limit`` is the last N, and an unusable limit reads everything.

    The degrade matters more than the happy path: a negative limit through
    ``collected[-n:]`` would silently drop the OLDEST entries and look like a
    successful read, so anything that cannot mean "the last N" has to fall out of
    that slice entirely.
    """
    log = DurableDecisionLog()
    for i in range(5):
        log.record("classifier", {"model": f"m{i}"}, task_preview=f"t{i}")

    assert [e["task"] for e in ddl.read_entries(2)] == ["t3", "t4"]
    assert [e["task"] for e in ddl.read_entries("3")] == ["t2", "t3", "t4"]
    everything = [f"t{i}" for i in range(5)]
    for unusable in (None, 0, -2, "oops", object()):
        assert [e["task"] for e in ddl.read_entries(unusable)] == everything, unusable


def test_read_chain_plans_is_one_plan_per_entry_and_agrees_with_the_accessor(
    state_home,
):
    """One plan per entry, whatever the entry's vintage — no presence check needed.

    The plans are asserted against ``chain_plan_of`` on the same entries rather
    than against literals: this function exists to save a caller the presence
    check, not to have its own opinion about what a plan is.
    """
    from router.decision_log import chain_plan_of

    log = DurableDecisionLog()
    log.record("hard_rule", {"model": "glm-5.3"}, task_preview="planless")
    log.record("keyword_match", {"model": "glm-5.3"}, task_preview="planned",
               chain_plan={"chain": [{"model": "gpt-5.6-luna", "provider": "openai"}],
                           "rejected": [], "strategy": "cheapest_now"})

    entries = ddl.read_entries()
    plans = ddl.read_chain_plans()
    assert len(plans) == len(entries) == 2
    assert plans == [chain_plan_of(entry) for entry in entries]
    # The old-entry default is a real plan shape, not a missing key...
    assert plans[0]["chain"] == [] and plans[0]["strategy"] == "sequential"
    # ...and the persisted one round-trips with its strategy and head intact.
    assert plans[1]["chain"][0]["model"] == "gpt-5.6-luna"
    assert plans[1]["strategy"] == "cheapest_now"
    assert [p["chain"] for p in ddl.read_chain_plans(1)] == [plans[1]["chain"]]
