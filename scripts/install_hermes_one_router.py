#!/usr/bin/env python3
"""Install Smart Router assets into a Hermes One extension bundle.

The installer is deliberately narrow and idempotent:

* copies versioned assets, never symlinks (WebUI rejects escaping symlinks);
* replaces only the ``hermes-smart-router`` entry in ``extensions.json``, and
  sweeps retired ids (``_RETIRED_EXTENSION_IDS``) out of the bundle — a merge
  keyed on the CURRENT id never matches the old entry, and an unswept one
  leaves a second, dead nav button beside the live one;
* preserves every sibling entry and its ordering (for example Office 3D);
* refuses to render production units that would bake an agent profile's
  ``HERMES_HOME``/``HERMES_WEBUI_STATE_DIR`` into the unit (see
  ``ProfileHomeRefused``);
* renders a loopback-only systemd user unit pointing at the effective plugin
  installation; and
* does not start services or grant WebUI proxy consent. Those are explicit
  operator actions because consent creates the token-v1 credential boundary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

EXTENSION_ID = "hermes-smart-router"

# Ids this installer superseded. The 2026-08-26 rename (hermes-one-capability-
# router -> hermes-smart-router) is the reason this exists: the manifest merge
# replaces the entry whose id matches EXTENSION_ID, so the old entry under the
# old id would survive every install — two nav buttons, one of them pointing at
# a consent that no longer exists. Swept from extensions.json AND from disk.
_RETIRED_EXTENSION_IDS = ("hermes-one-capability-router",)

# The unit directory of the *operator* — the one systemd --user loads at boot.
# Compared by SHAPE (suffix match), not against Path.home(): in the 2026-08-26
# incident the installer ran inside an agent shell whose HOME was remapped to
# the profile sandbox, so "is this the default dir" answered False for the very
# directory that mattered. The shape `.config/systemd/user` cannot be spoofed
# into existence by an environment variable.
_UNIT_DIR_SUFFIX = (".config", "systemd", "user")


class ProfileHomeRefused(ValueError):
    """A production unit would have baked an agent profile's paths into itself.

    Raised when an inherited HERMES_HOME/HERMES_WEBUI_STATE_DIR resolves under
    an agent profile (a `profiles` component below `.hermes`) and the units are
    headed for the operator's systemd user directory. Measured 2026-08-26: such
    a unit served /health "ok" for hours while every tokened route answered
    503 "sidecar token not provisioned" and the trace log read from a path that
    did not exist. Explicit flags, or --allow-profile-hermes-home, are the ways
    out; the guard is about INHERITING, never about the value itself.
    """


def _is_operator_unit_dir(systemd_dir: Path) -> bool:
    """True when units land in the directory the operator's systemd loads."""
    return systemd_dir.parts[-3:] == _UNIT_DIR_SUFFIX


def _under_agent_profile(path: Path) -> bool:
    """True when a `profiles` component sits directly under a `.hermes` one.

    The `profiles` component must itself have something under it: the profile
    home is `.hermes/profiles/<name>...`, and a path ending AT `profiles` names
    the container, not a profile — refusing it would be noise.
    """
    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index] == ".hermes" and parts[index + 1] == "profiles":
            return True
    return False



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
    """Replace our entry in place; append only when it is new.

    Retired ids are dropped: their entries can never be matched by the
    EXTENSION_ID-keyed replace, so keeping them would leave a second nav
    button wired to a nonexistent consent/token after a rename.
    """
    entries = [
        candidate
        for candidate in manifest["extensions"]
        if not (
            isinstance(candidate, dict)
            and candidate.get("id") in _RETIRED_EXTENSION_IDS
        )
    ]
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
    _sweep_retired(extension_root)


def _sweep_retired(*roots: Path) -> None:
    """Remove every retired id's asset directory from each of ``roots``.

    The manifest entry removal alone would leave the old asset directory behind,
    serving stale bytes to anything that still walks the tree.

    TWO ROOTS, not one. This ran over the bundle only, and the SERVED PLUGIN COPY
    kept its own copy of the retired directory for sixteen days after the rename —
    found on the reference install 2026-09-02:
    ``…/plugins/hermes-smart-router/webui_extension/hermes-one-capability-router``
    still held DESIGN.md, a 492KB console.html and a manifest.json declaring the OLD
    id. No deploy could ever have removed it: ``router-deploy.sh`` syncs that copy
    with ``git archive HEAD | tar -x``, which only ADDS, and its drift check asks
    only whether tracked files MATCH — never whether extra ones exist. So a file
    tracked once and later deleted survives in the served copy forever.
    """
    for root in roots:
        for retired in _RETIRED_EXTENSION_IDS:
            retired_dir = Path(root) / retired
            if retired_dir.exists():
                shutil.rmtree(retired_dir)


