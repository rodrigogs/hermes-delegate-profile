"""Which files this plugin reads and writes — the resolution, not the contents.

`router/paths.py` exists because a path was typed twice and the two copies diverged for
four weeks. `resolve_policy_path` is the third reader of that idea and was added for the
same reason: the plugin and the sidecar each decided which `router.yaml` to use, and on
the docker stack they picked DIFFERENT FILES.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from router.paths import POLICY_ENV, hermes_root, resolve_policy_path, state_dir


@pytest.fixture(autouse=True)
def _no_inherited_policy_override(monkeypatch):
    """The suite must not answer from whatever the operator's shell exports."""
    monkeypatch.delenv(POLICY_ENV, raising=False)


def test_the_rooted_policy_wins_when_it_exists(tmp_path, monkeypatch):
    """The docker layout: the sidecar is pointed at HERMES_HOME/router.yaml.

    Measured 2026-09-04, minutes after the plugin first loaded there: the plugin read
    `<plugin_dir>/router.yaml` — a symlink into the image's source clone, where no policy
    existed — seeded one from `router.example.yaml`, and routed on it. Five trace entries
    named `gpt-5.6-terra/openai-codex`, `glm-5.3-flash/zai` and `mimo-v2.5/xiaomi`: the
    example's rails, none reachable on that install, none in the policy the operator was
    editing through the console.
    """
    home = tmp_path / "home"
    home.mkdir()
    plugin_dir = tmp_path / "plug"
    plugin_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    rooted = home / "router.yaml"
    rooted.write_text("enabled: true\n", encoding="utf-8")

    assert resolve_policy_path(plugin_dir) == rooted
    # Non-vacuity: the plugin-dir candidate also exists, so this is a real precedence
    # decision and not "the only file there was".
    (plugin_dir / "router.yaml").write_text("enabled: false\n", encoding="utf-8")
    assert resolve_policy_path(plugin_dir) == rooted


def test_the_plugin_directory_answers_when_the_root_has_no_policy(tmp_path, monkeypatch):
    """The WSL layout, which must stay byte-for-byte unaffected.

    Measured there: `~/.hermes/router.yaml` does not exist, the operator's policy is
    `~/.hermes/plugins/hermes-smart-router/router.yaml` (3395 bytes), and the sidecar
    unit passes exactly that path with `--config`. So the rooted rung must MISS and this
    one must answer, or the fix for one install would break the other.
    """
    home = tmp_path / "home"
    home.mkdir()
    plugin_dir = tmp_path / "plug"
    plugin_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    operator = plugin_dir / "router.yaml"
    operator.write_text("enabled: true\n", encoding="utf-8")

    assert not (home / "router.yaml").exists(), "non-vacuity: the rooted rung must miss"
    assert resolve_policy_path(plugin_dir) == operator


def test_neither_present_still_names_the_seed_target(tmp_path, monkeypatch):
    """First run: the documented behaviour is to seed from the tracked example.

    So a resolution with nothing on disk must still name a WRITABLE path in the plugin
    directory, or the seed has nowhere to land.
    """
    home = tmp_path / "home"
    home.mkdir()
    plugin_dir = tmp_path / "plug"
    plugin_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    resolved = resolve_policy_path(plugin_dir)
    assert resolved == plugin_dir / "router.yaml"
    assert not resolved.exists(), "it names where to write, not something already there"


def test_the_explicit_override_wins_even_over_an_existing_rooted_policy(tmp_path, monkeypatch):
    """Explicit beats both rungs, and does NOT require the file to exist.

    Same shape as `HERMES_CORE_CONFIG_FILE` and `HERMES_ROUTE_TRACE_FILE`. Not requiring
    existence is what lets a test or a unit point at a file it is about to create — if
    this checked `.exists()` the override would silently fall through to another policy,
    which is the exact failure mode this whole function was added to end.
    """
    home = tmp_path / "home"
    home.mkdir()
    plugin_dir = tmp_path / "plug"
    plugin_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "router.yaml").write_text("enabled: true\n", encoding="utf-8")
    (plugin_dir / "router.yaml").write_text("enabled: false\n", encoding="utf-8")

    chosen = tmp_path / "somewhere" / "explicit.yaml"
    monkeypatch.setenv(POLICY_ENV, str(chosen))
    assert resolve_policy_path(plugin_dir) == chosen
    assert not chosen.exists(), "existence is deliberately not a condition"


def test_the_policy_and_the_state_dir_agree_about_the_root(tmp_path, monkeypatch):
    """One root, every derived path. The profile peel applies to both.

    `hermes_root` peels a trailing `profiles/<name>` so every profile converges on one
    directory; a policy resolved under the profile would split the routing decision from
    the breaker state and the trace that record it.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "coder").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "coder"))
    (root / "router.yaml").write_text("enabled: true\n", encoding="utf-8")

    assert hermes_root() == root, "non-vacuity: the peel really did fire"
    assert resolve_policy_path(tmp_path / "plug") == root / "router.yaml"
    assert state_dir().parent.parent == root, "and state hangs off the same root"


def test_the_plugin_and_the_sidecar_resolve_THE_SAME_policy(tmp_path, monkeypatch):
    """The defect, stated as the property it violated: one install, one policy.

    The plugin read `<plugin_dir>/router.yaml` and the sidecar defaulted to
    `parent.parent / "router.yaml"` — two independent answers to one question. On the WSL
    layout they coincide; on the docker stack they did not, and the console edited one
    document while dispatch obeyed another.

    Asserted through the two real entry points rather than by re-deriving the path, so a
    future change to either has to keep them equal.
    """
    import router.one_sidecar as sidecar

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "router.yaml").write_text("enabled: true\n", encoding="utf-8")

    # The sidecar's own parser default, as a bare start would compute it.
    sidecar_default = sidecar.build_parser().get_default("config")
    # The plugin's resolution, from the module directory the plugin lives in.
    plugin_choice = resolve_policy_path(Path(sidecar.__file__).resolve().parent.parent)

    assert Path(sidecar_default) == Path(plugin_choice)
    assert Path(sidecar_default) == home / "router.yaml", (
        "non-vacuity: with a rooted policy present, BOTH must move to it — if this were "
        "the plugin directory the test would pass while proving nothing"
    )
