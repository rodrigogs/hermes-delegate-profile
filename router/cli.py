"""CLI governance — router explain, lint, blocklist, log.

v1: file-config + CLI governance. No webui panel.
"""

from __future__ import annotations

import argparse
import json
import sys
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

from .signals import extract
from .rules import explain as rules_explain, lint as rules_lint
from .blocklist import Blocklist
from .decision_log import DecisionLog
from .cache import Cache, SessionPin


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str = "router.yaml") -> Dict[str, Any]:
    """Load router.yaml."""
    p = Path(path)
    if not p.exists():
        print(f"router: config not found at {p.resolve()}", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_explain(args: argparse.Namespace) -> None:
    """Run explain() on a task and print the decision trace."""
    task = args.task
    config = load_config(args.config)

    features = extract(task)
    blocklist = Blocklist(config)

    # Check if a model override is in the task
    requested_model = args.model or ""
    blocked = blocklist.is_blocked(requested_model, "")
    if blocked:
        print(json.dumps({
            "cause": "blocklist_veto",
            "output": {"deny": True},
            "fallback": blocklist.fallback_for(requested_model),
        }, indent=2))
        return

    result = rules_explain(
        task, features, blocked,
        config.get("rules", []),
        config.get("default", {}),
        config.get("tiers", {}),
    )

    print(json.dumps(result, indent=2, default=str))


def cmd_lint(args: argparse.Namespace) -> None:
    """Validate router.yaml, fail-closed."""
    config = load_config(args.config)
    errors = rules_lint(config)
    if errors:
        print(f"router: {len(errors)} config error(s):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    print("router: config valid")


def cmd_blocklist(args: argparse.Namespace) -> None:
    """Show banned models, breaker state, and fallback chain."""
    config = load_config(args.config)
    bl = Blocklist(config)

    print("Manual bans:")
    for ban in bl.manual_bans():
        print(f"  - model={ban['model']} provider={ban.get('provider', '*')} "
              f"reason={ban.get('reason', 'none')}")

    # Show breaker cooldowns if enabled
    if bl.breaker_enabled():
        status = bl.breaker_status()
        if status:
            print(f"\nAuto-breaker cooldowns:")
            for s in status:
                remaining = s.get("cooldown_remaining_s", 0)
                if remaining > 120:
                    rem_str = f"{remaining/60:.0f}m remaining"
                elif remaining > 1:
                    rem_str = f"{remaining:.0f}s remaining"
                else:
                    rem_str = "expiring now"
                print(f"  - model={s['model_key']} state={s['state']} "
                      f"cooldown={rem_str} backoff={s['backoff_seconds']:.0f}s "
                      f"last_failure={s.get('last_failure_kind', '-')}")
        else:
            print(f"\nAuto-breaker: enabled, no active cooldowns")

    print(f"\nFallback chain: {' → '.join(bl.fallback_chain())}")


def cmd_log(args: argparse.Namespace) -> None:
    """Tail the decision log."""
    n = args.tail or 20
    # In production, this reads from the session decision log.
    # For v1 CLI, we read from a file if --file is given.
    if args.file:
        try:
            with open(args.file) as f:
                lines = f.readlines()
            for line in lines[-n:]:
                print(line.rstrip())
        except FileNotFoundError:
            print(f"router: log file not found: {args.file}", file=sys.stderr)
            sys.exit(1)
    else:
        print("router: no log file specified (use --file)")

    if args.follow:
        print("router: --follow not yet implemented (v2)")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="router",
        description="Capability Router governance CLI",
    )
    parser.add_argument("--config", default="router.yaml",
                       help="Path to router.yaml (default: router.yaml)")

    sub = parser.add_subparsers(dest="command", required=True)

    # explain
    p_explain = sub.add_parser("explain", help="Trace routing decision for a task")
    p_explain.add_argument("task", help="Task description to classify")
    p_explain.add_argument("--model", default="", help="Requested model (for blocklist check)")
    p_explain.set_defaults(func=cmd_explain)

    # lint
    p_lint = sub.add_parser("lint", help="Validate router.yaml")
    p_lint.set_defaults(func=cmd_lint)

    # blocklist
    p_bl = sub.add_parser("blocklist", help="Show blocked models")
    p_bl.set_defaults(func=cmd_blocklist)

    # log
    p_log = sub.add_parser("log", help="Tail decision log")
    p_log.add_argument("--tail", type=int, default=20, help="Number of lines (default: 20)")
    p_log.add_argument("--file", help="Log file path")
    p_log.add_argument("--follow", "-f", action="store_true",
                       help="Follow (v2)")
    p_log.set_defaults(func=cmd_log)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
