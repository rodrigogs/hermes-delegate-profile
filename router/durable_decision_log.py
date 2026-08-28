"""Durable decision log — persists routing traces for visual replay.

A :class:`DecisionLog` subclass that, in addition to the in-memory list, appends
each recorded entry as one JSON line to ``<state>/routes.jsonl`` (the same state
dir the breaker uses). This is the single writer: the delegate_profile plugin
process records; the sidecar process only reads the file back (the JSONL file is
the IPC between the two).

Safety properties (every one load-bearing):
  * Stdlib-only — a bad import here must never brick the plugin, so there are no
    third-party deps to fail.
  * Never raises into routing — all IO is wrapped; a full/slow disk degrades to
    "no trace recorded", never a routing failure.
  * In-process lock — delegate_profile can be invoked concurrently in one
    process, and a trace entry with a classifier payload can exceed PIPE_BUF
    (4096B), so O_APPEND atomicity is not enough; the lock serializes the
    append + size-check + rotation critical section.
  * Bounded on disk — at most ``(_TRACE_BACKUPS + 1) * _TRACE_MAX_BYTES``: on
    rotation the backups cascade (.1→.2…) and the oldest is unlinked. The line
    about to be written is part of the size test, so a file rotates BEFORE it
    crosses the cap rather than after it already has (see
    :meth:`DurableDecisionLog._persist`). Rotating afterwards let every file
    overshoot by one whole entry, and this bullet was wrong by that much: measured
    1252 bytes on disk against the 600 it promised, on a 200-byte cap. The single
    exception is an entry LARGER than the whole cap: a JSON line cannot be split
    without corrupting it, so such an entry lands whole in a file of its own.
  * Bounded per entry — the chain plan's ``rejected`` list is truncated by
    :func:`decision_log.bound_chain_plan` before it ever reaches the disk.
  * Forward/backward readable — :func:`read_entries` skips corrupt lines (decoded
    per line, so a torn multi-byte write from the other process costs that line
    and not the whole file) and :func:`decision_log.chain_plan_of` gives OLD
    entries (written before the chain-plan feature) an empty default instead of a
    KeyError.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .decision_log import DecisionLog, attempts_of, chain_plan_of

logger = logging.getLogger(__name__)

# Bound: keep the current file plus this many rotated backups. Total disk is
# capped at (_TRACE_BACKUPS + 1) * _TRACE_MAX_BYTES, because the size test counts
# the line about to be written; an entry bigger than the cap on its own is the one
# documented exception (see the module docstring).
_TRACE_MAX_BYTES = 5 * 1024 * 1024   # 5 MiB per file
_TRACE_BACKUPS = 3                   # routes.jsonl.1 .. .3  → ~20 MiB ceiling

# One lock per process guards the append+rotate critical section across threads.
_WRITE_LOCK = threading.Lock()


def routes_path() -> Path:
    """Absolute path of the durable route-trace log — the single source of truth
    shared by the writer (the delegate_profile plugin, running per-profile) and
    the reader (the sidecar, running under one fixed profile).

    CRITICAL: this must resolve identically in BOTH processes or replay silently
    shows nothing. The plugin runs with a PROFILE-SCOPED ``HERMES_HOME``
    (``~/.hermes/profiles/<profile>``) that varies per delegation, while the
    sidecar is pinned to one profile — so a profile-scoped path would diverge.
    We therefore anchor the trace at a PROFILE-INDEPENDENT location:
      1. ``HERMES_ROUTE_TRACE_FILE`` if set (explicit override for both units);
      2. else ``<hermes-root>/hermes-smart-router/state/routes.jsonl`` where
         hermes-root is HERMES_HOME with any trailing ``profiles/<name>`` peeled
         off, so every profile and the sidecar converge on one file.
    """
    explicit = os.environ.get("HERMES_ROUTE_TRACE_FILE")
    if explicit:
        return Path(explicit)
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    # Peel a trailing ``profiles/<name>`` so a profile-scoped HERMES_HOME and the
    # bare root resolve to the same canonical trace file.
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home / "hermes-smart-router" / "state" / "routes.jsonl"


def attempts_path() -> Path:
    """Executor journal beside the route trace; one state authority per run."""
    return routes_path().with_name("attempts.jsonl")


def merge_attempts(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return copies of decision entries joined with executor outcomes.

    Old decisions remain key-absent. A journal row must match both ``task_id``
    and the dispatch run where that id is available: task ids are reused when a
    card is re-run, and attaching a prior retry's failure to today's decision
    is a more dangerous lie than showing it as not instrumented.
    """
    try:
        raw = attempts_path().read_bytes().splitlines()
    except OSError:
        return [dict(entry) for entry in entries]
    by_key: Dict[tuple[str, object], List[Dict[str, Any]]] = {}
    for line in raw:
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("schema") != "route-attempts/1":
            continue
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        by_key.setdefault((task_id, row.get("run_id")), []).append(row)
    merged: List[Dict[str, Any]] = []
    for entry in entries:
        copy = dict(entry)
        task_id = copy.get("task_id")
        run_id = copy.get("run_id")
        if isinstance(task_id, str) and task_id:
            records = by_key.get((task_id, run_id), [])
            if records:
                copy["attempts"] = records
                # Normalize at the boundary, not in every surface.
                copy["attempts"] = attempts_of(copy) or []
        merged.append(copy)
    return merged


