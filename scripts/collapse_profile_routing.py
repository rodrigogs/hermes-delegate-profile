#!/usr/bin/env python3
"""Collapse per-profile routing overrides back onto the root Hermes config.

The routing order currently exists in 18 places: the global config,
``router.yaml``'s ``blocklist.fallback_chain``, and 15 per-profile
``config.yaml`` copies that each redeclare ``model``, ``fallback_providers`` and
``auxiliary.vision``. A single canonical chain is unusable while 15 copies
shadow it, so this script deletes those three key paths from every profile and
lets them resolve to the root.

Profiles DO inherit an omitted key from the root — proven in production by
``trama-engineer``, which omits ``reasoning_effort`` and inherits the root's
``"max"``. Removing a key is therefore a de-shadowing operation, not a deletion
of behaviour.

Contract
--------
* Dry-run is the DEFAULT; writing requires an explicit ``--apply``.
* Only the three key paths above are ever removed. Every other key
  (``max_turns``, ``platform_toolsets``, ``plugins``, role-guard settings,
  ``mcp_servers``, ``terminal``, ``delegation``, ``kanban``, ``agent.*``,
  ``onboarding``, ``tool_loop_guardrails``, anything else present) is preserved.
* One exception, and it is deliberate: if removing ``auxiliary.vision`` leaves
  ``auxiliary`` an empty mapping, the now-empty ``auxiliary`` key is pruned too.
  An empty mapping is not the same as an absent key — it would shadow the root's
  whole ``auxiliary`` block (compression included) instead of inheriting it.
  The emptiness is *checked*, never assumed: an ``auxiliary`` that still holds
  ``compression`` (or anything else) survives.
* ``auxiliary.vision`` has two shapes in the wild. As a scalar (``vision:
  glm-4.6v``) the whole key is the routing declaration and the whole key goes.
  As a MAPPING it can also carry non-routing settings, so only the routing keys
  inside it (``_NESTED_TARGET_KEYS``) are removed and every sibling setting is
  left alone; the ``vision`` key itself is pruned only if nothing is left in it.
* Every file that will change is copied to
  ``<hermes-home>/backups/collapse-profile-routing-<stamp>/<relative path>``
  BEFORE any write. The stamp comes from ``--stamp``, never from the clock, so a
  run is reproducible and testable.
* Fail-closed: if any target fails to parse (or is not a mapping), nothing is
  written at all and the exit status is non-zero. Parsing of every target
  happens before the first byte is written.
* Re-parsed AFTER the write, all of them, because that is the second half of the
  constraint the operator's runbook records from the incident that corrupted all
  16 configs at once: *"Only ever edit with Python + PyYAML, then re-parse all
  16."* A rewritten file must also re-parse EQUAL to the document that was
  planned, which is what catches a write that succeeded and landed the wrong
  bytes. A failure here is loud on both streams, names the file, points at the
  backup and exits non-zero — see :func:`verify_after_write`.
* Fail-closed on permissions too: under ``--apply`` every file the plan intends
  to rewrite is checked for WRITE access (the file itself and its directory,
  since the atomic replace needs both) before the first byte is written. An
  unwritable target aborts the run with a diagnostic naming it, having written
  nothing — not even a backup. Targets the plan does not rewrite are not
  checked, so a read-only already-collapsed profile cannot block a no-op run.
* A partial result IS reachable when the filesystem fails between two writes
  (disk full, a revoked permission, a racing ``chmod``) — the pre-flight check
  narrows that window but cannot close it. So a mid-run write failure is caught,
  not raised: the remaining targets are left alone and the full report is still
  printed, naming every file that WAS written and every file that was NOT,
  together with the backup directory holding the originals. The exit status is
  non-zero, but the operator is never left guessing which files changed.
* Idempotent: a profile that is already collapsed produces a warning, not a
  failure, and a second ``--apply`` is a clean no-op that creates no backup.
* Exit status: ``0`` success (dry-run included), ``2`` usage / missing hermes
  home / parse failure (nothing written), ``3`` write refused up front, a write
  failed mid-run, or the post-write re-parse failed (the report says which files
  changed either way).

IO
--
Impure by design: reads ``<hermes-home>/profiles/*/config.yaml``, and under
``--apply`` writes those files (temp file + ``os.replace``, so a partial file is
never observable) plus the backup tree. It touches nothing else — no network, no
services, no ``router.yaml``.

Comment preservation
--------------------
NOT achievable with pyyaml alone, and no dependency is added for it: pyyaml
discards comments and reformats scalars/indentation on ``safe_dump``. Rewritten
profile files therefore lose their comments and their exact byte layout. The
script says so in its own output, and the timestamped backup is the recovery
path. Nothing else about the data changes: key order is preserved, no key is
added, and the file's PERMISSIONS are carried across the atomic replace (see
:func:`_atomic_write_bytes` — they were not, and 0664 came back as 0600).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

# ---------------------------------------------------------------------------
# Closed target set — extend ONLY by adding a path, never by widening the walk
# ---------------------------------------------------------------------------

# The three key paths that redeclare root routing. Ordered for stable reporting.
_TARGET_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("model",),
    ("fallback_providers",),
    ("auxiliary", "vision"),
)

# A parent that exists only to hold a removed target and is empty afterwards.
# Keyed by the target path whose removal can empty it.
_PRUNE_EMPTY_PARENTS: Dict[Tuple[str, ...], Tuple[str, ...]] = {
    ("auxiliary", "vision"): ("auxiliary",),
}

# When a target's value is itself a MAPPING it may hold non-routing settings
# alongside the routing declaration, so only these keys are removed from it and
# every sibling survives. Keyed by target path; closed set, like _TARGET_PATHS.
_NESTED_TARGET_KEYS: Dict[Tuple[str, ...], Tuple[str, ...]] = {
    ("auxiliary", "vision"): ("model", "provider", "fallback_providers"),
}

# Backup directory names are built from operator input; keep them to a safe,
# traversal-free alphabet rather than trusting the caller.
_STAMP_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_BACKUP_PREFIX = "collapse-profile-routing-"

_COMMENT_WARNING = (
    "note: pyyaml cannot preserve YAML comments — rewritten profile files lose "
    "their comments and exact formatting; recover them from the backup directory"
)


# ---------------------------------------------------------------------------
# Pure planning helpers
# ---------------------------------------------------------------------------

def _format_path(path: Sequence[str]) -> str:
    """Render a key path for operator output (``auxiliary.vision``)."""
    return ".".join(path)


def _holder_of(document: Dict[str, Any], path: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Return the mapping that holds ``path[-1]``, or None if it is not reachable.

    Absent keys and non-mapping intermediates are a no-op, never an error: an
    already-collapsed profile must stay a warning rather than a failure.
    """
    node: Any = document
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, dict) else None


