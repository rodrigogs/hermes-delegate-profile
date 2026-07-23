"""Tests for the Hermes One extension installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import scripts.install_hermes_one_router as installer
from scripts.install_hermes_one_router import install


ROOT = Path(__file__).resolve().parent.parent


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
    assert [entry["id"] for entry in payload["extensions"]] == ["office", "capability-router"]
    router = payload["extensions"][1]
    assert router["scripts"] == ["capability-router/router-nav.js"]
    assert router["stylesheets"] == ["capability-router/router-nav.css"]
    assert router["sidecar"]["proxy_auth"] == "token-v1"

    installed = extension_root / "capability-router"
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


def test_install_replaces_existing_router_entry_without_reordering_others(tmp_path):
    extension_root = tmp_path / "extensions"
    extension_root.mkdir()
    (extension_root / "extensions.json").write_text(
        json.dumps(
            {
                "extensions": [
                    {"id": "first"},
                    {"id": "capability-router", "scripts": ["stale.js"]},
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
    assert [entry["id"] for entry in entries] == ["first", "capability-router", "last"]
    assert entries[1]["scripts"] == ["capability-router/router-nav.js"]


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
    manifest = source_root / "webui_extension/capability-router/manifest.json"
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
    assert "installed capability-router" in capsys.readouterr().out