def _default_python() -> str:
    """The interpreter the unit should run.

    The template used to hardcode /usr/local/lib/hermes-agent/venv, which is one
    install's layout: on an install that keeps its venv under HERMES_HOME the unit
    pointed at a path that does not exist and the sidecar failed at ExecStart. Use
    the interpreter running this installer, which is by construction the one whose
    environment has the router module importable.
    """
    return sys.executable


#: What each unit template MUST reference, per unit. Checked by :func:`_render_unit`
#: in addition to the unknown-placeholder check, because only the unknown direction
#: was guarded and the missing direction is the one that shipped broken.
#:
#: The stale-check unit needs ``@WEBUI_STATE_DIR@`` because the poller resolves the
#: sidecar token through ``one_sidecar.resolve_token_path``, whose second rung reads
#: ``HERMES_WEBUI_STATE_DIR`` — the same variable the sidecar's own unit sets. Its
#: absence is not cosmetic: the poller then reads a token the sidecar does not
#: authorise with and cannot tell that from "the service is down".
_REQUIRED_PLACEHOLDERS: dict[str, set[str]] = {
    "hermes-router-sidecar.service": {
        "@PLUGIN_DIR@", "@HERMES_HOME@", "@WEBUI_STATE_DIR@", "@PYTHON@",
    },
    "hermes-router-sidecar-stale-check.service": {
        "@PLUGIN_DIR@", "@HERMES_HOME@", "@WEBUI_STATE_DIR@", "@PYTHON@",
    },
}


def _render_unit(
    repo_root: Path,
    plugin_dir: Path,
    hermes_home: Path,
    webui_state_dir: Path,
    python: str | None = None,
    template_name: str = "hermes-router-sidecar.service",
) -> str:
    template = (repo_root / "systemd" / template_name).read_text(
        encoding="utf-8"
    )
    replacements = {
        "@PLUGIN_DIR@": str(plugin_dir),
        "@HERMES_HOME@": str(hermes_home),
        "@WEBUI_STATE_DIR@": str(webui_state_dir),
        "@PYTHON@": python or _default_python(),
    }
    tokens = re.findall(r"@[A-Z_]+@", template)
    if not tokens:
        raise ValueError(f"{template_name} template lacks placeholders")
    unknown = [token for token in tokens if token not in replacements]
    if unknown:
        raise ValueError(
            f"{template_name} template has unknown placeholders: {', '.join(unknown)}"
        )
    # ...and the other direction, which is the one that actually bit. Only ONE
    # direction was checked, so a template that FORGOT a placeholder rendered a unit
    # that looked complete: the stale-check unit was missing `@WEBUI_STATE_DIR@`
    # while this function was already handed the value, so the poller's token ladder
    # silently fell to a different rung, read a file the sidecar does not authorise
    # with, and reported "sidecar down" forever.
    #
    # Per-template, NOT "every value must be consumed": the two units legitimately
    # need different subsets, and a blanket rule would forbid the smaller one. What
    # each unit REQUIRES is a contract, so it is written down.
    required = _REQUIRED_PLACEHOLDERS.get(template_name)
    if required is not None:
        missing = sorted(required - set(tokens))
        if missing:
            raise ValueError(
                f"{template_name} must use {', '.join(missing)} — the installer "
                f"computes these, and a template that drops one renders a unit whose "
                f"environment is silently incomplete"
            )
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def _default_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured) if configured else Path.home() / ".hermes"


def _refuse_inherited_profile_home(
    systemd_dir: Path,
    hermes_home: Path,
    webui_state_dir: Path,
    *,
    hermes_home_flag: Path | None,
    webui_state_dir_flag: Path | None,
) -> None:
    """Refuse a production unit that would inherit an agent profile's paths.

    Two conditions, both required — the unit is headed for the operator's
    systemd user directory, and an *inherited* effective path sits under an
    agent profile. Explicit flags always win because the danger is not the
    value but the silence of where it came from: someone who wrote the path
    down can be held to it, an environment variable nobody read cannot. The
    escape hatch exists for the rare deliberate case, and pays for itself by
    being named on the command line of the deploy that used it.
    """
    inherited: list[tuple[str, Path]] = []
    if hermes_home_flag is None and _under_agent_profile(hermes_home):
        inherited.append(("HERMES_HOME", hermes_home))
    if webui_state_dir_flag is None and _under_agent_profile(webui_state_dir):
        inherited.append(("HERMES_WEBUI_STATE_DIR", webui_state_dir))
    if not inherited:
        return
    facts = "\n".join(
        f"  Environment={name}={path}  <- inherited, not passed as a flag"
        for name, path in inherited
    )
    raise ProfileHomeRefused(
        "refusing to write a production systemd unit that bakes an agent "
        f"profile's paths into it. Unit directory: {systemd_dir}. Paths that "
        f"would be written:\n{facts}\n"
        "Fix: pass --hermes-home and --webui-state-dir explicitly (always "
        "allowed, any value), or --allow-profile-hermes-home if this profile "
        "path is genuinely what you want. Measured 2026-08-26: such a unit "
        "kept /health answering ok for hours while every tokened route "
        "returned 503 and the trace log was read from a path that did not "
        "exist."
    )