def _remove_path(document: Dict[str, Any], path: Sequence[str]) -> List[str]:
    """Remove one routing target from ``document`` in place.

    Returns the key paths actually removed — empty when there was nothing to
    remove. A scalar target is removed whole; a MAPPING target loses only its
    declared routing keys (``_NESTED_TARGET_KEYS``) so that unrelated settings
    living beside them survive, and is itself dropped only once it is empty.
    """
    holder = _holder_of(document, path)
    if holder is None or path[-1] not in holder:
        return []
    value = holder[path[-1]]
    nested = _NESTED_TARGET_KEYS.get(tuple(path))
    if nested is not None and isinstance(value, dict):
        removed = [f"{_format_path(path)}.{key}" for key in nested if key in value]
        for key in nested:
            value.pop(key, None)
        # Check before pruning: a mapping still holding non-routing settings is
        # a legitimate override, not a leftover shell.
        if removed and not value:
            del holder[path[-1]]
            removed.append(f"{_format_path(path)} (now-empty mapping)")
        return removed
    del holder[path[-1]]
    return [_format_path(path)]


def collapse_document(document: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Return ``(collapsed copy, removed key paths)`` for one profile config.

    Pure: the input document is never mutated. Key order of everything that
    survives is preserved, because only deletions are performed.
    """
    result = _deep_copy_mapping(document)
    removed: List[str] = []
    for path in _TARGET_PATHS:
        removed_here = _remove_path(result, path)
        if not removed_here:
            continue
        removed.extend(removed_here)
        parent = _PRUNE_EMPTY_PARENTS.get(path)
        # An emptied parent would shadow the root's whole block instead of
        # inheriting it, so it is pruned as part of the same removal — but only
        # after confirming it really is empty.
        if parent is None:
            continue
        holder = _holder_of(result, parent)
        if holder is not None and holder.get(parent[-1]) == {}:
            del holder[parent[-1]]
            removed.append(f"{_format_path(parent)} (now-empty parent)")
    return result, removed


def _deep_copy_mapping(value: Any) -> Any:
    """Structural copy that keeps mapping insertion order."""
    if isinstance(value, dict):
        return {key: _deep_copy_mapping(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy_mapping(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Discovery + parsing (fail-closed, diagnostics instead of exceptions)
# ---------------------------------------------------------------------------

def discover_targets(hermes_home: Path) -> List[Path]:
    """Return every ``profiles/*/config.yaml`` under ``hermes_home``, sorted."""
    profiles_dir = hermes_home / "profiles"
    if not profiles_dir.is_dir():
        return []
    targets = [
        candidate / "config.yaml"
        for candidate in sorted(profiles_dir.iterdir())
        if candidate.is_dir() and (candidate / "config.yaml").is_file()
    ]
    return targets


def _load_profile(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse one profile config. Returns ``(document, error)`` — never raises."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read {path}: {exc}"
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return None, f"could not parse {path}: {exc}"
    if document is None:
        document = {}
    if not isinstance(document, dict):
        return None, f"{path}: profile config root must be a mapping"
    return document, None


def plan(hermes_home: Path) -> Dict[str, Any]:
    """Parse every target and compute the removals. No IO beyond reads.

    Returns ``{"changes": [...], "unchanged": [...], "errors": [...]}`` where
    each change is ``{"path", "relative", "removed", "document"}``.
    """
    changes: List[Dict[str, Any]] = []
    unchanged: List[Path] = []
    errors: List[str] = []

    for target in discover_targets(hermes_home):
        document, error = _load_profile(target)
        if error is not None:
            errors.append(error)
            continue
        assert document is not None  # error is None => document parsed
        collapsed, removed = collapse_document(document)
        if removed:
            changes.append(
                {
                    "path": target,
                    "relative": target.relative_to(hermes_home),
                    "removed": removed,
                    "document": collapsed,
                }
            )
        else:
            unchanged.append(target)

    return {"changes": changes, "unchanged": unchanged, "errors": errors}


# ---------------------------------------------------------------------------
# Write path (backups first, then atomic per-file replace)
# ---------------------------------------------------------------------------

def backup_root(hermes_home: Path, stamp: str) -> Path:
    """Return the stamped backup directory for this run."""
    return hermes_home / "backups" / f"{_BACKUP_PREFIX}{stamp}"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

    Mirrors ``RouterService._atomic_write_bytes``: a half-written profile config
    can never be observed, because the rename is atomic.

    THE MODE IS CARRIED OVER, and it has to be: ``mkstemp`` creates at 0600 and
    ``os.replace`` keeps the TEMP file's mode, so without this every rewritten
    profile config silently lost its permissions (measured: 0664 in, 0600 out).
    That is a data change the script's contract does not license — it claims only
    comments and byte layout are lost — and it is invisible until something else
    that reads these files as another user stops working. The chmod happens on the
    temp file, BEFORE the rename, so the config is never briefly readable by fewer
    principals than it was under its real name.

    A path that does not exist yet has no mode to preserve, so mkstemp's 0600
    stands; this script only ever rewrites files it already parsed, so that is the
    untaken branch rather than the normal case.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        original_mode: Optional[int] = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        original_mode = None
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="collapse-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if original_mode is not None:
            os.chmod(tmp_path, original_mode)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_backups(hermes_home: Path, stamp: str, changes: List[Dict[str, Any]]) -> Path:
    """Copy every changing target under the stamped backup dir, paths preserved."""
    root = backup_root(hermes_home, stamp)
    for change in changes:
        destination = root / change["relative"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(change["path"], destination)
    return root


def _write_access_error(path: Path) -> Optional[str]:
    """Return a diagnostic if ``path`` cannot be rewritten, else None.

    The atomic replace needs BOTH the file (it is being superseded) and its
    directory (the temp file is created and renamed there) to be writable, so
    both are checked and the diagnostic says which one failed.
    """
    directory = path.parent
    if not os.access(str(directory), os.W_OK | os.X_OK):
        return f"{path}: directory {directory} is not writable"
    if path.exists() and not os.access(str(path), os.W_OK):
        return f"{path}: file is not writable"
    return None


def write_access_errors(changes: List[Dict[str, Any]]) -> List[str]:
    """Pre-flight WRITE access for every target the plan intends to rewrite.

    Only planned rewrites are checked: an unwritable profile that the plan does
    not touch is none of this script's business, and checking it would let a
    read-only already-collapsed profile block an otherwise valid run.
    """
    errors: List[str] = []
    for change in changes:
        error = _write_access_error(change["path"])
        if error is not None:
            errors.append(error)
    return errors


def verify_after_write(
    hermes_home: Path,
    written: Dict[str, Dict[str, Any]],
) -> List[str]:
    """RE-PARSE every profile config on disk after a write. [] means clean.

    This is the operator runbook's constraint, quoted verbatim in the deploy doc:
    *"All 16 configs corrupted at once — a regex/sed edit across config.yaml +
    profiles/*/config.yaml. Restore from config-snapshot/. Only ever edit with
    Python + PyYAML, then re-parse all 16."* The script honoured the first half
    and not the second, while the deploy doc asserted it did both.

    EVERY discovered target is re-read, not only the rewritten ones — that is what
    "re-parse all 16" says, and a target this run did not touch failing to parse is
    news either way. For a file this run DID write the check is stronger than
    parseability: the re-parsed document must EQUAL the document that was planned,
    which is the only assertion that catches a write that succeeded and landed
    something other than what was intended. Comparing the plan to itself would
    prove nothing; comparing it to what came back off the disk is the agreement.

    Returns diagnostics, never raises: every string names the file, so an operator
    reading it knows which of the 16 to restore.
    """
    problems: List[str] = []
    for target in discover_targets(hermes_home):
        relative = str(target.relative_to(hermes_home))
        document, error = _load_profile(target)
        if error is not None:
            problems.append(f"{relative}: does NOT re-parse after the write: {error}")
            continue
        planned = written.get(relative)
        if planned is not None and document != planned:
            problems.append(
                f"{relative}: re-parsed, but does not match the planned document "
                f"— the file on disk is not what this run intended to write"
            )
    return problems


def collapse(
    hermes_home: Path,
    stamp: str,
    apply: bool = False,
) -> Dict[str, Any]:
    """Plan (and optionally perform) the collapse. Returns a report dict.

    Report keys: ``changes`` (relative path -> removed key paths), ``unchanged``
    (relative paths already collapsed), ``errors`` (parse failures),
    ``write_errors`` (pre-flight permission/backup failures — nothing was
    written), ``written`` and ``not_written`` (relative paths, so the operator is
    told exactly which files changed), ``failures`` (per-file mid-run write
    diagnostics), ``verify_errors`` (post-write re-parse diagnostics — see
    :func:`verify_after_write`), ``applied`` (True only when every planned write
    succeeded AND every config still re-parses) and ``backup_dir`` (``None`` when
    nothing was written).

    Never raises on an IO failure: every problem comes back as a diagnostic.
    """
    planned = plan(hermes_home)
    report: Dict[str, Any] = {
        "changes": [
            {"relative": str(change["relative"]), "removed": list(change["removed"])}
            for change in planned["changes"]
        ],
        "unchanged": [
            str(path.relative_to(hermes_home)) for path in planned["unchanged"]
        ],
        "errors": list(planned["errors"]),
        "write_errors": [],
        "written": [],
        "not_written": [],
        "failures": [],
        "verify_errors": [],
        "applied": False,
        "backup_dir": None,
    }
    if planned["errors"] or not apply:
        return report
    if not planned["changes"]:
        # Idempotent second run: no change, so no backup directory is created.
        report["applied"] = True
        return report

    # Pre-flight: an unwritable target aborts before the first byte is written,
    # backups included, so the tree is left exactly as it was found.
    access_errors = write_access_errors(planned["changes"])
    if access_errors:
        report["write_errors"] = access_errors
        report["not_written"] = [str(change["relative"]) for change in planned["changes"]]
        return report

    # Backups are complete before any write; if they cannot be taken, nothing is.
    try:
        root = _write_backups(hermes_home, stamp, planned["changes"])
    except OSError as exc:
        report["write_errors"] = [f"could not write the backup tree: {exc}; nothing written"]
        report["not_written"] = [str(change["relative"]) for change in planned["changes"]]
        return report
    report["backup_dir"] = str(root)

    # Relative path -> the document that write was supposed to land, so the
    # post-write re-parse can compare the disk against the intent rather than
    # merely against "is this still YAML".
    intended: Dict[str, Dict[str, Any]] = {}
    for index, change in enumerate(planned["changes"]):
        relative = str(change["relative"])
        payload = yaml.safe_dump(
            change["document"], sort_keys=False, default_flow_style=False, allow_unicode=True
        )
        try:
            _atomic_write_bytes(change["path"], payload.encode("utf-8"))
        except OSError as exc:
            # Stop at the first failure — the fewer files that diverge, the
            # smaller the recovery — and name the file that failed plus every
            # file left untouched behind it.
            report["failures"].append({"relative": relative, "error": str(exc)})
            report["not_written"] = [
                str(remaining["relative"]) for remaining in planned["changes"][index:]
            ]
            break
        report["written"].append(relative)
        intended[relative] = change["document"]

    # Re-parse ALL of them, and on the partial path too: a run that stopped
    # halfway is exactly when the operator most needs to know the tree is still
    # loadable. Verification cannot undo a write, so it does not gate one — it
    # gates the SUCCESS CLAIM, which is the thing the deploy doc was wrong about.
    report["verify_errors"] = verify_after_write(hermes_home, intended)
    report["applied"] = not report["failures"] and not report["verify_errors"]
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
        help="Hermes home containing profiles/ (default: ~/.hermes)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write; without this the run is a dry-run (the default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit no-op form of the default behaviour",
    )
    parser.add_argument(
        "--stamp",
        default=None,
        help="backup directory stamp; required with --apply, never taken from the clock",
    )
    return parser


def _print_report(report: Dict[str, Any], apply: bool) -> None:
    """Print the full report — including after a failure, per file, by name."""
    written = set(report["written"])
    failed = {failure["relative"]: failure["error"] for failure in report["failures"]}
    aborted = bool(report["write_errors"]) or bool(report["failures"])
    # A file the re-parse rejected must NOT read as a clean "removed ..." line
    # three lines above the diagnostic saying it is corrupt.
    unverified = {error.split(":", 1)[0] for error in report["verify_errors"]}
    for change in report["changes"]:
        relative = change["relative"]
        removed = ", ".join(change["removed"])
        if relative in unverified:
            print(f"{relative}: WRITTEN but does NOT re-parse — restore it")
        elif relative in written:
            print(f"{relative}: removed {removed}")
        elif relative in failed:
            print(f"{relative}: NOT WRITTEN (write failed: {failed[relative]})")
        elif aborted:
            print(f"{relative}: NOT WRITTEN (run aborted before this file was written)")
        else:
            print(f"{relative}: {'removed' if report['applied'] else 'would remove'} {removed}")
    for relative in report["unchanged"]:
        print(f"{relative}: already collapsed, nothing to remove (warning only)")
    if not report["changes"] and not report["unchanged"]:
        print("no profiles/*/config.yaml found — nothing to do")
    if report["backup_dir"]:
        print(f"backup: {report['backup_dir']}")
    # Nothing was rewritten when the run aborted up front, so the reformatting
    # caveat would be noise there.
    if report["changes"] and not report["write_errors"]:
        print(_COMMENT_WARNING)
    if report["write_errors"]:
        print(
            "collapse: aborted before the first write; "
            f"{len(report['not_written'])} file(s) NOT written, nothing changed:"
        )
        for error in report["write_errors"]:
            print(f"  - {error}")
    if report["failures"]:
        print(
            f"collapse: partial write — {len(report['written'])} file(s) written, "
            f"{len(report['not_written'])} NOT written; "
            f"recover the originals from {report['backup_dir']}"
        )
        for failure in report["failures"]:
            print(f"  - {failure['relative']}: {failure['error']}")
    if report["verify_errors"]:
        # The runbook's own instruction ("re-parse all 16") turned into the
        # sentence an operator can act on: which file, and where the original is.
        print(
            f"collapse: POST-WRITE RE-PARSE FAILED for "
            f"{len(report['verify_errors'])} file(s). The tree may be corrupt — "
            f"restore the file(s) named below from {report['backup_dir']} before "
            f"restarting anything:"
        )
        for error in report["verify_errors"]:
            print(f"  - {error}")
    elif apply and report["written"]:
        print(
            f"re-parsed all {len(report['unchanged']) + len(report['changes'])} "
            f"profile config(s) after the write; every one loads under PyYAML and "
            f"each rewritten file matches the document that was planned"
        )
    if not apply and report["changes"]:
        print("dry-run: nothing was written. Re-run with --apply --stamp <stamp> to write.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    apply = bool(args.apply)
    stamp = args.stamp or ""
    if apply:
        if not stamp:
            parser.error("--apply requires --stamp (the timestamp is an input, not the clock)")
        if not _STAMP_RE.match(stamp):
            parser.error("--stamp must match [A-Za-z0-9._-]+")

    hermes_home: Path = args.hermes_home.expanduser()
    if not hermes_home.is_dir():
        print(f"collapse: hermes home not found at {hermes_home}", file=sys.stderr)
        return 2

    report = collapse(hermes_home, stamp, apply=apply)
    if report["errors"]:
        print(
            f"collapse: {len(report['errors'])} target(s) failed to parse; nothing written:",
            file=sys.stderr,
        )
        for error in report["errors"]:
            print(f"  - {error}", file=sys.stderr)
        return 2

    # The report is printed even when a write failed: the operator has to be
    # told which files changed, and a raw traceback tells them nothing.
    _print_report(report, apply)
    if report["write_errors"]:
        for error in report["write_errors"]:
            print(f"collapse: {error}", file=sys.stderr)
        print("collapse: nothing was written.", file=sys.stderr)
        return 3
    if report["failures"]:
        for failure in report["failures"]:
            print(
                f"collapse: could not write {failure['relative']}: {failure['error']}",
                file=sys.stderr,
            )
        print(
            f"collapse: partial write — see the report above; originals are in "
            f"{report['backup_dir']}",
            file=sys.stderr,
        )
        return 3
    if report["verify_errors"]:
        # Loud on BOTH streams for the same reason the permission abort is: this
        # is the runbook's corruption incident happening again, and the operator
        # has to be told which file and that a backup exists before they restart.
        for error in report["verify_errors"]:
            print(f"collapse: {error}", file=sys.stderr)
        print(
            f"collapse: the post-write re-parse FAILED — files were written and at "
            f"least one no longer loads as expected. Restore the file(s) named "
            f"above from {report['backup_dir']} before restarting the gateway.",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
