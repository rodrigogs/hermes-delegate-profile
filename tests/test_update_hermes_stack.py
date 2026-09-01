"""Regression tests for the transactional Hermes stack updater.

These tests use disposable bare Git remotes. They prove the two properties that
matter more than happy-path fetches: local commits and a dirty worktree survive
a successful merge, and a stash conflict can be restored byte-for-byte from the
pre-update snapshot.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_hermes_stack.py"
INSTALLER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_hermes_stack_updater.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


updater = load_module("hermes_stack_update", SCRIPT)
timer_installer = load_module("hermes_stack_timer_installer", INSTALLER_SCRIPT)


def git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def configure_repo(repo: Path) -> None:
    git(repo, "config", "user.name", "Updater Test")
    git(repo, "config", "user.email", "updater@example.invalid")


@pytest.fixture
def component_repo(tmp_path: Path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True)
    configure_repo(repo)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "push", "origin", "main")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True)
    configure_repo(other)
    yield repo, other


def runtime_paths(tmp_path: Path) -> Any:
    home = tmp_path / "home"
    extensions = home / "hermes-one-extensions"
    extensions.mkdir(parents=True)
    (extensions / "extensions.json").write_text('{"extensions": []}\n', encoding="utf-8")
    systemd_user = home / ".config/systemd/user"
    systemd_user.mkdir(parents=True)
    (systemd_user / "hermes-router-sidecar.service").write_text("old unit\n", encoding="utf-8")
    profile_webui = home / ".hermes/profiles/rodrigo/webui"
    profile_webui.mkdir(parents=True)
    (profile_webui / "models_cache.rodrigo.json").write_text('{"old": true}\n', encoding="utf-8")
    return updater.RuntimePaths(
        home=home,
        profile="rodrigo",
        core=tmp_path / "unused-core",
        plugin=tmp_path / "unused-plugin",
        one=tmp_path / "unused-one",
        extensions=extensions,
        systemd_user=systemd_user,
    )


def test_update_preserves_local_commit_and_dirty_files(component_repo, tmp_path: Path):
    repo, other = component_repo
    (other / "remote.txt").write_text("incoming\n", encoding="utf-8")
    git(other, "add", "remote.txt")
    git(other, "commit", "-m", "incoming")
    git(other, "push", "origin", "main")

    (repo / "local.txt").write_text("local commit\n", encoding="utf-8")
    git(repo, "add", "local.txt")
    git(repo, "commit", "-m", "local patch")
    (repo / "tracked.txt").write_text("dirty local edit\n", encoding="utf-8")
    (repo / "nested").mkdir()
    (repo / "nested/untracked.txt").write_text("untracked\n", encoding="utf-8")

    component = updater.Component("plugin", repo, "origin", "main")
    changed = updater.update_component(component, "test-run")

    assert changed is True
    assert (repo / "remote.txt").read_text(encoding="utf-8") == "incoming\n"
    assert (repo / "local.txt").read_text(encoding="utf-8") == "local commit\n"
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "dirty local edit\n"
    assert (repo / "nested/untracked.txt").read_text(encoding="utf-8") == "untracked\n"
    assert "tracked.txt" in git(repo, "status", "--short")


def test_snapshot_restores_after_stash_conflict(component_repo, tmp_path: Path):
    repo, other = component_repo
    paths = runtime_paths(tmp_path)
    component = updater.Component("plugin", repo, "origin", "main")

    # Remote and local modify the same hunk. Merge succeeds after stashing, but
    # stash pop conflicts and leaves Git unmerged. The snapshot is the recovery
    # authority, not Git's best-effort conflict state.
    (other / "tracked.txt").write_text("remote version\n", encoding="utf-8")
    git(other, "add", "tracked.txt")
    git(other, "commit", "-m", "remote conflict")
    git(other, "push", "origin", "main")

    (repo / "tracked.txt").write_text("local dirty version\n", encoding="utf-8")
    (repo / "keep.txt").write_text("must survive\n", encoding="utf-8")
    head_before = git(repo, "rev-parse", "HEAD")
    snapshot = updater.create_snapshot(paths, [component])

    (paths.extensions / "extensions.json").write_text('{"extensions": ["mutated"]}\n', encoding="utf-8")
    paths.router_unit.write_text("mutated unit\n", encoding="utf-8")
    paths.model_cache.write_text('{"mutated": true}\n', encoding="utf-8")

    with pytest.raises(updater.UpdateError):
        updater.update_component(component, "test-conflict")

    updater.restore_snapshot(paths, [component], snapshot)

    assert git(repo, "rev-parse", "HEAD") == head_before
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "local dirty version\n"
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "must survive\n"
    assert (paths.extensions / "extensions.json").read_text(encoding="utf-8") == '{"extensions": []}\n'
    assert paths.router_unit.read_text(encoding="utf-8") == "old unit\n"
    assert paths.model_cache.read_text(encoding="utf-8") == '{"old": true}\n'


def test_restore_rejects_archive_path_escape(tmp_path: Path):
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../../escape")
        info.size = 0
        tar.addfile(info)

    with pytest.raises(updater.UpdateError, match="unsafe archive member"):
        updater._extract_archive(archive, tmp_path / "destination")


def test_timer_installer_deploys_runtime_copy_and_safe_weekly_timer(tmp_path: Path):
    source = tmp_path / "source.py"
    source.write_text("print('updater')\n", encoding="utf-8")
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    systemd = tmp_path / "systemd"

    installed, service, timer = timer_installer.install(
        source_script=source,
        plugin=plugin,
        systemd_dir=systemd,
        python=Path("/opt/hermes/python3"),
        profile="rodrigo",
        core=Path("/opt/hermes"),
        one=Path("/opt/one"),
        extensions=Path("/opt/extensions"),
    )

    assert installed.read_text(encoding="utf-8") == "print('updater')\n"
    service_text = service.read_text(encoding="utf-8")
    assert f"ExecStart=/opt/hermes/python3 {installed} --profile rodrigo apply --yes" in service_text
    assert "HERMES_PLUGIN_DIR=" + str(plugin) in service_text
    timer_text = timer.read_text(encoding="utf-8")
    assert "OnCalendar=Sat *-*-* 04:15:00" in timer_text
    assert "Persistent=true" in timer_text


def test_webui_cache_is_deleted_before_webui_restart(tmp_path: Path, monkeypatch):
    paths = runtime_paths(tmp_path)
    events: list[tuple[str, tuple[str, ...]]] = []

    def fake_systemctl(_paths, *args, check=True):
        events.append(("systemctl", args))
        if args == ("restart", "hermes-webui.service"):
            assert not paths.model_cache.exists()
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(updater, "_systemctl", fake_systemctl)
    monkeypatch.setattr(updater, "_wait_health", lambda url, service: events.append(("health", (service,))))

    updater._restart_and_healthcheck(paths)

    assert ("health", ("Hermes One",)) in events


def test_no_module_imports_the_3_11_only_UTC_alias():
    """A single 3.11-only import aborted COLLECTION of the whole suite.

    ``datetime.UTC`` is an alias added in 3.11. ``pyproject.toml`` does declare
    ``requires-python = ">=3.11"``, so using it was allowed — the defect is the
    FAILURE MODE, not the version floor. This module is imported at collection
    time by this very file, so on 3.10 pytest raised during collection and
    reported ZERO tests instead of failing one file. A contributor whose default
    interpreter is 3.10 (which is the case on the machine this was found on) saw
    the entire suite refuse to start, with a traceback pointing at a deployment
    helper they had not touched.

    ``timezone.utc`` is identical, works everywhere, and is what every other
    module in the tree already imports — this was the lone outlier. Asserted over
    the whole tree rather than over the one file that had it, so the next
    3.11-only alias does not reintroduce the abort somewhere else.
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", ".worktrees", "__pycache__"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our files
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "datetime":
                for alias in node.names:
                    if alias.name == "UTC":
                        offenders.append(
                            f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, (
        "these import the 3.11-only datetime.UTC alias; use timezone.utc so a "
        f"3.10 interpreter fails a test rather than aborting collection: {offenders}"
    )