# ---------------------------------------------------------------------------
# Readers — fail-safe, tolerant of every historical entry shape
# ---------------------------------------------------------------------------

def trace_files() -> List[Path]:
    """Current trace file plus its rotated backups, newest file first.

    Reading the backups too keeps a replay non-empty right after a rotation,
    when the current file is still fresh.
    """
    base = routes_path()
    files = [base]
    for n in range(1, _TRACE_BACKUPS + 1):
        files.append(base.with_suffix(base.suffix + f".{n}"))
    return files


def read_entries(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Parsed trace entries oldest→newest across the rotated files.

    Defensive by construction — this runs in the console/CLI read path against a
    file another process is appending to:
      * missing/unreadable file -> that file contributes nothing;
      * truncated or corrupt JSON line -> that line is skipped;
      * a line whose BYTES are not valid UTF-8 -> that line is skipped;
      * a line that is valid JSON but not a mapping -> skipped;
      * entries WITHOUT ``chain_plan`` (everything written before that feature)
        are returned untouched — use :func:`decision_log.chain_plan_of` for the
        empty default rather than indexing the key.
    Never raises.

    DECODED PER LINE, for the reason ``service._read_trace_entries`` documents at
    length: the writer appends from another process, so a torn multi-byte write is
    a thing that happens, and a whole-file ``read_text`` raises
    ``UnicodeDecodeError`` for the entire file over one bad byte. Caught here that
    did not raise — it silently made the file contribute NOTHING, so a single torn
    byte discarded up to 5 MiB of traces including every entry written after the
    damage, while the service's reader (same file, same purpose) served them all.
    Two readers of one file disagreeing about what is readable is the defect this
    module works hardest to avoid; the per-line decode is what makes them agree.

    A ``limit`` that cannot mean "the last N" — 0, negative, or unparseable — is
    the unlimited read rather than an error. The ``n > 0`` guard is load-bearing:
    ``collected[-0:]`` is the whole list (so the guard is not what makes 0 work)
    and ``collected[-(-2):]`` would silently drop the two OLDEST entries, which is
    a wrong answer where returning everything is a defensible one.
    """
    collected: List[Dict[str, Any]] = []
    for path in trace_files():
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        file_entries: List[Dict[str, Any]] = []
        for raw_line in raw.splitlines():
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue  # torn/foreign bytes — this line only
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                file_entries.append(obj)
        # Older files come first so the combined list stays oldest→newest.
        collected = file_entries + collected
    collected = merge_attempts(collected)
    if limit is not None:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            return collected
        if n > 0:
            return collected[-n:]
    return collected


def read_chain_plans(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """``read_entries`` narrowed to chain plans, one per entry, never raising.

    Old entries yield the empty default, so the list is always the same length
    as the entry list and callers need no presence check.
    """
    return [chain_plan_of(entry) for entry in read_entries(limit)]


class DurableDecisionLog(DecisionLog):
    """A DecisionLog that also appends each entry to ``routes.jsonl``."""

    def record(
        self,
        cause: str,
        output: Dict[str, Any],
        matched_rule_id: Optional[str] = None,
        task_preview: str = "",
        *,
        steps: Optional[List[Dict[str, Any]]] = None,
        chain_plan: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record in memory and persist one JSON line.

        ``chain_plan`` rides along unchanged (the base class bounds it), so a
        persisted trace can be replayed with the capability filter's verdict —
        eligible order, rejected+reason, strategy — intact.
        """
        super().record(
            cause, output, matched_rule_id, task_preview,
            steps=steps, chain_plan=chain_plan,
        )
        # The entry we just appended in-memory is the one to persist.
        try:
            entry = self._entries[-1]
        except IndexError:  # pragma: no cover - super always appends
            return
        self._persist(entry)

    @staticmethod
    def _persist(entry: Dict[str, Any]) -> None:
        """Append one JSON line, rotating first if it would cross the cap.

        Fully guarded: any OSError (full disk, permissions, a directory where the
        file should be, races) is logged and swallowed so routing is never
        affected.
        """
        path = routes_path()
        try:
            line = json.dumps(entry, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as exc:  # non-serializable payload
            logger.warning("route trace not serializable, skipped: %s", exc)
            return
        pending = len(line.encode("utf-8"))  # bytes, like st_size
        with _WRITE_LOCK:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0  # absent, or unstattable: there is nothing to rotate
                # THE INCOMING LINE COUNTS, which is the ``st_size >= cap`` test
                # this used to run and the reason the advertised ceiling was wrong.
                # Rotating only once the file was ALREADY at the cap let every one
                # of the (_TRACE_BACKUPS + 1) files overshoot by a whole entry:
                # measured 1252 bytes on disk against the 600 the module
                # advertises, on a 200-byte cap. Asking "would this record cross
                # the cap?" is both what "size-bounded" means and what
                # ``logging.handlers.RotatingFileHandler`` asks.
                #
                # ``size and`` is load-bearing: an EMPTY or absent file must never
                # rotate. ``_rotate`` ends in ``os.replace(path, .1)``, which is an
                # OSError for a path that does not exist yet — swallowed by the
                # guard below, taking the entry with it — so a first entry larger
                # than the whole cap would be dropped on a fresh install instead of
                # landing whole in a file of its own.
                if size and size + pending > _TRACE_MAX_BYTES:
                    DurableDecisionLog._rotate(path)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError as exc:
                logger.warning("could not persist route trace: %s", exc)

    @staticmethod
    def _rotate(path: Path) -> None:
        """Cascade routes.jsonl → .1 → .2 … and unlink the oldest.

        Called under ``_WRITE_LOCK``. Bounds total disk at
        ``(_TRACE_BACKUPS + 1) * _TRACE_MAX_BYTES``.
        """
        # Drop the oldest backup so the cascade below cannot grow unbounded.
        oldest = path.with_suffix(path.suffix + f".{_TRACE_BACKUPS}")
        try:
            oldest.unlink()
        except OSError:
            pass  # absent or unremovable — the cascade overwrites it anyway
        # Shift .N-1 → .N down to .1 → .2.
        for n in range(_TRACE_BACKUPS - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{n}")
            dst = path.with_suffix(path.suffix + f".{n + 1}")
            if src.exists():
                os.replace(src, dst)
        # Current file becomes .1, leaving a fresh current file to be created.
        os.replace(path, path.with_suffix(path.suffix + ".1"))
