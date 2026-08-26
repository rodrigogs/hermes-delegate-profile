"""Tests for the Hermes One extension installer."""

from __future__ import annotations

import sys
import pathlib
import json
from pathlib import Path

import pytest
import scripts.install_hermes_one_router as installer
from scripts.install_hermes_one_router import ProfileHomeRefused, install


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _hermetic_hermes_home(monkeypatch, tmp_path):
    """Pin HOME and clear the HERMES_* environment for every test in this file.

    The flagless install() calls in the older tests resolve hermes_home from
    the environment, so what they asserted depended on who ran the suite: in a
    kanban worker shell HERMES_HOME points at the agent sandbox (measured:
    trama-engineer sets it), and the rendered unit silently carried that path.
    Same disease the installer guard exists for, one level down — a test that
    cannot fail in the environment that matters. Tests that WANT a specific
    inherited value set it after this fixture, and win.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "runner-home"))


def test_install_preserves_manifest_entries_and_is_idempotent(tmp_path):
    extension_root = tmp_path / "extensions"
    extension_root.mkdir()
    root_manifest = extension_root / "extensions.json"
    root_manifest.write_text(
        json.dumps({"extensions": [{"id": "office", "scripts": ["office/app.js"]}]}),
        encoding="utf-8",
    )
    systemd_dir = tmp_path / "systemd"
    plugin_dir = tmp_path / "plugin"
    hermes_home = tmp_path / "hermes-home"
    webui_state_dir = hermes_home / "webui"
    plugin_dir.mkdir()

    install(
        ROOT,
        extension_root,
        systemd_dir,
        plugin_dir,
        hermes_home=hermes_home,
        webui_state_dir=webui_state_dir,
    )
    install(
        ROOT,
        extension_root,
        systemd_dir,
        plugin_dir,
        hermes_home=hermes_home,
        webui_state_dir=webui_state_dir,
    )

    payload = json.loads(root_manifest.read_text(encoding="utf-8"))
    assert [entry["id"] for entry in payload["extensions"]] == ["office", "hermes-one-capability-router"]
    router = payload["extensions"][1]
    assert router["scripts"] == ["hermes-one-capability-router/router-nav.js"]
    assert router["stylesheets"] == ["hermes-one-capability-router/router-nav.css"]
    assert router["sidecar"]["proxy_auth"] == "token-v1"

    installed = extension_root / "hermes-one-capability-router"
    assert (installed / "router-nav.js").is_file()
    assert (installed / "router-nav.css").is_file()
    assert not (installed / "router-nav.js").is_symlink()

    unit = (systemd_dir / "hermes-router-sidecar.service").read_text(encoding="utf-8")
    assert f"WorkingDirectory={plugin_dir}" in unit
    assert f"--config {plugin_dir / 'router.yaml'}" in unit
    assert f"Environment=HERMES_HOME={hermes_home}" in unit
    assert f"Environment=HERMES_WEBUI_STATE_DIR={webui_state_dir}" in unit
    assert "X-Hermes-Sidecar-Token" not in unit
    assert "HERMES_EXT_SIDECAR_TOKEN" not in unit
    assert "127.0.0.1" in unit
    assert "PYTHONDONTWRITEBYTECODE=1" in unit

    # The stale-code poller: a timer runs the check service, which restarts the
    # sidecar when router/*.py on disk is newer than the process start. This is
    # the mechanism that keeps the Aug 2026 lint phantom from recurring.
    check_unit = (systemd_dir / "hermes-router-sidecar-stale-check.service").read_text(
        encoding="utf-8"
    )
    assert f"WorkingDirectory={plugin_dir}" in check_unit
    assert "scripts/sidecar_stale_check.py" in check_unit
    assert f"Environment=HERMES_HOME={hermes_home}" in check_unit
    timer = (systemd_dir / "hermes-router-sidecar-stale-check.timer").read_text(
        encoding="utf-8"
    )
    assert "OnUnitActiveSec=2min" in timer
    assert "@" not in timer


def test_install_replaces_existing_router_entry_without_reordering_others(tmp_path):
    extension_root = tmp_path / "extensions"
    extension_root.mkdir()
    (extension_root / "extensions.json").write_text(
        json.dumps(
            {
                "extensions": [
                    {"id": "first"},
                    {"id": "hermes-one-capability-router", "scripts": ["stale.js"]},
                    {"id": "last"},
                ]
            }
        ),
        encoding="utf-8",
    )
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()

    install(ROOT, extension_root, tmp_path / "systemd", plugin_dir)

    entries = json.loads((extension_root / "extensions.json").read_text(encoding="utf-8"))["extensions"]
    assert [entry["id"] for entry in entries] == ["first", "hermes-one-capability-router", "last"]
    assert entries[1]["scripts"] == ["hermes-one-capability-router/router-nav.js"]


def test_installer_rejects_malformed_inputs_and_missing_templates(tmp_path):
    missing = tmp_path / "missing.json"
    assert installer._read_json(missing) == {"extensions": []}

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="could not read"):
        installer._read_json(malformed)

    wrong_shape = tmp_path / "wrong-shape.json"
    wrong_shape.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="extensions"):
        installer._read_json(wrong_shape)

    with pytest.raises(ValueError, match="could not read extension entry"):
        installer._read_extension_entry(tmp_path)

    source_root = tmp_path / "source"
    manifest = source_root / "webui_extension/hermes-one-capability-router/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"id":"wrong"}', encoding="utf-8")
    with pytest.raises(ValueError, match="must declare"):
        installer._read_extension_entry(source_root)

    with pytest.raises(ValueError, match="assets missing"):
        installer._copy_assets(tmp_path, tmp_path / "destination")

    unit_root = tmp_path / "unit-root"
    unit = unit_root / "systemd/hermes-router-sidecar.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("no placeholder", encoding="utf-8")
    with pytest.raises(ValueError, match="placeholder"):
        installer._render_unit(
            unit_root,
            tmp_path / "plugin",
            tmp_path / "home",
            tmp_path / "home/webui",
        )

    unit.write_text("@NOPE@", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown placeholders"):
        installer._render_unit(
            unit_root,
            tmp_path / "plugin",
            tmp_path / "home",
            tmp_path / "home/webui",
        )

    # The check-service template only needs @PLUGIN_DIR@/@PYTHON@/@HERMES_HOME@ —
    # it must render without the service-only placeholders.
    check_template = unit_root / "systemd/hermes-router-sidecar-stale-check.service"
    check_template.write_text(
        "WorkingDirectory=@PLUGIN_DIR@\nExecStart=@PYTHON@ scripts/sidecar_stale_check.py\n",
        encoding="utf-8",
    )
    rendered = installer._render_unit(
        unit_root,
        tmp_path / "plugin",
        tmp_path / "home",
        tmp_path / "home/webui",
        template_name="hermes-router-sidecar-stale-check.service",
    )
    assert f"WorkingDirectory={tmp_path / 'plugin'}" in rendered
    assert "scripts/sidecar_stale_check.py" in rendered


def test_installer_cli_builds_defaults_and_invokes_install(monkeypatch, tmp_path, capsys):
    parser = installer.build_parser()
    args = parser.parse_args([])
    assert args.extension_root.name == "hermes-one-extensions"
    assert args.systemd_dir.name == "user"
    assert args.plugin_dir.name == "delegate-profile"

    captured = {}
    monkeypatch.setattr(
        installer,
        "install",
        lambda repo_root, extension_root, systemd_dir, plugin_dir, **kwargs: captured.update(
            repo_root=repo_root,
            extension_root=extension_root,
            systemd_dir=systemd_dir,
            plugin_dir=plugin_dir,
            **kwargs,
        ),
    )
    assert installer.main([
        "--extension-root", str(tmp_path / "extensions"),
        "--systemd-dir", str(tmp_path / "systemd"),
        "--plugin-dir", str(tmp_path / "plugin"),
    ]) == 0
    assert captured["extension_root"] == tmp_path / "extensions"
    assert "installed hermes-one-capability-router" in capsys.readouterr().out

def test_unit_execstart_uses_a_python_that_exists(tmp_path):
    """The unit must not hardcode one install's venv layout.

    It did: /usr/local/lib/hermes-agent/venv/bin/python3, which is absent on an
    install that keeps its venv under HERMES_HOME. systemd then failed at ExecStart
    with no hint that the path was the problem, and no test noticed because none
    read ExecStart.
    """
    install(
        ROOT,
        tmp_path / "extensions",
        tmp_path / "systemd",
        tmp_path / "plugin",
        hermes_home=tmp_path / "hermes",
    )
    unit = (tmp_path / "systemd" / "hermes-router-sidecar.service").read_text(encoding="utf-8")
    execstart = next(
        line for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    interpreter = execstart[len("ExecStart="):].split()[0]
    assert interpreter == sys.executable, execstart
    assert pathlib.Path(interpreter).exists(), f"unit points at a missing interpreter: {interpreter}"
    assert "@PYTHON@" not in unit


def test_unit_execstart_honours_an_explicit_python(tmp_path):
    install(
        ROOT,
        tmp_path / "extensions",
        tmp_path / "systemd",
        tmp_path / "plugin",
        hermes_home=tmp_path / "hermes",
        python="/opt/custom/bin/python3",
    )
    unit = (tmp_path / "systemd" / "hermes-router-sidecar.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/custom/bin/python3 -m router.one_sidecar" in unit


def test_install_preserves_operator_unit_dir_under_a_clean_home(monkeypatch, tmp_path):
    """A clean operator install writes units and does not refuse itself.

    Companion to the refusal tests: the guard must keep the ordinary path
    (default `~/.hermes`, no profile involved) working, otherwise it is not a
    guard but a roadblock. Uses monkeypatch, not the live environment: this
    suite runs inside agent shells where HERMES_HOME points at the sandbox
    (measured: the trama-engineer worker has it set), which is exactly the
    environment the guard exists to catch — an ordinary-path test must not
    depend on which side of the fence the runner sits on.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # tmp_path + ".config/systemd/user": the operator's unit-directory shape,
    # reproducible without touching any real directory.
    systemd_dir = tmp_path / ".config/systemd/user"
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()

    install(ROOT, tmp_path / "extensions", systemd_dir, plugin_dir)

    assert (systemd_dir / "hermes-router-sidecar.service").is_file()
    unit = (systemd_dir / "hermes-router-sidecar.service").read_text(encoding="utf-8")
    # _default_hermes_home() resolved from the patched HOME: the unit that a
    # clean operator install produces, unchanged by the guard.
    assert f"Environment=HERMES_HOME={tmp_path / '.hermes'}" in unit


