#!/usr/bin/env python3
"""Transactional updater for Rodrigo's Hermes Agent / Hermes One stack.

The stock ``hermes update`` is intentionally not called here. It updates the
core checkout against ``origin/main`` and may switch/reset branches that carry
local patches. This controller instead merges the selected upstream ref into
the currently checked-out local branch for every component, preserves dirty
worktrees through a temporary stash, and restores an exact source/assets
snapshot on *any* failed stage.

Components
----------
* core:   /usr/local/lib/hermes-agent <- upstream/main
* plugin: ~/.hermes/plugins/delegate-profile <- origin/main
* one:    ~/hermes-webui <- origin/master

A snapshot contains Git bundles, binary tracked diffs, untracked files, the
Hermes One extension bundle, the Router unit, and the WebUI model cache. It is
stored under ``~/.hermes/update-backups/hermes-stack`` with mode 0700. Snapshot
metadata contains paths and Git SHAs only: never credentials or configuration
contents.

Usage
-----
    python3 scripts/update_hermes_stack.py status
    python3 scripts/update_hermes_stack.py plan
    python3 scripts/update_hermes_stack.py apply --yes
    python3 scripts/update_hermes_stack.py rollback <snapshot-id> --yes

``plan`` only fetches ref metadata. ``apply`` is the sole mutating update mode.
It acquires a per-user lock, creates a snapshot before every mutation, updates
all three components, reinstalls the Router bundle, validates source code,
restarts units in dependency order, and checks loopback health endpoints.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


SNAPSHOT_FORMAT = 1
SNAPSHOT_ROOT_NAME = "update-backups/hermes-stack"
LOCK_NAME = "update-locks/hermes-stack.lock"
# The manifest entries whose loss means a broken shell, by their post-rename IDs
# (see HERMES_EXTENSION_NAMING_MIGRATION.md). This gate raises UpdateError on any
# missing entry, so it has to track the renames: it still named hermes-panel after
# phase A renamed it to hermes-one-extension-kit, which meant a validation failure
# on a healthy manifest.
#
# Only the two extensions this install actually ships are required. capability-router
# and memory-graph carry sidecars that run on the old box and are deliberately absent
# from extensions.json here, so requiring them would fail the same way.
REQUIRED_EXTENSION_IDS = {
    "hermes-one-extension-kit",
    "hermes-one-office-3d",
}


class UpdateError(RuntimeError):
    """An expected operation failure which must trigger transactional rollback."""


@dataclass(frozen=True)
class Component:
    name: str
    path: Path
    remote: str
    branch: str

    @property
    def remote_ref(self) -> str:
        return f"{self.remote}/{self.branch}"


@dataclass(frozen=True)
class RuntimePaths:
    home: Path
    profile: str
    core: Path
    plugin: Path
    one: Path
    extensions: Path
    systemd_user: Path

    @property
    def profile_home(self) -> Path:
        return self.home / ".hermes" / "profiles" / self.profile

    @property
    def backup_root(self) -> Path:
        return self.home / ".hermes" / SNAPSHOT_ROOT_NAME

    @property
    def lock_path(self) -> Path:
        return self.home / ".hermes" / LOCK_NAME

    @property
    def router_unit(self) -> Path:
        return self.systemd_user / "hermes-router-sidecar.service"

    @property
    def model_cache(self) -> Path:
        return self.profile_home / "webui" / f"models_cache.{self.profile}.json"

    @property
    def installer(self) -> Path:
        return self.plugin / "scripts" / "install_hermes_one_router.py"

    @property
    def python(self) -> Path:
        candidate = self.core / "venv" / "bin" / "python3"
        return candidate if candidate.exists() else Path(sys.executable)


def default_paths(home: Path | None = None, profile: str = "rodrigo") -> RuntimePaths:
    home = (home or Path.home()).resolve()
    return RuntimePaths(
        home=home,
        profile=profile,
        core=Path(os.environ.get("HERMES_AGENT_DIR", "/usr/local/lib/hermes-agent")),
        plugin=Path(os.environ.get("HERMES_PLUGIN_DIR", home / ".hermes/plugins/delegate-profile")),
        one=Path(os.environ.get("HERMES_ONE_DIR", home / "hermes-webui")),
        extensions=Path(os.environ.get("HERMES_ONE_EXTENSIONS_DIR", home / "hermes-one-extensions")),
        systemd_user=Path(os.environ.get("HERMES_SYSTEMD_USER_DIR", home / ".config/systemd/user")),
    )


def default_components(paths: RuntimePaths) -> list[Component]:
    return [
        Component("core", paths.core, "upstream", "main"),
        Component("plugin", paths.plugin, "origin", "main"),
        Component("one", paths.one, "origin", "master"),
    ]


def _print(message: str) -> None:
    print(message, flush=True)


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    rendered = " ".join(command)
    result = subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise UpdateError(f"{rendered}: {detail[-1200:]}")
    return result


def _git(component: Component, *args: str, check: bool = True) -> str:
    result = _run(("git", "-C", str(component.path), *args), check=check)
    return result.stdout.strip()


def _git_bytes(component: Component, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(component.path), *args],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise UpdateError(result.stderr.decode("utf-8", "replace").strip() or "git command failed")
    return result.stdout


def _branch(component: Component) -> str:
    value = _git(component, "rev-parse", "--abbrev-ref", "HEAD")
    if value == "HEAD":
        raise UpdateError(f"{component.name}: detached HEAD is not a supported update target")
    return value


def _status(component: Component) -> str:
    return _git(component, "status", "--porcelain=v1", "--untracked-files=all")


def _assert_git_component(component: Component) -> None:
    if not (component.path / ".git").exists():
        raise UpdateError(f"{component.name}: not a Git checkout: {component.path}")
    _git(component, "rev-parse", "--is-inside-work-tree")
    remotes = set(_git(component, "remote").splitlines())
    if component.remote not in remotes:
        raise UpdateError(f"{component.name}: missing Git remote '{component.remote}'")
    _branch(component)


def _safe_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _timestamp_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _safe_mode(path, 0o600)


def _relative_archive_name(path: Path) -> str:
    if not path.is_absolute():
        raise UpdateError(f"backup path must be absolute: {path}")
    return str(path).lstrip("/")


def _archive_paths(archive: Path, paths: Iterable[Path]) -> list[str]:
    """Archive absolute support paths for extraction back at filesystem root."""
    added: list[str] = []
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for path in paths:
            if path.exists() or path.is_symlink():
                tar.add(path, arcname=_relative_archive_name(path), recursive=True)
                added.append(str(path))
    _safe_mode(archive, 0o600)
    return added


def _archive_repo_untracked(archive: Path, root: Path, relative_paths: Iterable[str]) -> None:
    """Archive untracked repository files relative to their worktree root."""
    root = root.resolve()
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for relative_path in relative_paths:
            candidate = (root / relative_path).resolve()
            if root not in candidate.parents and candidate != root:
                raise UpdateError(f"untracked path escapes repository root: {relative_path}")
            if candidate.exists() or candidate.is_symlink():
                tar.add(candidate, arcname=relative_path, recursive=True)
    _safe_mode(archive, 0o600)


def _extract_archive(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        destination_root = destination.resolve()
        for member in tar.getmembers():
            target = (destination_root / member.name).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise UpdateError(f"unsafe archive member during rollback: {member.name}")
        tar.extractall(destination)


def _component_metadata(component: Component) -> dict[str, Any]:
    _assert_git_component(component)
    untracked = _git_bytes(component, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_paths = [entry.decode("utf-8", "surrogateescape") for entry in untracked.split(b"\0") if entry]
    return {
        "path": str(component.path),
        "branch": _branch(component),
        "head": _git(component, "rev-parse", "HEAD"),
        "remote": component.remote,
        "remote_url": _git(component, "remote", "get-url", component.remote),
        "target_branch": component.branch,
        "status": _status(component),
        "untracked_paths": untracked_paths,
    }


def create_snapshot(paths: RuntimePaths, components: Sequence[Component]) -> Path:
    """Capture source and runtime assets before a mutating update operation."""
    paths.backup_root.mkdir(parents=True, exist_ok=True)
    _safe_mode(paths.backup_root, 0o700)
    snapshot = paths.backup_root / _timestamp_id()
    snapshot.mkdir(mode=0o700)
    repos_dir = snapshot / "repos"
    repos_dir.mkdir(mode=0o700)

    metadata: dict[str, Any] = {
        "format": SNAPSHOT_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "components": {},
        "support_paths": {},
    }

    for component in components:
        _print(f"snapshot: {component.name}")
        component_meta = _component_metadata(component)
        metadata["components"][component.name] = component_meta
        bundle = repos_dir / f"{component.name}.bundle"
        _run(("git", "-C", str(component.path), "bundle", "create", str(bundle), "--all"))
        _safe_mode(bundle, 0o600)

        patch = repos_dir / f"{component.name}.patch"
        patch.write_bytes(_git_bytes(component, "diff", "--binary", "HEAD"))
        _safe_mode(patch, 0o600)

        untracked_paths = list(component_meta["untracked_paths"])
        _archive_repo_untracked(
            repos_dir / f"{component.name}.untracked.tar.gz", component.path, untracked_paths
        )

    support = [paths.extensions, paths.router_unit, paths.model_cache]
    metadata["support_paths"] = {str(path): path.exists() or path.is_symlink() for path in support}
    _archive_paths(snapshot / "support.tar.gz", support)
    _write_json(snapshot / "metadata.json", metadata)
    _print(f"snapshot created: {snapshot}")
    return snapshot


def _load_metadata(snapshot: Path) -> dict[str, Any]:
    try:
        metadata = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"invalid snapshot {snapshot}: {exc}") from exc
    if metadata.get("format") != SNAPSHOT_FORMAT:
        raise UpdateError(f"unsupported snapshot format in {snapshot}")
    return metadata


def _remove_untracked(component: Component) -> None:
    _run(("git", "-C", str(component.path), "clean", "-fd"))


def _restore_component(component: Component, snapshot: Path, metadata: dict[str, Any]) -> None:
    details = metadata["components"].get(component.name)
    if not isinstance(details, dict):
        raise UpdateError(f"snapshot lacks component '{component.name}'")
    branch = str(details["branch"])
    head = str(details["head"])
    _print(f"rollback: restoring {component.name} to {head[:12]}")
    # A failed merge/stash apply may leave unmerged index entries. A hard reset
    # first clears that state before checking out the recorded branch.
    _run(("git", "-C", str(component.path), "reset", "--hard"))
    _run(("git", "-C", str(component.path), "checkout", "-f", branch))
    _run(("git", "-C", str(component.path), "reset", "--hard", head))
    _remove_untracked(component)

    patch = snapshot / "repos" / f"{component.name}.patch"
    if patch.stat().st_size:
        _run(("git", "-C", str(component.path), "apply", "--binary", str(patch)))
    untracked = snapshot / "repos" / f"{component.name}.untracked.tar.gz"
    if untracked.exists():
        _extract_archive(untracked, component.path)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _restore_support(paths: RuntimePaths, snapshot: Path, metadata: dict[str, Any]) -> None:
    support_paths = metadata.get("support_paths", {})
    for raw_path, existed in support_paths.items():
        if existed or Path(raw_path).exists() or Path(raw_path).is_symlink():
            _remove_path(Path(raw_path))
    archive = snapshot / "support.tar.gz"
    if archive.exists():
        _extract_archive(archive, Path("/"))


def restore_snapshot(paths: RuntimePaths, components: Sequence[Component], snapshot: Path) -> None:
    """Restore source trees and runtime support artifacts from a snapshot."""
    metadata = _load_metadata(snapshot)
    for component in components:
        _restore_component(component, snapshot, metadata)
    _restore_support(paths, snapshot, metadata)


def _fetch(component: Component) -> str:
    _print(f"fetch: {component.name} {component.remote_ref}")
    _git(component, "fetch", "--no-tags", component.remote, component.branch)
    return _git(component, "rev-parse", component.remote_ref)


def plan(components: Sequence[Component]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for component in components:
        _assert_git_component(component)
        target = _fetch(component)
        head = _git(component, "rev-parse", "HEAD")
        ahead_behind = _git(component, "rev-list", "--left-right", "--count", f"{head}...{target}")
        left, right = (int(value) for value in ahead_behind.split())
        row = {
            "component": component.name,
            "branch": _branch(component),
            "head": head,
            "target": target,
            "local_only_commits": left,
            "incoming_commits": right,
            "dirty": bool(_status(component)),
        }
        result.append(row)
    return result


def _stash_if_dirty(component: Component, label: str) -> str | None:
    if not _status(component):
        return None
    _print(f"stash: preserving dirty tree for {component.name}")
    _git(component, "stash", "push", "--include-untracked", "--message", label)
    if _status(component):
        raise UpdateError(f"{component.name}: worktree remained dirty after stash")
    # ``git stash pop`` accepts a reflog selector (stash@{0}), not the raw
    # object SHA returned by rev-parse. This updater holds a process-wide lock
    # and creates no other stashes between push and pop, so stash@{0} is stable.
    return "stash@{0}"


def _restore_stash(component: Component, stash_ref: str | None) -> None:
    if stash_ref is None:
        return
    _print(f"stash: restoring local changes for {component.name}")
    _git(component, "stash", "pop", "--index", stash_ref)


def update_component(component: Component, run_label: str) -> bool:
    """Merge a remote ref into the active local branch without rewriting it."""
    _assert_git_component(component)
    target = _fetch(component)
    current = _git(component, "rev-parse", "HEAD")
    if current == target:
        _print(f"update: {component.name} already at {target[:12]}")
        return False

    stash_ref = _stash_if_dirty(component, f"hermes-stack-update:{run_label}:{component.name}")
    try:
        _print(f"merge: {component.name} {component.remote_ref} into {_branch(component)}")
        _git(component, "merge", "--no-edit", component.remote_ref)
        _restore_stash(component, stash_ref)
    except Exception:
        # Do not run a destructive merge --abort here: the caller owns rollback
        # and has a byte-for-byte snapshot. Keep Git's diagnostics available.
        raise
    return _git(component, "rev-parse", "HEAD") != current


def _systemctl(paths: RuntimePaths, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    result = subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "systemctl failed").strip()
        raise UpdateError(f"systemctl --user {' '.join(args)}: {detail[-1200:]}")
    return result


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                raise UpdateError(f"health endpoint returned HTTP {response.status}: {url}")
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"health endpoint unavailable {url}: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"health endpoint did not return JSON {url}: {body[:200]}") from exc


def _wait_health(url: str, service: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            payload = _http_json(url)
            if payload.get("ok") is True:
                return
            last_error = f"payload lacks ok=true: {payload}"
        except UpdateError as exc:
            last_error = str(exc)
        time.sleep(1)
    raise UpdateError(f"{service} did not become healthy: {last_error}")


def _validate_extensions(paths: RuntimePaths) -> None:
    manifest = paths.extensions / "extensions.json"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        entries = document["extensions"]
        ids = {entry["id"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"invalid Hermes One extension manifest {manifest}: {exc}") from exc
    missing = sorted(REQUIRED_EXTENSION_IDS - ids)
    if missing:
        raise UpdateError(f"extension manifest lost required entries: {', '.join(missing)}")


def _validate_sources(paths: RuntimePaths, components: Sequence[Component]) -> None:
    for component in components:
        _run(("git", "-C", str(component.path), "diff", "--check"))

    _print("validate: core Python syntax")
    _run(
        (
            str(paths.python),
            "-m",
            "compileall",
            "-q",
            "hermes_cli",
            "agent",
            "plugins/memory/holographic",
            "tools/memory_tool.py",
        ),
        cwd=paths.core,
    )

    memory_tests = [
        paths.core / "tests/plugins/memory/test_holographic_recall_benchmark.py",
        paths.core / "tests/plugins/memory/test_holographic_recall_fixes.py",
        paths.core / "tests/plugins/memory/test_holographic_embedder_health.py",
    ]
    if all(test.exists() for test in memory_tests):
        _print("validate: Holographic Memory regression suite")
        _run((str(paths.python), "-m", "pytest", "-q", *(str(test) for test in memory_tests)), cwd=paths.core)

    _print("validate: delegate-profile suite")
    _run((sys.executable, "-m", "pytest", "-q", "--disable-warnings", "--maxfail=1"), cwd=paths.plugin)

    _print("validate: Hermes One Python syntax")
    _run((str(paths.python), "-m", "compileall", "-q", "api", "server.py"), cwd=paths.one)
    _validate_extensions(paths)


def _reinstall_router_bundle(paths: RuntimePaths) -> None:
    if not paths.installer.exists():
        raise UpdateError(f"Router installer not found after plugin update: {paths.installer}")
    _print("install: refreshing Capability Router extension and service unit")
    _run(
        (
            str(paths.python),
            str(paths.installer),
            "--extension-root",
            str(paths.extensions),
            "--systemd-dir",
            str(paths.systemd_user),
            "--plugin-dir",
            str(paths.plugin),
            "--hermes-home",
            str(paths.profile_home),
            "--webui-state-dir",
            str(paths.profile_home / "webui"),
        )
    )


def _restart_and_healthcheck(paths: RuntimePaths) -> None:
    _print("restart: systemd user units")
    _systemctl(paths, "daemon-reload")
    # The WebUI imports Hermes in-process. Delete this cache before its restart
    # so the next process rebuilds labels/model metadata from the new runtime.
    paths.model_cache.unlink(missing_ok=True)

    # The Router must come up before WebUI, whose extension proxy forwards to it.
    for unit in ("hermes-router-sidecar.service", "hermes-memory-sidecar.service", "hermes-webui.service"):
        _systemctl(paths, "restart", unit)
        _systemctl(paths, "is-active", "--quiet", unit)

    _wait_health("http://127.0.0.1:8791/health", "Capability Router")
    _wait_health("http://127.0.0.1:8792/health", "Memory Graph")
    _wait_health("http://127.0.0.1:8787/health", "Hermes One")


def _restart_after_rollback(paths: RuntimePaths) -> None:
    """Best-effort recovery: source was restored even if a unit refuses restart."""
    try:
        _systemctl(paths, "daemon-reload")
        for unit in ("hermes-router-sidecar.service", "hermes-memory-sidecar.service", "hermes-webui.service"):
            _systemctl(paths, "restart", unit)
    except UpdateError as exc:
        _print(f"rollback warning: service restart failed: {exc}")


def apply(paths: RuntimePaths, components: Sequence[Component], *, skip_tests: bool = False) -> Path:
    snapshot = create_snapshot(paths, components)
    try:
        run_label = snapshot.name
        changes = [component.name for component in components if update_component(component, run_label)]
        _reinstall_router_bundle(paths)
        if not skip_tests:
            _validate_sources(paths, components)
        else:
            _print("validate: skipped by explicit --skip-tests")
        _restart_and_healthcheck(paths)
        metadata = _load_metadata(snapshot)
        metadata["outcome"] = "committed"
        metadata["changed_components"] = changes
        metadata["completed_at"] = datetime.now(UTC).isoformat()
        _write_json(snapshot / "metadata.json", metadata)
        _print(f"update committed; rollback snapshot: {snapshot}")
        return snapshot
    except Exception as exc:
        _print(f"update failed: {exc}")
        _print(f"rollback: restoring snapshot {snapshot}")
        try:
            restore_snapshot(paths, components, snapshot)
            _restart_after_rollback(paths)
        except Exception as rollback_exc:
            raise UpdateError(
                f"update failed ({exc}); AUTOMATIC ROLLBACK ALSO FAILED ({rollback_exc}). "
                f"Snapshot remains at {snapshot}"
            ) from rollback_exc
        metadata = _load_metadata(snapshot)
        metadata["outcome"] = "rolled_back"
        metadata["failure"] = str(exc)
        metadata["completed_at"] = datetime.now(UTC).isoformat()
        _write_json(snapshot / "metadata.json", metadata)
        raise UpdateError(f"update rolled back successfully; snapshot retained at {snapshot}: {exc}") from exc


def status(paths: RuntimePaths, components: Sequence[Component]) -> list[dict[str, Any]]:
    rows = plan(components)
    _validate_extensions(paths)
    for unit in ("hermes-router-sidecar.service", "hermes-memory-sidecar.service", "hermes-webui.service"):
        active = _systemctl(paths, "is-active", unit, check=False).returncode == 0
        rows.append({"service": unit, "active": active})
    return rows


def _lock(paths: RuntimePaths):
    paths.lock_path.parent.mkdir(parents=True, exist_ok=True)
    _safe_mode(paths.lock_path.parent, 0o700)
    handle = paths.lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise UpdateError("another Hermes stack update is already running") from exc
    return handle


def _resolve_snapshot(paths: RuntimePaths, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = paths.backup_root / value
    candidate = candidate.resolve()
    if paths.backup_root.resolve() not in candidate.parents:
        raise UpdateError("snapshot must be under the configured backup root")
    if not candidate.is_dir():
        raise UpdateError(f"snapshot not found: {candidate}")
    return candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("HERMES_PROFILE", "rodrigo"))
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--component", choices=("all", "core", "plugin", "one"), default="all")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_component_override(command_parser: argparse.ArgumentParser) -> None:
        # Accept it both before and after the subcommand; argparse otherwise
        # treats ``plan --component core`` as an error despite being natural CLI
        # syntax. SUPPRESS preserves the top-level default when omitted.
        command_parser.add_argument(
            "--component",
            choices=("all", "core", "plugin", "one"),
            default=argparse.SUPPRESS,
        )

    status_parser = subparsers.add_parser("status", help="fetch refs and print update/service state")
    add_component_override(status_parser)
    plan_parser = subparsers.add_parser("plan", help="fetch refs and print the merge plan without updating")
    add_component_override(plan_parser)
    apply_parser = subparsers.add_parser("apply", help="snapshot, update, validate, restart, and healthcheck")
    add_component_override(apply_parser)
    apply_parser.add_argument("--yes", action="store_true", help="required acknowledgement for a mutating update")
    apply_parser.add_argument("--skip-tests", action="store_true", help="skip regression suites; intended only for emergency recovery")
    rollback_parser = subparsers.add_parser("rollback", help="restore an exact prior snapshot")
    add_component_override(rollback_parser)
    rollback_parser.add_argument("snapshot", help="snapshot id or absolute snapshot path")
    rollback_parser.add_argument("--yes", action="store_true", help="required acknowledgement for rollback")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = default_paths(args.home, args.profile)
    components = default_components(paths)
    if args.component != "all":
        components = [component for component in components if component.name == args.component]

    try:
        lock = _lock(paths)
        try:
            if args.command in {"status", "plan"}:
                rows = status(paths, components) if args.command == "status" else plan(components)
                print(json.dumps(rows, indent=2, sort_keys=True))
                return 0
            if args.command == "apply":
                if not args.yes:
                    raise UpdateError("refusing mutation without --yes")
                apply(paths, components, skip_tests=args.skip_tests)
                return 0
            if args.command == "rollback":
                if not args.yes:
                    raise UpdateError("refusing rollback without --yes")
                snapshot = _resolve_snapshot(paths, args.snapshot)
                restore_snapshot(paths, components, snapshot)
                _restart_after_rollback(paths)
                _print(f"rollback completed from {snapshot}")
                return 0
            raise AssertionError(f"unhandled command: {args.command}")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