def install(
    repo_root: Path,
    extension_root: Path,
    systemd_dir: Path,
    plugin_dir: Path,
    *,
    hermes_home: Path | None = None,
    webui_state_dir: Path | None = None,
    python: str | None = None,
    allow_profile_hermes_home: bool = False,
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
    if not allow_profile_hermes_home and _is_operator_unit_dir(systemd_dir):
        _refuse_inherited_profile_home(
            systemd_dir,
            effective_hermes_home,
            effective_webui_state_dir,
            hermes_home_flag=hermes_home,
            webui_state_dir_flag=webui_state_dir,
        )

    entry = _bundle_entry(_read_extension_entry(repo_root))
    manifest_path = extension_root / "extensions.json"
    manifest = _merge_entry(_read_json(manifest_path), entry)

    extension_root.mkdir(parents=True, exist_ok=True)
    _copy_assets(repo_root, extension_root)
    # And out of the served plugin's own copy of webui_extension/, which no deploy
    # can prune (see _sweep_retired). The plugin dir is already a parameter of this
    # call, so this needs nothing the installer was not already given.
    _sweep_retired(plugin_dir / "webui_extension")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "hermes-router-sidecar.service").write_text(
        _render_unit(
            repo_root,
            plugin_dir,
            effective_hermes_home,
            effective_webui_state_dir,
            python=python,
        ),
        encoding="utf-8",
    )
    # Stale-code poller: a timer runs the check script and restarts the sidecar
    # when router/*.py on disk is newer than the process start. This replaces
    # the .path-unit approach, which never armed its inotify fd on this WSL box.
    (systemd_dir / "hermes-router-sidecar-stale-check.service").write_text(
        _render_unit(
            repo_root,
            plugin_dir,
            effective_hermes_home,
            effective_webui_state_dir,
            python=python,
            template_name="hermes-router-sidecar-stale-check.service",
        ),
        encoding="utf-8",
    )
    # The timer has no placeholders; copy it verbatim rather than pretending it
    # needs rendering.
    shutil.copy2(
        repo_root / "systemd" / "hermes-router-sidecar-stale-check.timer",
        systemd_dir / "hermes-router-sidecar-stale-check.timer",
    )


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-root", type=Path, default=Path.home() / "hermes-one-extensions")
    parser.add_argument("--systemd-dir", type=Path, default=Path.home() / ".config/systemd/user")
    parser.add_argument("--plugin-dir", type=Path, default=Path.home() / ".hermes/plugins/hermes-smart-router")
    parser.add_argument("--hermes-home", type=Path, default=None)
    parser.add_argument("--webui-state-dir", type=Path, default=None)
    parser.add_argument(
        "--python",
        default=None,
        help="Interpreter for the unit's ExecStart (default: the one running this script).",
    )
    parser.add_argument(
        "--allow-profile-hermes-home",
        action="store_true",
        help=(
            "Escape hatch: let inherited HERMES_HOME/HERMES_WEBUI_STATE_DIR that "
            "sit under an agent profile (.hermes/profiles/...) be written into "
            "the operator's systemd unit. Only for a deliberate profile-scoped "
            "install; the default is to refuse."
        ),
    )
    parser.set_defaults(repo_root=repo_root)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        install(
            args.repo_root,
            args.extension_root,
            args.systemd_dir,
            args.plugin_dir,
            hermes_home=args.hermes_home,
            webui_state_dir=args.webui_state_dir,
            python=args.python,
            allow_profile_hermes_home=args.allow_profile_hermes_home,
        )
    except ProfileHomeRefused as exc:
        # An uncaught exception would print a traceback and exit 2 anyway, but
        # the operator reading a terminal deserves the three facts without the
        # stack noise. Still a hard failure — the incident lasted hours because
        # every signal looked healthy, so this must be impossible to skim past.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"installed {EXTENSION_ID} extension into {args.extension_root}")
    print("next: daemon-reload/start sidecar, reload Hermes One, approve token-v1 proxy in Settings → Extensions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