def test_install_refuses_inherited_profile_home_in_operator_unit_dir(
    monkeypatch, tmp_path
):
    """HERMES_HOME from an agent profile + operator unit dir => hard refusal.

    This is the 2026-08-26 incident as a test: the worker's environment carried
    HERMES_HOME under .hermes/profiles/, the flags were missing, and the unit
    was written anyway. The refusal must (a) raise, (b) name the path that
    would be written, (c) name the unit directory, (d) name both ways out.
    Nothing may be written — not the units, and not the extension bundle
    either, which did not exist before the guard.
    """
    profile_home = tmp_path / "agent-home/.hermes/profiles/some-worker"
    systemd_dir = tmp_path / "operator/.config/systemd/user"
    extension_root = tmp_path / "extensions"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)

    with pytest.raises(ProfileHomeRefused) as excinfo:
        install(ROOT, extension_root, systemd_dir, tmp_path / "plugin")

    message = str(excinfo.value)
    assert str(profile_home) in message
    assert str(systemd_dir) in message
    assert "--hermes-home" in message and "--webui-state-dir" in message
    assert "--allow-profile-hermes-home" in message
    assert not (systemd_dir / "hermes-router-sidecar.service").exists()
    assert not extension_root.exists()


def test_install_refuses_inherited_webui_state_dir_from_profile(monkeypatch, tmp_path):
    """HERMES_WEBUI_STATE_DIR alone must trip the same guard.

    The incident leaked BOTH variables, but each is its own failure surface:
    this one alone is what made /status answer 503 while /health said ok. It
    is inherited via environment (flag absent), while HERMES_HOME here is a
    clean operator default, so the refusal isolates the state dir.
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setenv(
        "HERMES_WEBUI_STATE_DIR", str(tmp_path / "agent/.hermes/profiles/w/webui")
    )
    systemd_dir = tmp_path / "operator/.config/systemd/user"

    with pytest.raises(ProfileHomeRefused) as excinfo:
        install(
            ROOT,
            tmp_path / "extensions",
            systemd_dir,
            tmp_path / "plugin",
        )

    message = str(excinfo.value)
    assert str(tmp_path / "agent/.hermes/profiles/w/webui") in message
    assert str(systemd_dir) in message
    assert "--allow-profile-hermes-home" in message
    assert not systemd_dir.exists()


def test_install_explicit_flags_bypass_the_guard_for_any_value(monkeypatch, tmp_path):
    """Explicit --hermes-home/--webui-state-dir always install.

    The guard is about INHERITING, not about the value: a caller who spells
    the profile path out on the command line can be held to it. The card's
    second acceptance case.
    """
    profile_home = tmp_path / "agent/.hermes/profiles/deliberate"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "unrelated-inherited"))
    monkeypatch.setenv(
        "HERMES_WEBUI_STATE_DIR", str(tmp_path / "other-inherited/webui")
    )
    systemd_dir = tmp_path / "operator/.config/systemd/user"

    install(
        ROOT,
        tmp_path / "extensions",
        systemd_dir,
        tmp_path / "plugin",
        hermes_home=profile_home,
        webui_state_dir=profile_home / "webui",
    )

    unit = (systemd_dir / "hermes-router-sidecar.service").read_text(encoding="utf-8")
    assert f"Environment=HERMES_HOME={profile_home}" in unit
    assert f"Environment=HERMES_WEBUI_STATE_DIR={profile_home / 'webui'}" in unit


def test_install_escape_hatch_allows_the_inherited_profile_home(monkeypatch, tmp_path):
    """--allow-profile-hermes-home installs with the inherited values.

    The card's third acceptance case: the escape hatch is explicit, and the
    unit bakes exactly the inherited paths (proving nothing was silently
    swapped to a different default).
    """
    profile_home = tmp_path / "agent-home/.hermes/profiles/wanted"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    systemd_dir = tmp_path / "operator/.config/systemd/user"

    install(
        ROOT,
        tmp_path / "extensions",
        systemd_dir,
        tmp_path / "plugin",
        allow_profile_hermes_home=True,
    )

    unit = (systemd_dir / "hermes-router-sidecar.service").read_text(encoding="utf-8")
    assert f"Environment=HERMES_HOME={profile_home}" in unit
    assert f"Environment=HERMES_WEBUI_STATE_DIR={profile_home / 'webui'}" in unit


def test_install_profile_paths_only_refuse_in_operator_unit_dir(monkeypatch, tmp_path):
    """A throwaway systemd dir takes the inherited profile home in stride.

    The guard is scoped to production units. A scratch dir (e.g. a test or a
    dry run into tmp) is not the operator's systemd, so refusing there would
    be noise, and noise trains operators to reach for the escape hatch.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes/profiles/x"))
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)
    systemd_dir = tmp_path / "scratch-units"

    install(ROOT, tmp_path / "extensions", systemd_dir, tmp_path / "plugin")

    unit = (systemd_dir / "hermes-router-sidecar.service").read_text(encoding="utf-8")
    assert f"Environment=HERMES_HOME={tmp_path / '.hermes/profiles/x'}" in unit


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("/home/u/.hermes/profiles/coder", True),
        ("/home/u/.hermes/profiles/a/b/c", True),
        ("/tmp/x/.hermes/profiles", False),  # 'profiles' must sit UNDER .hermes, not be it
        ("/home/u/.hermes", False),
        ("/home/u/hermes/profiles/coder", False),  # missing the dot: not the marker
        ("/home/u/.hermes/plugins/delegate-profile", False),
        ("/tmp/pytest-123/.hermes/profiles/trama-engineer", True),  # tmp_path has the same shape
    ],
)
def test_under_agent_profile_measures_the_path_shape(candidate, expected):
    """The 'came from a profile' predicate, measured on path shape alone.

    The card is explicit: the test is the PATH that would be written, not the
    environment. These cases pin the predicate so a future edit cannot loosen
    it (e.g. matching `profiles` anywhere, which would refuse legitimate
    /home/u/profiles work dirs) nor tighten it into refusing plain ~/.hermes.
    """
    assert installer._under_agent_profile(Path(candidate)) is expected


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("/home/u/.config/systemd/user", True),
        ("/tmp/pytest-9/.config/systemd/user", True),
        ("/home/u/.config/systemd", False),
        ("/home/u/opt/units", False),
    ],
)
def test_is_operator_unit_dir_matches_the_suffix(candidate, expected):
    """Operator unit dir by shape, so tmp_path can stand in for the real one."""
    assert installer._is_operator_unit_dir(Path(candidate)) is expected


def test_cli_refusal_names_the_three_facts_and_exits_nonzero(monkeypatch, tmp_path, capsys):
    """End to end through main(): exit != 0, message on stderr, three facts.

    main() is what router-deploy.sh calls; the refusal is only real if it
    survives the CLI boundary as a legible error rather than a traceback.
    """
    profile_home = tmp_path / "agent/.hermes/profiles/cli-case"
    systemd_dir = tmp_path / "operator/.config/systemd/user"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_WEBUI_STATE_DIR", raising=False)

    code = installer.main([
        "--extension-root", str(tmp_path / "extensions"),
        "--systemd-dir", str(systemd_dir),
        "--plugin-dir", str(tmp_path / "plugin"),
    ])

    assert code != 0
    err = capsys.readouterr().err
    assert str(profile_home) in err
    assert str(systemd_dir) in err
    assert "--hermes-home" in err and "--webui-state-dir" in err
    assert "--allow-profile-hermes-home" in err
    assert not systemd_dir.exists()
