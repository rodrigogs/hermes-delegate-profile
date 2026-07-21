"""
Hermes Delegate Profile Plugin

Spawn subagents under a **different** Hermes profile via a fully isolated
subprocess (``hermes -p <profile> chat -q "<goal>"``).

Why this exists alongside the built-in ``delegate_task(profile=...)``:

  The built-in ``delegate_task`` ALREADY supports ``profile=`` for *in-process*
  cross-profile delegation — it swaps the child's config, secret scope, SOUL,
  and toolsets. That path is fast and shares the parent process.

  This plugin is for the *subprocess-isolation* case, where you want a hard
  process boundary around the child: the child runs as its own OS process,
  cannot crash the parent, can run a different Hermes version, and gets the
  target profile's full configured toolset (the in-process path can only
  *narrow* the parent's capabilities, never widen them).

  Rule of thumb: use ``delegate_task(profile=...)`` for speed when you'd be
  happy running the subagent in the current process; use ``delegate_profile``
  when the subprocess boundary itself is the point.

Same-profile calls (profile omitted or matching the active profile) fall back
to the built-in ``delegate_task`` via ``ctx.dispatch_tool`` so the parent
agent context is wired up correctly.

Installation:
    hermes plugins install rodrigogs/hermes-delegate-profile
    hermes plugins enable delegate-profile
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HERMES_BIN = "hermes"

# Result/stderr truncation limits — keep subprocess output from blowing up the
# parent's context window.
_MAX_RESULT_CHARS = 8000
_MAX_STDERR_CHARS = 2000

# Timeout ladder rungs (see _resolve_timeout).
_DEFAULT_TIMEOUT_S = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_timeout(explicit: Any) -> int:
    """Resolve the effective timeout: explicit arg > env > default.

    Invalid values (non-int, <= 0) fall through to the next rung rather than
    raising — the handler must always return a usable int.
    """
    if explicit is not None and explicit != "":
        try:
            val = int(explicit)
            if val > 0:
                return val
        except (TypeError, ValueError):
            logger.warning("delegate_profile: invalid timeout %r, ignoring", explicit)
    env_val = os.environ.get("HERMES_DELEGATE_PROFILE_TIMEOUT")
    if env_val:
        try:
            val = int(env_val)
            if val > 0:
                return val
        except (TypeError, ValueError):
            logger.warning(
                "delegate_profile: invalid HERMES_DELEGATE_PROFILE_TIMEOUT=%r",
                env_val,
            )
    return _DEFAULT_TIMEOUT_S


def _resolve_hermes_bin() -> str:
    """Find the hermes binary, preferring the one next to our own interpreter."""
    venv_bin = Path(sys.executable).parent / HERMES_BIN
    if venv_bin.exists():
        return str(venv_bin)
    return HERMES_BIN  # fall back to PATH lookup


def _get_active_profile_name() -> str:
    """Return the active profile name via Hermes's own resolver.

    Falls back to the ``HERMES_PROFILE`` env var and finally ``"default"``
    when the import fails (e.g. plugin loaded outside a running Hermes
    process — tests, lint).
    """
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return os.environ.get("HERMES_PROFILE", "default")


def _profile_exists(profile: str) -> bool:
    """Return True if the named profile directory exists.

    Uses the canonical resolver (honors HERMES_HOME, the ``default`` special
    case) with a defensive fallback so the plugin still loads on older or
    minimal runtimes.
    """
    try:
        from hermes_cli.profiles import profile_exists

        return bool(profile_exists(profile))
    except Exception:
        try:
            from hermes_constants import get_hermes_home

            if profile == "default":
                return True
            return (get_hermes_home() / "profiles" / profile).is_dir()
        except Exception:
            return False  # safer to refuse to spawn than to guess


def _list_known_profiles() -> list:
    """Best-effort list of existing profile names, for error messages."""
    try:
        from hermes_cli import profiles as _prof

        return [p.name for p in _prof.list_profiles()] or []
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home

        pdir = get_hermes_home() / "profiles"
        if pdir.is_dir():
            return sorted(p.name for p in pdir.iterdir() if p.is_dir())
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Tool handler factory
# ---------------------------------------------------------------------------
def _make_handler(current_profile: str, dispatch_delegate: Callable) -> Callable:
    """Build the delegate_profile tool handler.

    Captures the active profile (resolved once at register time) and a
    ``dispatch_delegate`` callable that routes same-profile calls through the
    plugin context's ``dispatch_tool`` — which wires ``parent_agent`` onto the
    call, something a direct ``delegate_task(...)`` import cannot do.
    """

    def delegate_profile(args: dict, **_kwargs) -> str:
        goal = (args.get("goal") or "").strip()
        context = (args.get("context") or "").strip()
        profile = (args.get("profile") or "").strip()
        model = (args.get("model") or "").strip()
        timeout = _resolve_timeout(args.get("timeout"))

        if not goal:
            return json.dumps({"error": "goal is required"})
        if not profile:
            return json.dumps({"error": "profile is required"})

        # Validate the target profile BEFORE the same-profile shortcut, so a
        # typo produces an instant clear error even when it happens to differ
        # from the active profile.
        if not _profile_exists(profile):
            known = _list_known_profiles()
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Profile {profile!r} does not exist. "
                        f"Create it with: hermes profile create {profile}"
                    ),
                    "profile": profile,
                    "available_profiles": known,
                    "hint": (
                        f"Available profiles: {', '.join(known)}" if known
                        else "Run `hermes profile list` to see profiles."
                    ),
                },
                ensure_ascii=False,
            )

        # Same-profile shortcut: stay in-process for efficiency. Route through
        # dispatch_tool so parent_agent is wired up for delegate_task.
        if profile == current_profile:
            logger.info(
                "delegate_profile: profile %s matches current, routing inline "
                "to delegate_task",
                profile,
            )
            dt_args: Dict[str, Any] = {"goal": goal}
            if context:
                dt_args["context"] = context
            if model:
                dt_args["model"] = model
            try:
                return dispatch_delegate(dt_args)
            except Exception as exc:
                logger.exception("delegate_profile: inline dispatch failed")
                return json.dumps(
                    {"error": f"Inline delegation failed: {exc}"}
                )

        # Cross-profile: spawn a fully independent hermes process.
        hermes_bin = _resolve_hermes_bin()
        prompt = f"Context: {context}\n\nTask: {goal}" if context else goal

        # `hermes -p <profile> chat -q "<prompt>"` — -p is a global flag and
        # MUST come before the subcommand.
        cmd = [hermes_bin, "-p", profile, "chat", "-q", prompt]
        if model:
            cmd.extend(["-m", model])

        env = os.environ.copy()
        # Make the child resolve HERMES_HOME the same way we do, so it finds
        # the real ~/.hermes rather than inheriting any per-session override
        # that points at a scratch workspace.
        if "HERMES_HOME" not in env:
            try:
                from hermes_constants import get_hermes_home

                env["HERMES_HOME"] = str(get_hermes_home())
            except Exception:
                pass
        # Prevent infinite recursion if the child also loads this plugin.
        env["HERMES_DELEGATE_PROFILE_DISABLE"] = "1"

        subagent_id = f"dp_{uuid.uuid4().hex[:12]}"
        started_at = time.time()
        logger.info(
            "delegate_profile: spawning %s (profile=%s, timeout=%ds)",
            subagent_id, profile, timeout,
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.time() - started_at
            return json.dumps({
                "success": False,
                "subagent_id": subagent_id,
                "profile": profile,
                "error": (
                    f"Timeout after {timeout}s. Raise the `timeout` arg or set "
                    f"HERMES_DELEGATE_PROFILE_TIMEOUT."
                ),
                "elapsed_s": round(elapsed, 1),
            })
        except FileNotFoundError:
            return json.dumps({
                "success": False,
                "error": (
                    f"Hermes binary not found: {hermes_bin}. "
                    f"Ensure hermes is on PATH."
                ),
            })
        except Exception as exc:
            logger.exception("delegate_profile: subprocess failed")
            return json.dumps({
                "success": False,
                "error": f"Subprocess error: {exc}",
            })

        elapsed = time.time() - started_at
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            return json.dumps({
                "success": False,
                "subagent_id": subagent_id,
                "profile": profile,
                "error": f"Subprocess exited with code {result.returncode}",
                "stderr": stderr[-_MAX_STDERR_CHARS:] if stderr else "",
                "elapsed_s": round(elapsed, 1),
            })

        return json.dumps({
            "success": True,
            "subagent_id": subagent_id,
            "profile": profile,
            "result": stdout[-_MAX_RESULT_CHARS:] if stdout else "(no output)",
            "elapsed_s": round(elapsed, 1),
        })

    return delegate_profile


# ---------------------------------------------------------------------------
# Post-tool-call hook
# ---------------------------------------------------------------------------
def _on_post_tool_call(tool_name: str, params: dict, result: str) -> None:
    """Warn when delegate_task is called with a `profile` param.

    Advisory only — never blocks. The built-in delegate_task *does* accept
    ``profile=`` for in-process delegation; the nudge is for callers who
    actually want subprocess isolation (this plugin's purpose).
    """
    if tool_name != "delegate_task":
        return
    if params and isinstance(params, dict) and "profile" in params:
        logger.warning(
            "delegate_profile: delegate_task called with 'profile' param "
            "(in-process delegation). If you want subprocess isolation under "
            "that profile, use delegate_profile instead."
        )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------
def register(ctx):
    """Register the delegate_profile tool and post_tool_call hook."""

    current_profile = _get_active_profile_name()

    def _dispatch_delegate(dt_args: dict) -> str:
        return ctx.dispatch_tool("delegate_task", dt_args)

    handler = _make_handler(current_profile, _dispatch_delegate)

    DELEGATE_PROFILE_SCHEMA = {
        "name": "delegate_profile",
        "description": (
            "Spawn a subagent under a SPECIFIC Hermes profile as a fully "
            "isolated subprocess (`hermes -p <profile> chat -q`). The child "
            "runs as its own OS process with the target profile's config, "
            "skills, memories, model, and toolset — a hard process boundary "
            "the built-in delegate_task(profile=...) does not provide. Use "
            "this when you need process-level isolation (crash safety, "
            "different Hermes version, the target profile's FULL toolset). "
            "For in-process cross-profile delegation, delegate_task(profile=...) "
            "is faster. Same-profile calls fall back to delegate_task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "What the subagent should accomplish. Be specific and "
                        "self-contained — the subagent knows nothing about "
                        "your conversation history."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Background information the subagent needs: file paths, "
                        "error messages, project structure, constraints."
                    ),
                },
                "profile": {
                    "type": "string",
                    "description": (
                        "REQUIRED. Hermes profile name to run the subagent "
                        "under (e.g., 'coder', 'reviewer', 'researcher-a'). "
                        "The profile must exist (validated before spawn). Use "
                        "'hermes profile list' to see available profiles."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional model override for the subagent, passed as "
                        "-m. If omitted, uses the target profile's default."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Max seconds to wait for the subprocess. Default: 300 "
                        "(5 minutes). Override globally with the "
                        "HERMES_DELEGATE_PROFILE_TIMEOUT env var."
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
        handler=handler,
        description=(
            "Spawn a subagent under a specific Hermes profile via "
            "hermes -p <profile> chat -q (subprocess isolation)"
        ),
    )

    ctx.register_hook("post_tool_call", _on_post_tool_call)

    logger.info(
        "delegate-profile plugin registered (profile=%s)", current_profile,
    )
