"""
Hermes Delegate Profile Plugin

Extends delegate_task with profile selection — spawn subagents under
different Hermes profiles via `hermes -p <profile>` subprocess.

Architecture:
- Registers a new tool `delegate_profile` that mirrors `delegate_task`
  but adds a `profile` parameter.
- When `profile` is set (not the current profile), spawns a one-shot
  `hermes -p <profile> chat -q "<goal>"` subprocess instead of using
  the internal ThreadPoolExecutor.
- When `profile` is omitted or matches the current profile, delegates
  to the normal `delegate_task` for efficiency.
- Also registers a `post_tool_call` hook that can intercept
  `delegate_task` calls and warn if the user tried to pass a profile
  to the standard tool.

Installation:
    cp -r hermes-delegate-profile ~/.hermes/plugins/
    hermes plugins enable delegate-profile
"""

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HERMES_BIN = "hermes"
CURRENT_PROFILE = os.environ.get("HERMES_PROFILE", "default")


def _resolve_hermes_bin() -> str:
    """Find the hermes binary, preferring the one matching our own runtime."""
    # Check if we're running inside the hermes venv
    venv_bin = Path(sys.executable).parent / HERMES_BIN
    if venv_bin.exists():
        return str(venv_bin)
    # Fall back to PATH
    return HERMES_BIN


# ---------------------------------------------------------------------------
# Tool handler: delegate_profile
# ---------------------------------------------------------------------------
def delegate_profile(args: dict, **kwargs) -> str:
    """Spawn a subagent under a specific Hermes profile.

    Accepts the same parameters as delegate_task plus `profile` (required).
    When profile differs from the current profile, spawns a one-shot
    `hermes -p <profile> chat -q "<goal>"` subprocess.

    Args:
        goal: What the subagent should accomplish
        context: Background info for the subagent
        profile: Hermes profile to use (required)
        model: Optional model override
        timeout: Max seconds (default 300)

    Returns:
        JSON string with {success, result, profile, ...}
    """
    goal = args.get("goal", "")
    context = args.get("context", "")
    profile = args.get("profile", "")
    model = args.get("model", "")
    timeout = int(args.get("timeout", 300))

    if not goal:
        return json.dumps({"error": "goal is required"})

    if not profile:
        return json.dumps({"error": "profile is required"})

    # If profile matches current, delegate to normal delegate_task
    if profile == CURRENT_PROFILE:
        logger.info(
            "delegate_profile: profile %s matches current, using inline delegation",
            profile,
        )
        return _delegate_inline(goal, context, kwargs)

    # Build the one-shot command
    hermes_bin = _resolve_hermes_bin()
    prompt = goal
    if context:
        prompt = f"Context: {context}\n\nTask: {goal}"

    cmd = [hermes_bin, "-p", profile, "chat", "-q", prompt]
    if model:
        cmd.extend(["-m", model])

    # Inject HERMES_HOME so profiles resolve correctly
    env = os.environ.copy()
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    env["HERMES_HOME"] = hermes_home

    # Avoid nested delegate_profile in child
    env["HERMES_DELEGATE_PROFILE_DISABLE"] = "1"

    subagent_id = f"dp_{uuid.uuid4().hex[:12]}"
    started_at = time.time()

    logger.info(
        "delegate_profile: spawning %s via %s (timeout=%ds)",
        subagent_id, " ".join(cmd), timeout,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=kwargs.get("_workdir") or os.getcwd(),
        )
        elapsed = time.time() - started_at
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            return json.dumps({
                "success": False,
                "subagent_id": subagent_id,
                "profile": profile,
                "error": f"Subprocess exited with code {result.returncode}",
                "stderr": stderr[-2000:] if stderr else "",
                "elapsed_s": round(elapsed, 1),
            })

        return json.dumps({
            "success": True,
            "subagent_id": subagent_id,
            "profile": profile,
            "result": stdout[-8000:] if stdout else "(no output)",
            "elapsed_s": round(elapsed, 1),
        })

    except subprocess.TimeoutExpired:
        elapsed = time.time() - started_at
        return json.dumps({
            "success": False,
            "subagent_id": subagent_id,
            "profile": profile,
            "error": f"Timeout after {timeout}s",
            "elapsed_s": round(elapsed, 1),
        })
    except FileNotFoundError:
        return json.dumps({
            "success": False,
            "error": f"Hermes binary not found: {hermes_bin}",
        })
    except Exception as exc:
        logger.exception("delegate_profile: unexpected error")
        return json.dumps({
            "success": False,
            "error": str(exc),
        })


def _delegate_inline(goal: str, context: str, kwargs: dict) -> str:
    """Fall back to normal delegate_task when profile matches current."""
    try:
        from tools.delegate_tool import delegate_task

        result = delegate_task(
            goal=goal,
            context=context,
            **(kwargs or {}),
        )
        return result if isinstance(result, str) else json.dumps(result)
    except ImportError:
        return json.dumps({
            "error": "Cannot import delegate_task for inline delegation",
        })
    except Exception as exc:
        return json.dumps({
            "error": f"Inline delegation failed: {exc}",
        })


# ---------------------------------------------------------------------------
# Post-tool-call hook
# ---------------------------------------------------------------------------
def _on_post_tool_call(tool_name: str, params: dict, result: str) -> None:
    """Warn when delegate_task is called with what looks like a profile param."""
    if tool_name != "delegate_task":
        return
    if params and isinstance(params, dict) and "profile" in params:
        logger.warning(
            "delegate_profile: delegate_task called with 'profile' param. "
            "Use delegate_profile tool instead for cross-profile delegation."
        )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------
def register(ctx):
    """Register the delegate_profile tool and post_tool_call hook."""

    DELEGATE_PROFILE_SCHEMA = {
        "name": "delegate_profile",
        "description": (
            "Spawn a subagent under a SPECIFIC Hermes profile. "
            "Unlike delegate_task (which always uses the current profile), "
            "this tool runs the subagent via `hermes -p <profile>` so it "
            "inherits that profile's config, skills, memories, and model. "
            "Use this when you need a subagent with a different personality "
            "or capability set than the current session. "
            "For same-profile delegation, use delegate_task instead (faster, "
            "in-process)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "What the subagent should accomplish. Be specific "
                        "and self-contained — the subagent knows nothing "
                        "about your conversation history."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Background information the subagent needs: file "
                        "paths, error messages, project structure, constraints."
                    ),
                },
                "profile": {
                    "type": "string",
                    "description": (
                        "REQUIRED. Hermes profile name to run the subagent "
                        "under (e.g., 'coder', 'reviewer', 'researcher-a'). "
                        "The profile must exist. Use 'hermes profile list' "
                        "to see available profiles."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model override for the subagent. "
                        "If omitted, uses the target profile's default model."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Max seconds to wait for the subagent. "
                        "Default: 300 (5 minutes)."
                    ),
                },
            },
            "required": ["goal", "profile"],
        },
    }

    ctx.register_tool(
        name="delegate_profile",
        toolset="delegation",
        schema=DELEGATE_PROFILE_SCHEMA,
        handler=delegate_profile,
        description=(
            "Spawn a subagent under a specific Hermes profile via "
            "hermes -p <profile> chat -q"
        ),
    )

    ctx.register_hook("post_tool_call", _on_post_tool_call)

    logger.info(
        "delegate-profile plugin registered (profile=%s)", CURRENT_PROFILE,
    )
