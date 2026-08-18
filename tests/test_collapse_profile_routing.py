"""Tests for the per-profile routing collapse script.

Each test builds a throwaway Hermes home in ``tmp_path`` — the real ``~/.hermes``
is never touched. The fixture deliberately contains the three states the script
has to survive: a full routing copy, an already-collapsed profile, and a profile
carrying per-role keys that MUST outlive the collapse.
"""

from __future__ import annotations

import os
import stat
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
    verify_after_write,
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


def _modes(home: Path) -> Dict[str, int]:
    """Permission bits of the live profile configs — never the backup copies."""
    return {
        str(path.relative_to(home)): stat.S_IMODE(path.stat().st_mode)
        for path in sorted((home / "profiles").glob("*/config.yaml"))
    }


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


# ---------------------------------------------------------------------------
# Permissions survive the atomic replace
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_ROOT_ONLY, reason="umask/ownership behave differently as root")
def test_every_rewritten_file_keeps_the_mode_it_had(tmp_path):
    """The AGREEMENT: mode after == mode before, per file, not "not 0600".

    ``tempfile.mkstemp`` creates at 0600 and ``os.replace`` keeps the TEMP file's
    mode, so every rewritten profile config silently lost its permissions —
    measured 0664 in, 0600 out. Asserting the pair rather than one side is what
    makes this test survive a different umask, and it covers the profile that was
    ALREADY 0600 too, where "not 0600" would have passed on a broken script.
    """
    home = _write_home(tmp_path)
    wanted = {
        "coder": 0o664,     # the measured real-world mode, and the one that broke
        "reviewer": 0o600,  # already private: a naive check cannot see this one
        "thin": 0o644,      # never written; must not be touched either
    }
    for profile, mode in wanted.items():
        (home / "profiles" / profile / "config.yaml").chmod(mode)
    before = _modes(home)

    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0

    assert _modes(home) == before
    # ...and the collapse really did happen, so this is not passing on a no-op.
    assert "model" not in _load(home, "coder")


# ---------------------------------------------------------------------------
# Post-write re-parse — the runbook's "then re-parse all 16"
# ---------------------------------------------------------------------------

def test_a_clean_apply_reports_that_it_re_parsed_every_config(tmp_path, capsys):
    """The deploy doc's §4 claim, asserted against the script that has to honour it."""
    home = _write_home(tmp_path)

    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0

    report = capsys.readouterr().out
    assert "re-parsed all 3 profile config(s) after the write" in report
    assert "matches the document that was planned" in report


def test_a_write_that_lands_unparseable_bytes_is_caught_and_named(
    tmp_path, capsys, monkeypatch
):
    """The runbook's incident, reproduced: PyYAML used, tree still corrupt.

    A write can succeed and still land the wrong bytes, which is exactly why the
    runbook's constraint has two halves — *"Only ever edit with Python + PyYAML,
    then re-parse all 16"* — and the script only honoured the first. The operator
    must be told WHICH file and that a backup exists, on stderr, non-zero.
    """
    home = _write_home(tmp_path)
    real_write = collapse_profile_routing._atomic_write_bytes

    def corrupting_write(path: Path, data: bytes) -> None:
        if path.parent.name == "reviewer":
            data = b"max_turns: [unterminated\n"
        real_write(path, data)

    monkeypatch.setattr(
        collapse_profile_routing, "_atomic_write_bytes", corrupting_write
    )

    rc = main(["--hermes-home", str(home), "--apply", "--stamp", STAMP])

    assert rc == 3, "a corrupt tree must not exit 0"
    out = capsys.readouterr()
    assert "profiles/reviewer/config.yaml" in out.err
    assert "does NOT re-parse after the write" in out.err
    assert str(backup_root(home, STAMP)) in out.err
    # The per-file line must not read as a clean success either.
    assert "profiles/reviewer/config.yaml: WRITTEN but does NOT re-parse" in out.out
    assert "profiles/reviewer/config.yaml: removed" not in out.out
    # The backup is the recovery path the diagnostic points at, so it has to hold
    # the original.
    restored = backup_root(home, STAMP) / "profiles" / "reviewer" / "config.yaml"
    assert yaml.safe_load(restored.read_text())["model"] == "glm-4.7"


def test_a_write_that_parses_but_is_not_what_was_planned_is_caught(
    tmp_path, capsys, monkeypatch
):
    """Parseability is not enough: the disk must match the INTENT.

    Valid YAML that is not the planned document is the failure "does it still
    parse?" cannot see, and it is the one a de-shadowing script has to rule out —
    a file that parses with `model` still in it has not been collapsed at all.
    """
    home = _write_home(tmp_path)
    real_write = collapse_profile_routing._atomic_write_bytes

    def stale_write(path: Path, data: bytes) -> None:
        if path.parent.name == "coder":
            data = b"model: gpt-5.6-terra\nmax_turns: 40\n"
        real_write(path, data)

    monkeypatch.setattr(collapse_profile_routing, "_atomic_write_bytes", stale_write)

    rc = main(["--hermes-home", str(home), "--apply", "--stamp", STAMP])

    assert rc == 3
    err = capsys.readouterr().err
    assert "profiles/coder/config.yaml" in err
    assert "does not match the planned document" in err


def test_the_re_parse_covers_files_this_run_did_not_write(tmp_path):
    """"Re-parse all 16" means all of them, not only the rewritten ones."""
    home = _write_home(tmp_path)
    assert main(["--hermes-home", str(home), "--apply", "--stamp", STAMP]) == 0

    # `thin` was never written by the run, so `written` says nothing about it;
    # something else breaking it still has to surface.
    (home / "profiles" / "thin" / "config.yaml").write_text("a: [oops\n")

    problems = verify_after_write(home, written={})

    assert len(problems) == 1
    assert problems[0].startswith("profiles/thin/config.yaml:")


def test_a_partial_write_is_still_re_parsed(tmp_path, monkeypatch):
    """A run that stopped halfway is when the tree most needs checking."""
    home = _write_home(tmp_path)
    calls: List[Path] = []
    real_write = collapse_profile_routing._atomic_write_bytes

    def flaky_write(path: Path, data: bytes) -> None:
        calls.append(path)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        real_write(path, data)

    monkeypatch.setattr(collapse_profile_routing, "_atomic_write_bytes", flaky_write)

    report = collapse(home, STAMP, apply=True)

    assert report["applied"] is False
    assert report["failures"], "the write failure is still reported"
    # The half-written tree is loadable, and the verification says so rather than
    # being skipped because the run had already failed.
    assert report["verify_errors"] == []


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
