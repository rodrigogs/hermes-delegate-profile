#!/usr/bin/env python3
"""Install the transactional Hermes stack updater as a systemd user timer.

The updater itself is copied into the effective delegate-profile installation so
its future plugin updates carry the controller forward. Installation does not
run an update. With ``--enable``, systemd schedules the next weekly run only.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


SERVICE_NAME = "hermes-stack-update.service"
TIMER_NAME = "hermes-stack-update.timer"


SERVICE_TEMPLATE = """[Unit]
Description=Transactional Hermes Agent and Hermes One updater
Documentation=https://github.com/rodrigogs/hermes-smart-router
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=@PLUGIN_DIR@
Environment=HERMES_PROFILE=@PROFILE@
Environment=HERMES_AGENT_DIR=@CORE_DIR@
Environment=HERMES_PLUGIN_DIR=@PLUGIN_DIR@
Environment=HERMES_ONE_DIR=@ONE_DIR@
Environment=HERMES_ONE_EXTENSIONS_DIR=@EXTENSIONS_DIR@
Environment=HERMES_SYSTEMD_USER_DIR=@SYSTEMD_DIR@
ExecStart=@PYTHON@ @SCRIPT@ --profile @PROFILE@ apply --yes
"""


TIMER_TEMPLATE = """[Unit]
Description=Weekly transactional Hermes stack update

[Timer]
# Saturday avoids triggering immediately when this timer is first enabled on a
# Monday after a missed weekly schedule. Persistent=true catches real downtime.
OnCalendar=Sat *-*-* 04:15:00
RandomizedDelaySec=30m
Persistent=true
Unit=hermes-stack-update.service

[Install]
WantedBy=timers.target
"""


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)


def render_service(
    *,
    python: Path,
    script: Path,
    profile: str,
    core: Path,
    plugin: Path,
    one: Path,
    extensions: Path,
    systemd_dir: Path,
) -> str:
    values = {
        "@PYTHON@": str(python),
        "@SCRIPT@": str(script),
        "@PROFILE@": profile,
        "@CORE_DIR@": str(core),
        "@PLUGIN_DIR@": str(plugin),
        "@ONE_DIR@": str(one),
        "@EXTENSIONS_DIR@": str(extensions),
        "@SYSTEMD_DIR@": str(systemd_dir),
    }
    rendered = SERVICE_TEMPLATE
    for placeholder, value in values.items():
        if placeholder not in rendered:
            raise ValueError(f"service template missing {placeholder}")
        rendered = rendered.replace(placeholder, value)
    return rendered


def install(
    *,
    source_script: Path,
    plugin: Path,
    systemd_dir: Path,
    python: Path,
    profile: str,
    core: Path,
    one: Path,
    extensions: Path,
) -> tuple[Path, Path, Path]:
    if not source_script.is_file():
        raise ValueError(f"updater source script not found: {source_script}")
    if not plugin.is_dir():
        raise ValueError(f"delegate-profile installation not found: {plugin}")

    installed_script = plugin / "scripts" / source_script.name
    installed_script.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(installed_script, source_script.read_text(encoding="utf-8"), 0o750)

    service_path = systemd_dir / SERVICE_NAME
    timer_path = systemd_dir / TIMER_NAME
    atomic_write(
        service_path,
        render_service(
            python=python,
            script=installed_script,
            profile=profile,
            core=core,
            plugin=plugin,
            one=one,
            extensions=extensions,
            systemd_dir=systemd_dir,
        ),
        0o644,
    )
    atomic_write(timer_path, TIMER_TEMPLATE, 0o644)
    return installed_script, service_path, timer_path


def systemctl(*args: str) -> None:
    env = os.environ.copy()
    runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    subprocess.run(["systemctl", "--user", *args], check=True, env=env)


def build_parser() -> argparse.ArgumentParser:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).with_name("update_hermes_stack.py"))
    parser.add_argument("--profile", default=os.environ.get("HERMES_PROFILE", "rodrigo"))
    parser.add_argument("--core", type=Path, default=Path(os.environ.get("HERMES_AGENT_DIR", "/usr/local/lib/hermes-agent")))
    parser.add_argument("--plugin", type=Path, default=Path(os.environ.get("HERMES_PLUGIN_DIR", home / ".hermes/plugins/hermes-smart-router")))
    parser.add_argument("--one", type=Path, default=Path(os.environ.get("HERMES_ONE_DIR", home / "hermes-webui")))
    parser.add_argument("--extensions", type=Path, default=Path(os.environ.get("HERMES_ONE_EXTENSIONS_DIR", home / "hermes-one-extensions")))
    parser.add_argument("--systemd-dir", type=Path, default=Path(os.environ.get("HERMES_SYSTEMD_USER_DIR", home / ".config/systemd/user")))
    parser.add_argument("--python", type=Path, default=Path("/usr/local/lib/hermes-agent/venv/bin/python3"))
    parser.add_argument("--enable", action="store_true", help="enable and start the weekly timer")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        installed, service, timer = install(
            source_script=args.source,
            plugin=args.plugin,
            systemd_dir=args.systemd_dir,
            python=args.python,
            profile=args.profile,
            core=args.core,
            one=args.one,
            extensions=args.extensions,
        )
        print(f"installed updater: {installed}")
        print(f"installed service: {service}")
        print(f"installed timer: {timer}")
        if args.enable:
            systemctl("daemon-reload")
            systemctl("enable", "--now", TIMER_NAME)
            print(f"enabled timer: {TIMER_NAME}")
        else:
            print(f"next: systemctl --user daemon-reload && systemctl --user enable --now {TIMER_NAME}")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
