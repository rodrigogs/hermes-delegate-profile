"""Tests for the per-profile routing collapse script.

Each test builds a throwaway Hermes home in ``tmp_path`` — the real ``~/.hermes``
is never touched. The fixture deliberately contains the three states the script
has to survive: a full routing copy, an already-collapsed profile, and a profile
carrying per-role keys that MUST outlive the collapse.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from scripts import collapse_profile_routing
from scripts.collapse_profile_routing import (
    backup_root,
    collapse,
    collapse_document,
    main,
)

STAMP = "20260817T120000Z"

# A full copy of the root routing block, exactly the shape being de-shadowed.
_FULL_COPY = """\
# coder profile
model: gpt-5.6-terra
fallback_providers:
  - zai
  - deepseek
max_turns: 40
platform_toolsets:
  - shell
auxiliary:
  vision: glm-4.6v
  compression:
    provider: auto
agent:
  role_guard: strict
"""

# Already collapsed: inherits model/fallback_providers/auxiliary.vision from root.
_ALREADY_COLLAPSED = """\
max_turns: 12
plugins:
  - delegate-profile
auxiliary:
  compression:
    provider: auto
"""

# Per-role overrides that are NOT routing and must survive byte-for-byte in
# meaning, including a non-vision key inside auxiliary.
_WITH_ROLE_OVERRIDE = """\
model: glm-4.7
fallback_providers:
  - xiaomi
max_turns: 3
reasoning_effort: low
mcp_servers:
  kanban:
    command: hermes-kanban
terminal:
  shell: zsh
delegation:
  allow_cross_profile: false
kanban:
  board: reviewer
onboarding:
  seen: true
tool_loop_guardrails:
  max_repeats: 2
auxiliary:
  vision: glm-4.5v
  compression:
    provider: zai
"""

# The other shape ``auxiliary.vision`` takes in the wild: a mapping that mixes
# the routing declaration with settings that are nobody's business here.
_VISION_MAPPING = """\
model: glm-4.7
max_turns: 9
auxiliary:
  vision:
    model: glm-4.6v
    provider: zai
    max_image_bytes: 2048
    detail: high
"""

# Same shape, but holding nothing except routing — so it collapses away whole.
_VISION_MAPPING_ROUTING_ONLY = """\
max_turns: 7
auxiliary:
  vision:
    model: glm-4.6v
    provider: zai
