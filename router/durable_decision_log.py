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
    rotation the backups cascade (.1→.2…) and the oldest is unlinked.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .decision_log import DecisionLog

logger = logging.getLogger(__name__)

# Bound: keep the current file plus this many rotated backups. Total disk is
# provably capped at (_TRACE_BACKUPS + 1) * _TRACE_MAX_BYTES.
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
      2. else ``<hermes-root>/delegate-profile/state/routes.jsonl`` where
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
    return home / "delegate-profile" / "state" / "routes.jsonl"


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
    ) -> None:
        super().record(
            cause, output, matched_rule_id, task_preview, steps=steps,
        )
        # The entry we just appended in-memory is the one to persist.
        try:
            entry = self._entries[-1]
        except IndexError:  # pragma: no cover - super always appends
            return
        self._persist(entry)

    @staticmethod
    def _persist(entry: Dict[str, Any]) -> None:
        """Append one JSON line, rotating first if the file is at the cap.

        Fully guarded: any OSError (full disk, permissions, races) is logged and
        swallowed so routing is never affected.
        """
        path = routes_path()
        try:
            line = json.dumps(entry, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as exc:  # non-serializable payload
            logger.warning("route trace not serializable, skipped: %s", exc)
            return
        with _WRITE_LOCK:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size >= _TRACE_MAX_BYTES:
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
