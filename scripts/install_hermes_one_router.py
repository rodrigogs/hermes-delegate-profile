#!/usr/bin/env python3
"""Install Capability Router assets into a Hermes One extension bundle.

The installer is deliberately narrow and idempotent:

* copies versioned assets, never symlinks (WebUI rejects escaping symlinks);
* replaces only the ``capability-router`` entry in ``extensions.json``;
* preserves every sibling entry and its ordering (for example Office 3D);
* renders a loopback-only systemd user unit pointing at the effective plugin
  installation; and
* does not start services or grant WebUI proxy consent. Those are explicit
  operator actions because consent creates the token-v1 credential boundary.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

EXTENSION_ID = "capability-router"


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"extensions": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read extension manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("extensions"), list):
        raise ValueError(f"extension manifest {path} must contain an 'extensions' list")
    return data


def _read_extension_entry(repo_root: Path) -> Dict[str, Any]:
    path = repo_root / "webui_extension" / EXTENSION_ID / "manifest.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read extension entry {path}: {exc}") from exc
    if not isinstance(entry, dict) or entry.get("id") != EXTENSION_ID:
        raise ValueError(f"extension entry {path} must declare id '{EXTENSION_ID}'")
    return entry


def _bundle_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Prefix per-extension relative assets for the root bundle manifest."""
    result = dict(entry)
    for key in ("scripts", "stylesheets"):
        paths = entry.get(key, [])
        result[key] = [f"{EXTENSION_ID}/{path}" for path in paths]
    return result


def _merge_entry(manifest: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    """Replace our entry in place; append only when it is new."""
    entries = list(manifest["extensions"])
    for index, candidate in enumerate(entries):
        if isinstance(candidate, dict) and candidate.get("id") == EXTENSION_ID:
            entries[index] = entry
            break
    else:
        entries.append(entry)
    manifest["extensions"] = entries
    return manifest


def _copy_assets(repo_root: Path, extension_root: Path) -> None:
    source = repo_root / "webui_extension" / EXTENSION_ID
    if not source.is_dir():
        raise ValueError(f"extension assets missing: {source}")
    destination = extension_root / EXTENSION_ID
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _render_unit(
    repo_root: Path,
    plugin_dir: Path,
    hermes_home: Path,
    webui_state_dir: Path,
) -> str:
    template = (repo_root / "systemd" / "hermes-router-sidecar.service").read_text(
        encoding="utf-8"
    )
    replacements = {
        "@PLUGIN_DIR@": str(plugin_dir),
        "@HERMES_HOME@": str(hermes_home),
        "@WEBUI_STATE_DIR@": str(webui_state_dir),
    }
    missing = [placeholder for placeholder in replacements if placeholder not in template]
    if missing:
        raise ValueError(f"sidecar unit template lacks placeholders: {', '.join(missing)}")
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def _default_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured) if configured else Path.home() / ".hermes"


def install(
    repo_root: Path,
    extension_root: Path,
    systemd_dir: Path,
    plugin_dir: Path,
    *,
    hermes_home: Path | None = None,
    webui_state_dir: Path | None = None,
) -> None:
    """Copy assets, merge manifest and render the systemd unit atomically enough.

    No process is restarted and no consent/token state is touched.
    """
    repo_root = Path(repo_root)
    extension_root = Path(extension_root)
    systemd_dir = Path(systemd_dir)
    plugin_dir = Path(plugin_dir)
    effective_hermes_home = Path(hermes_home) if hermes_home else _default_hermes_home()
    configured_state_dir = os.environ.get("HERMES_WEBUI_STATE_DIR")
    effective_webui_state_dir = (
        Path(webui_state_dir)
        if webui_state_dir
        else Path(configured_state_dir) if configured_state_dir else effective_hermes_home / "webui"
    )

    entry = _bundle_entry(_read_extension_entry(repo_root))
    manifest_path = extension_root / "extensions.json"
    manifest = _merge_entry(_read_json(manifest_path), entry)

    extension_root.mkdir(parents=True, exist_ok=True)
    _copy_assets(repo_root, extension_root)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "hermes-router-sidecar.service").write_text(
        _render_unit(repo_root, plugin_dir, effective_hermes_home, effective_webui_state_dir),
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-root", type=Path, default=Path.home() / "hermes-one-extensions")
    parser.add_argument("--systemd-dir", type=Path, default=Path.home() / ".config/systemd/user")
    parser.add_argument("--plugin-dir", type=Path, default=Path.home() / ".hermes/plugins/delegate-profile")
    parser.add_argument("--hermes-home", type=Path, default=None)
    parser.add_argument("--webui-state-dir", type=Path, default=None)
    parser.set_defaults(repo_root=repo_root)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    install(
        args.repo_root,
        args.extension_root,
        args.systemd_dir,
        args.plugin_dir,
        hermes_home=args.hermes_home,
        webui_state_dir=args.webui_state_dir,
    )
    print(f"installed {EXTENSION_ID} extension into {args.extension_root}")
    print("next: daemon-reload/start sidecar, reload Hermes One, approve token-v1 proxy in Settings → Extensions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