"""

# Root permission is not enough: the atomic replace also needs the directory.
_ROOT_ONLY = os.geteuid() == 0


@pytest.fixture
def make_read_only():
    """chmod a path for one test and put its mode back afterwards.

    Without the restore, pytest's tmp_path retention cannot clean up a 0o500
    directory and the *next* run inherits the failure.
    """
    restore: List[Tuple[Path, int]] = []

    def _make(path: Path, mode: int) -> Path:
        restore.append((path, path.stat().st_mode & 0o7777))
        path.chmod(mode)
        return path

    yield _make

    for path, mode in reversed(restore):
        try:
            path.chmod(mode)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass


def _write_home(tmp_path: Path, extra: Dict[str, str] | None = None) -> Path:
    """Build a fake hermes-home with three profiles (plus any ``extra``)."""
    home = tmp_path / "hermes-home"
    profiles = {
        "coder": _FULL_COPY,
        "thin": _ALREADY_COLLAPSED,
        "reviewer": _WITH_ROLE_OVERRIDE,
    }
    profiles.update(extra or {})
    for name, body in profiles.items():
        target = home / "profiles" / name / "config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return home


def _snapshot(home: Path) -> Dict[str, bytes]:
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in sorted(home.rglob("config.yaml"))
    }


def _load(home: Path, profile: str) -> Dict[str, Any]:
    raw = (home / "profiles" / profile / "config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(raw)


# ---------------------------------------------------------------------------
# Pure planning
# ---------------------------------------------------------------------------

def test_collapse_document_is_pure_and_reports_removed_paths():
    document = yaml.safe_load(_FULL_COPY)
    original = yaml.safe_load(_FULL_COPY)

    collapsed, removed = collapse_document(document)

    assert document == original, "collapse_document must not mutate its input"
    assert removed[:3] == ["model", "fallback_providers", "auxiliary.vision"]
    assert "model" not in collapsed
    assert "fallback_providers" not in collapsed
    assert "vision" not in collapsed["auxiliary"]
    # Everything else survives, ordering included.
    assert collapsed["auxiliary"]["compression"] == {"provider": "auto"}
    assert collapsed["max_turns"] == 40
    assert collapsed["agent"] == {"role_guard": "strict"}


def test_emptied_auxiliary_is_pruned_so_it_cannot_shadow_the_root_block():
    document = {"model": "x", "auxiliary": {"vision": "glm-4.6v"}, "max_turns": 5}

    collapsed, removed = collapse_document(document)

    assert "auxiliary" not in collapsed, (
        "an empty auxiliary mapping would shadow the root's whole auxiliary block"
    )
    assert "auxiliary (now-empty parent)" in removed
    assert collapsed == {"max_turns": 5}


def test_mapping_form_vision_loses_only_its_routing_keys():
    document = yaml.safe_load(_VISION_MAPPING)

    collapsed, removed = collapse_document(document)

    # The routing declaration goes; the settings sitting beside it do not.
    assert collapsed == {
        "max_turns": 9,
        "auxiliary": {"vision": {"max_image_bytes": 2048, "detail": "high"}},
    }
    assert removed == ["model", "auxiliary.vision.model", "auxiliary.vision.provider"]


def test_mapping_form_vision_is_pruned_only_once_it_is_empty():
    document = yaml.safe_load(_VISION_MAPPING_ROUTING_ONLY)

    collapsed, removed = collapse_document(document)

    assert collapsed == {"max_turns": 7}
    assert removed == [
        "auxiliary.vision.model",
        "auxiliary.vision.provider",
        "auxiliary.vision (now-empty mapping)",
        "auxiliary (now-empty parent)",
    ]


def test_mapping_form_vision_with_no_routing_keys_is_already_collapsed():
    document = {"auxiliary": {"vision": {"max_image_bytes": 2048}}}

    collapsed, removed = collapse_document(document)

    assert removed == [], "no routing key present means nothing to de-shadow"
    assert collapsed == {"auxiliary": {"vision": {"max_image_bytes": 2048}}}


def test_mapping_form_vision_survives_the_applied_collapse(tmp_path):
    home = _write_home(tmp_path, extra={"eyes": _VISION_MAPPING})

    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0

    assert _load(home, "eyes") == {
        "max_turns": 9,
        "auxiliary": {"vision": {"max_image_bytes": 2048, "detail": "high"}},
    }


# ---------------------------------------------------------------------------
# Dry-run is the default
# ---------------------------------------------------------------------------

def test_dry_run_is_the_default_and_writes_nothing(tmp_path, capsys):
    home = _write_home(tmp_path)
    before = _snapshot(home)

    rc = main(["--hermes-home", str(home)])

    assert rc == 0
    assert _snapshot(home) == before, "the default run must not write"
    assert not (home / "backups").exists()
    out = capsys.readouterr().out
    assert "would remove model, fallback_providers, auxiliary.vision" in out
    assert "dry-run: nothing was written" in out
    # pyyaml cannot keep comments; the script must say so rather than pretend.
    assert "cannot preserve YAML comments" in out


def test_explicit_dry_run_flag_also_writes_nothing(tmp_path):
    home = _write_home(tmp_path)
    before = _snapshot(home)

    assert main(["--hermes-home", str(home), "--dry-run", "--stamp", STAMP]) == 0

    assert _snapshot(home) == before
    assert not (home / "backups").exists()


def test_apply_without_stamp_is_a_usage_error_and_writes_nothing(tmp_path):
    home = _write_home(tmp_path)
    before = _snapshot(home)

    with pytest.raises(SystemExit) as excinfo:
        main(["--hermes-home", str(home), "--apply"])

    assert excinfo.value.code != 0
    assert _snapshot(home) == before
    assert not (home / "backups").exists()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def test_apply_removes_exactly_the_three_key_paths(tmp_path, capsys):
    home = _write_home(tmp_path)

    rc = main(["--hermes-home", str(home), "--apply", "--stamp", STAMP])

    assert rc == 0
    coder = _load(home, "coder")
    assert "model" not in coder
    assert "fallback_providers" not in coder
    assert "vision" not in coder["auxiliary"]
    # ...and nothing else went missing.
    assert coder == {
        "max_turns": 40,
        "platform_toolsets": ["shell"],
        "auxiliary": {"compression": {"provider": "auto"}},
        "agent": {"role_guard": "strict"},
    }
    assert "removed model, fallback_providers, auxiliary.vision" in capsys.readouterr().out


def test_per_role_keys_survive_the_collapse(tmp_path):
    home = _write_home(tmp_path)
    expected = yaml.safe_load(_WITH_ROLE_OVERRIDE)
    del expected["model"]
    del expected["fallback_providers"]
    del expected["auxiliary"]["vision"]

    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0

    assert _load(home, "reviewer") == expected
    # Spot-check the keys an over-eager rewrite would drop.
    reviewer = _load(home, "reviewer")
    for key in (
        "max_turns",
        "reasoning_effort",
        "mcp_servers",
        "terminal",
        "delegation",
        "kanban",
        "onboarding",
        "tool_loop_guardrails",
    ):
        assert key in reviewer, f"{key} must survive"
    assert reviewer["auxiliary"]["compression"] == {"provider": "zai"}


def test_backups_land_at_the_expected_stamped_paths(tmp_path):
    home = _write_home(tmp_path)
    original_coder = (home / "profiles" / "coder" / "config.yaml").read_bytes()

    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0

    root = backup_root(home, STAMP)
    assert root == home / "backups" / f"collapse-profile-routing-{STAMP}"
    assert (root / "profiles" / "coder" / "config.yaml").read_bytes() == original_coder
    assert (root / "profiles" / "reviewer" / "config.yaml").is_file()
    # The already-collapsed profile does not change, so it is not backed up.
    assert not (root / "profiles" / "thin" / "config.yaml").exists()


def test_already_collapsed_profile_warns_and_is_left_alone(tmp_path, capsys):
    home = _write_home(tmp_path)
    before = (home / "profiles" / "thin" / "config.yaml").read_bytes()

    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0

    assert (home / "profiles" / "thin" / "config.yaml").read_bytes() == before
    assert "profiles/thin/config.yaml: already collapsed" in capsys.readouterr().out


def test_second_apply_is_a_clean_no_op(tmp_path, capsys):
    home = _write_home(tmp_path)
    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0
    after_first = _snapshot(home)
    capsys.readouterr()

    second_stamp = "20260817T130000Z"
    rc = main(["--hermes-home", str(home), "--apply", "--stamp", second_stamp])

    assert rc == 0
    assert _snapshot(home) == after_first, "a second apply must not rewrite anything"
    assert not backup_root(home, second_stamp).exists(), (
        "a no-op run must not create a backup directory"
    )
    out = capsys.readouterr().out
    assert "already collapsed" in out
    assert "removed model" not in out


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------

def test_malformed_profile_aborts_non_zero_and_writes_nothing(tmp_path, capsys):
    home = _write_home(tmp_path, extra={"broken": "model: [unterminated\n"})
    before = _snapshot(home)

    rc = main(["--hermes-home", str(home), "--apply", "--stamp", STAMP])

    assert rc != 0
    assert _snapshot(home) == before, "a parse failure must not write a partial result"
    assert not (home / "backups").exists()
    assert "failed to parse" in capsys.readouterr().err


def test_non_mapping_profile_is_a_parse_failure(tmp_path):
    home = _write_home(tmp_path, extra={"listy": "- not\n- a mapping\n"})
    before = _snapshot(home)

    rc = main(["--hermes-home", str(home), "--apply", "--stamp", STAMP])

    assert rc != 0
    assert _snapshot(home) == before
    assert not (home / "backups").exists()


def test_missing_hermes_home_exits_non_zero(tmp_path, capsys):
    rc = main(["--hermes-home", str(tmp_path / "nope"), "--apply", "--stamp", STAMP])

    assert rc == 2
    assert "hermes home not found" in capsys.readouterr().err


def test_traversal_stamp_is_rejected(tmp_path):
    home = _write_home(tmp_path)
    before = _snapshot(home)

    with pytest.raises(SystemExit) as excinfo:
        main(["--hermes-home", str(home), "--apply", "--stamp", "../../etc"])

    assert excinfo.value.code != 0
    assert _snapshot(home) == before


@pytest.mark.skipif(_ROOT_ONLY, reason="root bypasses the write-permission check")
def test_unwritable_target_aborts_before_the_first_write(tmp_path, capsys, make_read_only):
    home = _write_home(tmp_path)
    make_read_only(home / "profiles" / "reviewer", 0o500)
    before = _snapshot(home)

    rc = main(["--hermes-home", str(home), "--apply", "--stamp", STAMP])

    assert rc != 0
    assert _snapshot(home) == before, "an unwritable target must abort with zero writes"
    assert not (home / "backups").exists(), "not even a backup is taken"
    out = capsys.readouterr()
    # The diagnostic has to name the offending file, on both streams.
    assert "profiles/reviewer/config.yaml" in out.err
    assert "not writable" in out.err
    assert "nothing was written" in out.err
    assert "profiles/coder/config.yaml: NOT WRITTEN" in out.out


@pytest.mark.skipif(_ROOT_ONLY, reason="root bypasses the write-permission check")
def test_unwritable_file_in_a_writable_directory_also_aborts(tmp_path, capsys, make_read_only):
    home = _write_home(tmp_path)
    make_read_only(home / "profiles" / "coder" / "config.yaml", 0o444)
    before = _snapshot(home)

    rc = main(["--hermes-home", str(home), "--apply", "--stamp", STAMP])

    assert rc != 0
    assert _snapshot(home) == before
    assert not (home / "backups").exists()
    assert "profiles/coder/config.yaml: file is not writable" in capsys.readouterr().err


@pytest.mark.skipif(_ROOT_ONLY, reason="root bypasses the write-permission check")
def test_unwritable_but_unchanged_profile_does_not_block_the_run(tmp_path, make_read_only):
    """``thin`` is already collapsed, so it is never written and never checked."""
    home = _write_home(tmp_path)
    make_read_only(home / "profiles" / "thin", 0o500)

    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0

    assert "model" not in _load(home, "coder")


def test_mid_run_write_failure_still_reports_written_and_not_written(
    tmp_path, capsys, monkeypatch
):
    """A failure the pre-flight cannot see (a racing chmod, a full disk)."""
    home = _write_home(tmp_path)
    real_write = collapse_profile_routing._atomic_write_bytes
    calls: List[Path] = []

    def flaky_write(path: Path, data: bytes) -> None:
        calls.append(path)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        real_write(path, data)

    monkeypatch.setattr(collapse_profile_routing, "_atomic_write_bytes", flaky_write)
    reviewer_before = (home / "profiles" / "reviewer" / "config.yaml").read_bytes()

    rc = main(["--hermes-home", str(home), "--apply", "--stamp", STAMP])

    assert rc != 0, "a partial write must not look like success"
    # coder was written, reviewer was not, and the operator is told exactly that.
    assert "model" not in _load(home, "coder")
    assert (home / "profiles" / "reviewer" / "config.yaml").read_bytes() == reviewer_before
    out = capsys.readouterr()
    assert "profiles/coder/config.yaml: removed model" in out.out
    assert "profiles/reviewer/config.yaml: NOT WRITTEN" in out.out
    assert "No space left on device" in out.out
    # Backups were complete before the first write, and the report says where.
    root = backup_root(home, STAMP)
    assert f"backup: {root}" in out.out
    assert (root / "profiles" / "coder" / "config.yaml").read_bytes() != b""
    assert (root / "profiles" / "reviewer" / "config.yaml").read_bytes() == reviewer_before
    assert str(root) in out.err


def test_mid_run_write_failure_report_is_usable_without_the_cli(tmp_path, monkeypatch):
    home = _write_home(tmp_path)

    def always_fails(path: Path, data: bytes) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(collapse_profile_routing, "_atomic_write_bytes", always_fails)

    report = collapse(home, STAMP, apply=True)

    assert report["applied"] is False
    assert report["written"] == []
    assert report["not_written"] == [
        "profiles/coder/config.yaml",
        "profiles/reviewer/config.yaml",
    ]
    assert len(report["failures"]) == 1
    assert report["failures"][0]["relative"] == "profiles/coder/config.yaml"
    assert "Permission denied" in report["failures"][0]["error"]
    assert report["backup_dir"] == str(backup_root(home, STAMP))
    assert report["errors"] == []


def test_collapse_report_is_usable_without_the_cli(tmp_path):
    home = _write_home(tmp_path)

    report = collapse(home, STAMP, apply=False)

    assert report["applied"] is False
    assert report["backup_dir"] is None
    assert report["errors"] == []
    changed = {change["relative"]: change["removed"] for change in report["changes"]}
    assert set(changed) == {
        "profiles/coder/config.yaml",
        "profiles/reviewer/config.yaml",
    }
    assert report["unchanged"] == ["profiles/thin/config.yaml"]
