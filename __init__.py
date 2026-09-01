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

Stall/orphan hardening
----------------------
A delegated ``hermes`` child spawns grandchildren of its own (MCP servers,
model-stream HTTP clients, LSP servers). A plain ``subprocess.run(timeout=)``
only SIGKILLs the direct child on timeout — the grandchildren reparent to init
and live on as orphans, holding sockets and burning API tokens (this is the
exact "stuck run" failure mode observed in the gateway). This plugin instead:

* spawns each child in its **own process group/session** (``start_new_session``
  = ``setsid`` on POSIX), so the whole tree shares one PGID;
* runs a **three-timer watchdog** — time-to-first-output (TTFB), inter-output
  idle, and an absolute hard ceiling — using a monotonic heartbeat updated by
  reader threads (the child streams stdout incrementally, so silence is a real
  liveness signal);
* on any timeout, **tree-kills** the whole group: ``killpg(SIGTERM)`` → grace →
  ``killpg(SIGKILL)`` — never orphaning grandchildren;
* bounds **concurrent** subprocesses and keeps a **live-child registry** so an
  interpreter exit / crash tree-kills every outstanding subagent (atexit);
* **classifies** the outcome (``failure_kind`` + ``retryable``) so an
  orchestrator can decide retry / fallback / give-up.

All thresholds are config-tunable in config.yaml (canonical Hermes plugin
location ``plugins.entries.hermes-smart-router.watchdog`` — see
:data:`_CONFIG_NAMESPACE`, which is where that string actually comes from),
falling back to env vars, then to tuned defaults:

  config.yaml (preferred — canonical plugin config):
    plugins:
      entries:
        hermes-smart-router:
          watchdog:
            hard_seconds: 600       # absolute ceiling
            ttfb_seconds: 120       # time-to-first-byte
            idle_seconds: 300       # inter-output silence
            kill_grace_seconds: 10  # SIGTERM -> SIGKILL grace
            max_concurrent: 4       # bounded concurrency
            queue_wait_seconds: 30  # slot wait (0 = up to hard ceiling)

  Env vars (legacy override when config.yaml key is absent):
  HERMES_DELEGATE_PROFILE_TIMEOUT        hard ceiling seconds (default 600; also the `timeout` arg)
  HERMES_DELEGATE_PROFILE_TTFB           no-first-output kill seconds (default 120)
  HERMES_DELEGATE_PROFILE_IDLE           inter-output idle kill seconds (default 300)
  HERMES_DELEGATE_PROFILE_KILL_GRACE     SIGTERM->SIGKILL grace seconds (default 10)
  HERMES_DELEGATE_PROFILE_MAX_CONCURRENT max concurrent subprocesses (default 4)
  HERMES_DELEGATE_PROFILE_QUEUE_WAIT     seconds to wait for a slot; 0 = up to the hard ceiling (default 30)

Installation:
    hermes plugins install rodrigogs/hermes-smart-router
    hermes plugins enable delegate-profile
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
# Hermes's plugin loader guarantees the ``hermes_plugins.<slug>`` namespace.
# Direct source-loading test harnesses use standalone module names and therefore
# need the top-level ``router`` package fallback instead.
_LOADED_AS_PACKAGE = __name__.startswith("hermes_plugins.")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HERMES_BIN = "hermes"

#: The per-plugin config namespace Hermes keys this plugin's settings under, i.e.
#: the ``<plugin_id>`` in ``plugins.entries.<plugin_id>``.
#:
#: ONE AUTHORITY, because a config key that appears in prose drifts from the code
#: that reads it. The 2026-08-27 rename moved this key from ``delegate-profile`` to
#: ``hermes-smart-router`` (along with the plugin directory and the state dir), and
#: the reader below was updated while FOUR other places were not: this module's
#: docstring, ``_watchdog_cfg``'s, ``_cfg_value``'s, and — the one that actually
#: cost something — the operator-facing ``at_capacity`` error, which told whoever
#: hit the concurrency cap to raise a key nothing reads. Every one of those now
#: interpolates from here or names this constant, and
#: ``test_the_capacity_error_names_the_key_the_reader_reads`` holds the message and
#: the reader to the same string.
#:
#: NOT the same thing as ``plugin.yaml``'s ``name:`` (still ``delegate-profile``)
#: or the tool name (deliberately still ``delegate_profile`` — renaming it breaks
#: every toolset allowlist). This is only the config namespace, and it is the
#: spelling verified live on the box.
_CONFIG_NAMESPACE = "hermes-smart-router"

#: Dotted path to the watchdog block, for docstrings and operator-facing messages.
_WATCHDOG_CFG_PATH = f"plugins.entries.{_CONFIG_NAMESPACE}.watchdog"

# Result/stderr truncation limits — keep subprocess output from blowing up the
# parent's context window.
_MAX_RESULT_CHARS = 8000
_MAX_STDERR_CHARS = 2000
# Per-stream in-memory cap while a child runs, so a chatty/runaway child can't
# grow the buffer without bound before the hard ceiling reaps it. We keep the
# TAIL (that's what the result/stderr fields report anyway).
_OUTPUT_BUFFER_CAP = 1_000_000

# Timeout ladder defaults (seconds).
# Tuned for reasoning-capable primaries (deepseek-v4-flash, glm-4.5-flash) at
# reasoning_effort=high: such models routinely spend 30-90 s per turn before
# streaming, and deep code reviews / research fan-outs run 5-10 min while making
# steady progress. The Hermes core delegation layer removed its blanket cap for
# the same reason — see commit history in delegate_tool.py and issue #14726.
# Invariant enforced at resolve time: ttfb < idle <= hard.
_DEFAULT_TIMEOUT_S = 600      # absolute hard ceiling (also the `timeout` arg)
_DEFAULT_TTFB_S = 120         # no first byte of output => startup wedged
_DEFAULT_IDLE_S = 300         # no NEW output for this long => mid-run stall
_DEFAULT_KILL_GRACE_S = 10    # SIGTERM -> grace -> SIGKILL (supervisord default)
_DEFAULT_MAX_CONCURRENT = 4   # bounded concurrency (rate-limit friendly)
_DEFAULT_QUEUE_WAIT_S = 30.0  # seconds to wait for a concurrency slot (0 = unbounded)
_SIGKILL_NUM = 9              # numeric to stay importable on Windows


# ---------------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        logger.warning("delegate_profile: invalid %s=%r, using %s", name, raw, default)
        return default
    return val if val > 0 else default


def _watchdog_cfg() -> Dict[str, Any]:
    """Return the plugin's ``watchdog:`` config from config.yaml.

    Canonical Hermes location: :data:`_WATCHDOG_CFG_PATH` (every plugin reads its
    per-plugin config from ``plugins.entries.<plugin_id>`` — see
    hermes_cli/plugins.py). Uses the same loader pattern as the holographic memory
    plugin: ``load_config_readonly`` + ``cfg_get``. Falls back to ``{}`` when the
    key is absent or config cannot be loaded — the resolvers then fall through to
    env vars, then module defaults.
    """
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        all_config = load_config_readonly()
        return cfg_get(
            all_config, "plugins", "entries", _CONFIG_NAMESPACE, "watchdog",
            default={},
        ) or {}
    except Exception:
        return {}


def _cfg_value(key: str, env_name: str, default: float) -> float:
    """Resolve one numeric watchdog param: config.yaml > env var > default.

    ``key`` is the config.yaml key under :data:`_WATCHDOG_CFG_PATH`; ``env_name``
    is the legacy env var kept for backward compatibility. Invalid (non-numeric,
    <= 0) config values fall through to the env/default rung.
    """
    val = _watchdog_cfg().get(key)
    if isinstance(val, (int, float)) and val > 0:
        return float(val)
    return _env_float(env_name, default)


def _resolve_timeout(explicit: Any) -> int:
    """Resolve the hard-ceiling timeout: explicit arg > config.yaml > env > default.

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
    return int(_cfg_value("hard_seconds", "HERMES_DELEGATE_PROFILE_TIMEOUT", _DEFAULT_TIMEOUT_S))


def _resolve_ladder(hard: int) -> Tuple[float, float, float, float]:
    """Return (ttfb, idle, hard, grace), coerced to a sane, ordered ladder.

    The child cannot legitimately be silent longer than the hard ceiling, and
    TTFB is meaningless once it exceeds idle, so clamp both under the ceiling
    and keep ttfb <= idle. This makes the three watchdogs strictly nested.

    Each value resolves as: config.yaml watchdog.<key> > env var > default.
    """
    ttfb = _cfg_value("ttfb_seconds", "HERMES_DELEGATE_PROFILE_TTFB", _DEFAULT_TTFB_S)
    idle = _cfg_value("idle_seconds", "HERMES_DELEGATE_PROFILE_IDLE", _DEFAULT_IDLE_S)
    grace = _cfg_value("kill_grace_seconds", "HERMES_DELEGATE_PROFILE_KILL_GRACE", _DEFAULT_KILL_GRACE_S)
    hard_f = float(hard)
    idle = min(float(idle), hard_f)
    ttfb = min(float(ttfb), idle)
    return ttfb, idle, hard_f, float(grace)


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

    ``default`` is Hermes's implicit profile, not a physical directory. Treat
    it as valid before consulting the runtime resolver, which may report False
    when a profile-scoped HERMES_HOME is active.
    """
    if profile == "default":
        return True

    try:
        from hermes_cli.profiles import profile_exists

        return bool(profile_exists(profile))
    except Exception:
        try:
            from hermes_constants import get_hermes_home

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


def _profile_schema_enum() -> List[str]:
    """Enum of accepted ``profile`` values for the tool schema.

    Known on-disk profiles plus the two always-valid special values:
    ``default`` (Hermes's implicit profile, never a physical directory) and
    ``auto`` (the documented router sentinel). Computed at register() time, so
    a profile created later simply isn't in the schema enum — the handler still
    validates every call against the live profile set via ``_profile_exists``.
    Never empty: the special values guarantee a usable JSON Schema enum.
    """
    return sorted(set(_list_known_profiles()) | {"default", "auto"})


# ---------------------------------------------------------------------------
# Process-tree lifecycle: spawn in own group, tree-kill on stall
# ---------------------------------------------------------------------------
def _spawn(cmd: List[str], env: dict) -> subprocess.Popen:
    """Spawn ``cmd`` in its OWN process group/session.

    POSIX: ``start_new_session=True`` -> the child calls ``setsid()`` before
    exec, becoming session+group leader (PGID == pid). Every grandchild it
    spawns inherits that PGID, so a single ``killpg`` reaps the whole tree.
    (``preexec_fn=os.setsid`` is deliberately NOT used — the stdlib warns it is
    unsafe with threads, and this handler runs inside a threaded agent.)

    Windows: ``CREATE_NEW_PROCESS_GROUP`` so the tree can be signalled/killed
    as a unit via taskkill (best-effort; the real deployment is POSIX/WSL).
    """
    kwargs: Dict[str, Any] = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered so the heartbeat updates per line
        env=env,
    )
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close captured stdout/stderr pipes after the child has been reaped.

    ``Popen.wait()`` reaps the process but deliberately leaves the parent-side
    file descriptors open. This leaks descriptors in long-lived agents and
    becomes a ResourceWarning failure under strict pytest settings.
    """
    for pipe in (proc.stdout, proc.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


def _kill_tree(proc: subprocess.Popen, pgid: Optional[int], grace: float) -> None:
    """Terminate the child AND its grandchildren, escalating TERM -> KILL.

    ``pgid`` must be captured at spawn time (``os.getpgid(proc.pid)``) because
    once the leader is reaped its pgid is no longer resolvable. Every step
    tolerates a race where the tree already exited.
    """
    if proc.poll() is not None:
        _close_pipes(proc)
        return  # already gone

    if IS_WINDOWS:
        # No process groups the POSIX way; taskkill /T walks the child tree.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=max(grace, 5.0),
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=max(grace, 5.0))
        except Exception:
            pass
        _close_pipes(proc)
        return

    # POSIX: signal the whole group.
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            _close_pipes(proc)
            return

    def _signal_group(sig: int) -> bool:
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return False
        except OSError as exc:
            logger.debug("delegate_profile: killpg(%s, %s) failed: %s", pgid, sig, exc)
            return False

    # Ask nicely, let the tree run its cleanup.
    _signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        _close_pipes(proc)
        return  # exited within grace
    except subprocess.TimeoutExpired:
        pass
    # Force-kill the whole group, then reap the leader (avoids a zombie).
    _signal_group(signal.SIGKILL)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        logger.warning("delegate_profile: pgid %s survived SIGKILL wait", pgid)
    finally:
        _close_pipes(proc)


class _Tail:
    """Thread-safe bounded buffer that keeps only the last N chars."""

    def __init__(self, cap: int = _OUTPUT_BUFFER_CAP) -> None:
        self._cap = cap
        self._parts: List[str] = []
        self._size = 0

    def append(self, chunk: str) -> None:
        self._parts.append(chunk)
        self._size += len(chunk)
        if self._size > self._cap * 2:
            # Collapse to the tail so memory stays bounded for chatty children.
            joined = "".join(self._parts)[-self._cap:]
            self._parts = [joined]
            self._size = len(joined)

    def text(self) -> str:
        return "".join(self._parts)[-self._cap:]


def _run_watched(
    proc: subprocess.Popen,
    pgid: Optional[int],
    ttfb: float,
    idle: float,
    hard: float,
    grace: float,
) -> Tuple[str, Optional[int], str, str]:
    """Drive ``proc`` under the three-timer watchdog.

    Returns ``(reason, returncode, stdout_tail, stderr_tail)`` where reason is
    one of ``exited`` | ``ttfb_timeout`` | ``idle_timeout`` | ``hard_timeout``.
    Reader threads stamp a monotonic heartbeat so the idle timer measures real
    output silence, not wall-clock. On any non-``exited`` reason the whole
    process tree is killed before returning.
    """
    out_buf, err_buf = _Tail(), _Tail()
    state_lock = threading.Lock()
    last_activity = time.monotonic()
    got_output = False

    def _reader(pipe, buf: _Tail) -> None:
        nonlocal last_activity, got_output
        try:
            while True:
                line = pipe.readline()
                if not line:
                    break
                buf.append(line)
                with state_lock:
                    last_activity = time.monotonic()
                    got_output = True
        except (ValueError, OSError):
            pass  # pipe closed under us (tree killed)

    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, out_buf), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, err_buf), daemon=True),
    ]
    for t in threads:
        t.start()

    start = time.monotonic()
    reason = "exited"
    while proc.poll() is None:
        now = time.monotonic()
        with state_lock:
            idle_for = now - last_activity
            first = got_output
        if now - start > hard:
            reason = "hard_timeout"
            break
        if not first and now - start > ttfb:
            reason = "ttfb_timeout"
            break
        if first and idle_for > idle:
            reason = "idle_timeout"
            break
        time.sleep(0.5)

    if reason != "exited":
        logger.warning(
            "delegate_profile: killing subprocess tree (pgid=%s) reason=%s",
            pgid, reason,
        )
        _kill_tree(proc, pgid, grace)
    else:
        # Ensure the leader is reaped and readers can flush remaining output.
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, pgid, grace)

    for t in threads:
        t.join(timeout=grace + 2)
    _close_pipes(proc)

    return reason, proc.returncode, out_buf.text().strip(), err_buf.text().strip()


def _classify(reason: str, returncode: Optional[int]) -> Tuple[Optional[str], bool]:
    """Map (watchdog reason, exit code) -> (failure_kind, retryable).

    Lets an orchestrator decide retry vs. fallback vs. give-up. ``None`` kind
    means success. POSIX reports signal death as a NEGATIVE return code.
    """
    if reason == "hard_timeout":
        return "hard_timeout", True          # maybe retry with a longer ceiling
    if reason == "ttfb_timeout":
        return "ttfb_stall", True            # startup wedged — usually transient
    if reason == "idle_timeout":
        return "idle_stall", True            # dead stream / hung tool — transient
    # reason == "exited"
    if returncode == 0:
        return None, False                   # success
    if returncode is not None and returncode < 0:
        sig = -returncode
        if sig == _SIGKILL_NUM:
            return "crash_or_oom", True      # OOM-killed (or external kill)
        return "crash", True                 # SIGSEGV/SIGABRT/... retry once
    return "nonzero_exit", False             # app-level error — retry repeats it


def _reported_agent_failure(stdout: str, stderr: str) -> bool:
    """Detect Hermes CLI failures that currently exit with status zero.

    ``hermes chat -q`` renders a stable terminal error after exhausting every
    provider, but its process status remains zero. Treating that transcript as
    a successful delegation silently returns an error banner as the agent's
    answer and prevents the router's cross-rail fallback from running.

    The retry count is not fixed: the same banner renders "after 1 retry",
    "after 3 retries", "after 5 retries" depending on the provider's own retry
    budget, and pinning the literal "after 3 retries:" meant every other count
    read as success. Measured against the real function: "after 5 retries" and
    "after 1 retry" were both missed, as was a bare provider banner carrying only
    the status code. The pattern now tolerates any count and singular/plural, and
    a terminal-error line that names no count at all still counts as a failure
    when it carries an exhaustion signal - reusing _EXHAUSTION_PATTERNS rather
    than inventing a second, divergent list.
    """
    blob = f"{stdout}\n{stderr}"
    if re.search(r"API call failed after \d+ retr(?:y|ies)\s*:", blob, re.IGNORECASE):
        return True
    # A provider can also fail terminally with no retry preamble at all, and not
    # every terminal failure is an exhaustion: an expired key (401), a revoked
    # entitlement (403), a TLS failure or a refused connection all abort without
    # ever retrying. Measured before this branch existed, every one of those read
    # as SUCCESS and the error transcript was returned as the agent's answer.
    if re.search(_TERMINAL_FAILURE_RE, blob, re.IGNORECASE):
        return True
    # A generic "API failed" line counts only when it names an exhaustion cause,
    # so an answer that merely discusses a 429 is not mistaken for one.
    if re.search(r"\b(?:API|provider|upstream)\s+(?:call\s+)?(?:failed|error)\b", blob, re.IGNORECASE):
        return _is_exhaustion(blob)
    return False


# Terminal provider failures that abort WITHOUT retrying, so they never carry the
# "after N retries" preamble. Each needs an abort/non-retryable marker or an
# unambiguous transport error - a bare "401" in prose must not trip this.
_TERMINAL_FAILURE_RE = "|".join((
    r"non-?retryable[^.\n]{0,40}\b(?:4\d\d|5\d\d)\b",
    r"\b(?:4\d\d|5\d\d)\b[^.\n]{0,40}\bnon-?retryable",
    r"\b(?:401|403)\b[^.\n]{0,60}\b(?:abort(?:ing|ed)?|giving up|unauthori[sz]ed|forbidden)\b",
    r"\b(?:unauthori[sz]ed|forbidden)\b[^.\n]{0,40}\babort(?:ing|ed)?\b",
    r"\bTLS\b[^.\n]{0,40}\b(?:verification|handshake)\s+failed\b",
    r"\b(?:certificate\s+verify|certificate\s+verification)\s+failed\b",
    r"\bconnection\s+(?:refused|reset\s+by\s+peer)\b",
    r"\b(?:name\s+or\s+service\s+not\s+known|temporary\s+failure\s+in\s+name\s+resolution)\b",
))


_EXHAUSTION_PATTERNS = (
    r"\b(?:429|402)\b",
    r"\busage_limit(?:_reached)?\b",
    r"\binsufficient\s+(?:credits|balance|account\s+balance)\b",
    r"\bweekly\s*/\s*monthly\s+limit\s+exhausted\b",
    r"\bcode\s*['\"]?\s*:\s*['\"]?1113\b",
)


def _is_exhaustion(text: str) -> bool:
    """Return whether provider output reports quota, credit, or rate exhaustion."""
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _EXHAUSTION_PATTERNS)


# ---------------------------------------------------------------------------
# Smart Router integration
# ---------------------------------------------------------------------------

# Thread-local recursion guard for the capability router. The classifier LLM
# dispatch (``ctx.llm.complete``) is synchronous and can re-enter the router on
# the SAME thread, so a re-entrant ``_route_task`` must bail to stop the
# classifier's own dispatch from looping. It must NOT be process-global:
# ``delegate_profile`` runs concurrently across threads (``_Pool`` /
# ``max_concurrent``), and a process-global flag made one in-flight classifier
# suppress every concurrent route — a second delegation read a sentinel the
# first one had set and silently skipped routing (surfacing as "profile is
# required" when no profile was named). A per-thread flag still catches the
# same-thread recursion while leaving every other thread's routing untouched.
_router_guard = threading.local()


def _classifier_defaults() -> Dict[str, Any]:
    """``router.classify.CLASSIFIER_DEFAULTS``, the one place they are written.

    Imported here rather than restated, so the pair this file DISPATCHES on cannot
    drift from the pair the classifier module documents — they did, and the losing
    copy was this one.

    Resolved at call time and both import shapes tried, exactly like every other
    sibling import in this file: the plugin is deployed by copy and must stay
    importable outside a live Hermes process, where it loads as a bare module
    rather than as ``hermes_plugins.<slug>``.
    """
    if _LOADED_AS_PACKAGE:
        from .router.classify import classifier_defaults
    else:  # direct source loading used by the development test harness
        from router.classify import classifier_defaults
    return classifier_defaults()


def _load_router_config() -> Dict[str, Any]:
    """Load router.yaml from the plugin directory. Returns {} on failure.

    router.yaml is the *live* policy and is deliberately not tracked by git, so
    that local tuning does not leave the checkout permanently dirty (and does
    not conflict on every pull). On first run it is seeded from the tracked
    router.example.yaml; after that it belongs to the operator.
    """
    try:
        import yaml
        plugin_dir = Path(__file__).resolve().parent
        config_path = plugin_dir / "router.yaml"
        if not config_path.exists():
            example = plugin_dir / "router.example.yaml"
            if not example.exists():
                return {}
            try:
                config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info(
                    "delegate_profile: seeded %s from router.example.yaml; edit it to tune routing",
                    config_path,
                )
            except OSError:
                # Read-only install: fall back to reading the example directly.
                return yaml.safe_load(example.read_text(encoding="utf-8")) or {}
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _make_classify_fn(ctx: Any) -> Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]]:
    """Build a classify_fn that uses the host's LLM for difficulty classification.

    Returns None if the router is disabled or ctx lacks llm. The classifier runs
    on the pair ``router.classify.CLASSIFIER_DEFAULTS`` names (trusted-streaming,
    temp=0, token-capped) unless router.yaml overrides it.
    Requires allow_provider_override + allow_model_override in plugin config.

    The defaults are IMPORTED, never restated: this function used to carry its own
    copy that defaulted to ``glm-5.2`` while ``classify.py`` said
    ``glm-5.3-flash``, and since this is the copy that actually dispatches, an
    absent ``classifier.model`` ran on an id the z.ai plan silently serves with a
    different model — succeeding, billing the substitute, and naming the wrong id
    in every trace.
    """
    config = _load_router_config()
    if not config.get("enabled", False):
        return None
    if ctx is None or not hasattr(ctx, "llm"):
        return None

    cls_conf = config.get("classifier", {})
    defaults = _classifier_defaults()
    provider = cls_conf.get("provider", defaults["provider"])
    model = cls_conf.get("model", defaults["model"])
    temperature = float(cls_conf.get("temperature", defaults["temperature"]))
    max_tokens = int(cls_conf.get("max_tokens", defaults["max_tokens"]))
    timeout = int(cls_conf.get("timeout_seconds", defaults["timeout_seconds"]))

    def classify_fn(task: str, features: Dict[str, Any]) -> Dict[str, Any]:
        """One-shot LLM difficulty classification. Returns {tier, confidence, ...}."""
        if _LOADED_AS_PACKAGE:
            from .router.classify import build_prompt_from_config
        else:  # direct source loading used by the development test harness
            from router.classify import build_prompt_from_config
        prompt = build_prompt_from_config(config, task, features)
        result = ctx.llm.complete(
            messages=[{"role": "user", "content": prompt}],
            provider=provider,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            purpose="hermes-smart-router.classify",
        )
        # Parse JSON response — model may wrap in markdown fences
        text = result.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("classifier did not return a JSON object")
        return parsed

    return classify_fn


def _compose_prompt(goal: str, context: str) -> str:
    """The exact text the delegated child model receives.

    One definition on purpose: the router sizes the turn from this string, so a
    second, drifting copy of the composition is what made the context signal
    blind in the first place — est_input_tokens measured the goal line while the
    child was sent goal PLUS context, and a 33k-token prompt could be routed as
    if it were six tokens.
    """
    return f"Context: {context}\n\nTask: {goal}" if context else goal


def _target_pairs(hops: Any) -> List[Tuple[str, str]]:
    """(model, provider) pairs from a chain/fallback list; malformed hops skipped."""
    if not isinstance(hops, list):
        return []
    pairs: List[Tuple[str, str]] = []
    for hop in hops:
        if isinstance(hop, dict) and hop.get("model"):
            pairs.append((str(hop["model"]), str(hop.get("provider") or "")))
    return pairs


def _routed_targets(routed: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Ordered (model, provider) attempts from one router decision.

    ``chain`` is the router's PLANNED order: the elos that can actually serve
    this turn, in the order the tier's fallback strategy chose. It is
    authoritative when present — rebuilding [primary] + declared fallbacks here
    is precisely what kept the capability filter and the fallback strategy inert
    on live traffic while the console displayed a filtered chain.

    A decision with no usable chain (a router that predates the plan, a test
    fake, a profile-only answer) degrades to the declared primary + fallback
    order, which is the historical behaviour.
    """
    if not isinstance(routed, dict):
        return []
    planned = _target_pairs(routed.get("chain"))
    if planned:
        return planned
    declared: List[Tuple[str, str]] = []
    if routed.get("model"):
        declared.append((str(routed["model"]), str(routed.get("provider") or "")))
    return declared + _target_pairs(routed.get("fallback"))


def _dedupe_targets(pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Drop repeat targets, first occurrence wins.

    A retry of the target that just failed is a wasted subprocess and a second
    breaker strike against the same rail.
    """
    seen: set = set()
    ordered: List[Tuple[str, str]] = []
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        ordered.append(pair)
    return ordered


def _route_task(
    goal: str,
    requested_model: str,
    classify_fn: Optional[Callable],
    prompt_text: str = "",
) -> Optional[Dict[str, Any]]:
    """Run the capability router on a goal string.

    Returns {profile, model?, provider?, chain?} or None if routing failed /
    router unavailable / blocklist veto / recursion guard active. ``chain`` is
    the planned attempt order (see router.adapter.route) and is what the
    executor iterates.

    ``prompt_text`` is the full text the child will receive (context + goal);
    the router reads its signals from that and classifies on ``goal`` alone.
    Empty means "same as goal", which is the shape a caller with no context has.

    Best-effort: routing failure → caller falls through to normal delegation.
    Never blocks — all errors are caught.
    """
    # Recursion guard: don't re-enter the router during a classifier dispatch.
    if getattr(_router_guard, "active", False):
        return None

    _router_guard.active = True
    try:
        if _LOADED_AS_PACKAGE:
            from .router.adapter import route
            from .router.blocklist import Blocklist
            from .router.durable_decision_log import DurableDecisionLog
        else:  # direct source loading used by the development test harness
            from router.adapter import route
            from router.blocklist import Blocklist
            from router.durable_decision_log import DurableDecisionLog

        config = _load_router_config()
        if not config.get("enabled", False):
            return None

        blocklist = Blocklist(config)
        # Persist a per-step trace for visual replay. This is the single writer;
        # the sidecar only reads routes.jsonl back. The durable log never raises
        # into routing (all IO is guarded), and the whole call already sits in
        # this best-effort try/except, so trace persistence can never break
        # routing. cache= and session_pin= stay per-call throwaway on the live
        # path (out of scope), so those pipeline nodes read as cold in replay.
        # rng= and now= are deliberately NOT passed: route() is the edge, so it
        # derives the per-turn seed from the task and reads the UTC clock itself,
        # and records both in the trace. Pinning them here would make every live
        # decision use one fixed order.
        result = route(
            task=goal,
            config=config,
            requested_model=requested_model,
            classify_fn=classify_fn,
            blocklist=blocklist,
            decision_log=DurableDecisionLog(),
            prompt_text=prompt_text,
        )

        # Blocklist veto or pending classify action → no concrete target
        if result.get("deny") or result.get("action") == "classify":
            return None

        # Must have a profile to be useful
        if not result.get("profile"):
            return None

        return result
    except Exception as exc:
        logger.debug("hermes-smart-router: _route_task failed: %s", exc)
        return None
    finally:
        _router_guard.active = False


# ---------------------------------------------------------------------------
# Kanban-dispatch routing (pre_kanban_dispatch hook) — shadow and live modes
# ---------------------------------------------------------------------------
#
# The capability router is a model-selection surface for kanban cards: a
# ``pre_kanban_dispatch`` hook, fired by the dispatcher AFTER a task is claimed
# and BEFORE the worker spawns (ready and review lanes). The ``shadow:``
# section of router.yaml picks the mode:
#
#   * shadow mode (default; ``shadow.enabled`` true or the section absent) —
#     the decision is recorded in the durable trace but the model/provider
#     field is NEVER returned, so dispatch behaves exactly as if no hook
#     subscribed. This is the measurement mode.
#   * live mode (``shadow.enabled: false``) — the SAME hook also returns
#     ``{"model", "provider"}`` for the decisions dispatch may apply (the head
#     of the planned chain, see _kanban_live_override); the dispatcher applies
#     it to that worker's spawn. Live entries are written with ``shadow: False``
#     and so never count in shadow_gate_rate.
#   * the router master ``enabled:`` gates BOTH modes — false means no routing
#     and no trace at all.
#
# Two properties of this path are deliberate:
#
#   * NO CLASSIFIER. Stage 1 is an LLM call and the dispatch path is per-card,
#     possibly many cards per tick — an LLM per card is exactly the per-turn
#     cost the design keeps out of the hot path. The shadow therefore measures
#     how well Stage 0 alone covers REAL cards; that measurement IS the gate
#     for leaving shadow mode (shadow_gate_rate).
#   * THE ROLE IS AN INPUT HERE, NOT AN OUTPUT. The worker is
#     ``hermes -p <assignee>`` and the dispatcher's hook applies only
#     ("model", "provider") — measured in hermes_cli/kanban_db.py, where
#     _PRE_DISPATCH_MUTABLE_FIELDS names exactly those two. So no decision on
#     this path can move a role, and a rule's ``then.profile`` is not a
#     destination here: it is an annotation about the role the policy had in mind.
#
#     Until 2026-08-26 a mismatch refused the WHOLE decision and dropped the
#     model half with it. Measured on 158 real cards: 135 (85%) died that way,
#     because every rule in the shipped policy names coder or reviewer while the
#     cards run as trama-engineer. The model half is the only thing this path can
#     contribute, so it now applies, and the trace records
#     cause=role_out_of_scope — the operator sees that an axis was left alone
#     instead of guessing that the router had nothing to say.
#
#     A rule that must NOT fire for a given role scopes itself on the input side:
#     ``when: {assignee: {eq: reviewer}}``. Same shape the clock uses, and it
#     keeps the old protection (a reviewer-tuned row buying the strongest tier for
#     every coder card) available as a policy statement instead of as a silent
#     veto over every decision.

# The agreed gate for leaving shadow mode: the fraction of real cards that fell
# through Stage 0 (no_classifier or fallthrough) must be at or below this.
# 0.20 = the shipped Table 1 covering 4 of 5 real card shapes; an operator who
# wants a stricter or looser bar changes this constant — the gate is advisory,
# it never blocks dispatch.
_SHADOW_MAX_FALLTHROUGH_RATE = 0.20

try:
    from .router.durable_decision_log import DurableDecisionLog, attempts_path, read_entries
    from .router.decision_log import DecisionLog, plan_head_of
except ImportError:  # pragma: no cover - flat layout used by the test harness
    from router.durable_decision_log import DurableDecisionLog, attempts_path, read_entries
    from router.decision_log import DecisionLog, plan_head_of


class _KanbanShadowLog(DurableDecisionLog):
    """Durable trace log for the kanban dispatch path — one entry per card.

    The ``shadow`` key on every persisted entry is the mode marker:

    * ``shadow: True`` — shadow mode (the default; ``shadow.enabled`` true or
      absent in router.yaml): the decision is recorded but NEVER returned to
      the dispatcher. The exit gate (:func:`shadow_gate_rate`) counts exactly
      these entries — the measurement of how well Stage 0 covers REAL cards.
    * ``shadow: False`` — live mode (``shadow.enabled: false``): the same
      trace entry, written by a hook that ALSO returns the decision. Live
      entries stay out of the gate measurement because the gate counts
      ``is True``; the gate function itself does not change.

    The role axis — this path cannot change the worker's role, so a decision
    whose ``profile`` differs from the card's assignee has its cause stamped
    ``role_out_of_scope`` and KEEPS the model half, which is the only half this
    path can apply. Nothing is dropped: the recorded output still carries the
    role the policy wanted, so the operator can tell "the router chose this
    model" from "the router also wanted another role, which this path never
    moves". The predicate is :func:`_kanban_role_out_of_scope`, the one authority
    shared with the live return path — two implementations of the same question
    is how the trace and dispatch come to disagree about the same card.
    """

    def __init__(
        self,
        allowed_profile: Optional[str] = None,
        *,
        live: bool = False,
        task_id: str = "",
        run_id: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._allowed_profile = allowed_profile
        self._live = live
        self._task_id = task_id
        self._run_id = run_id

    def record(
        self,
        cause: str,
        output: Dict[str, Any],
        matched_rule_id: Optional[str] = None,
        task_preview: str = "",
        *,
        steps: Optional[List[Dict[str, Any]]] = None,
        chain_plan: Optional[Dict[str, Any]] = None,
    ) -> None:
        if _kanban_role_out_of_scope(output, self._allowed_profile):
            cause = "role_out_of_scope"
        # In-memory append (NOT the parent's record, which would persist the
        # unstamped entry), then stamp, then persist once.
        DecisionLog.record(
            self, cause, output, matched_rule_id, task_preview,
            steps=steps, chain_plan=chain_plan,
        )
        entry = self._entries[-1]
        entry["shadow"] = not self._live
        if self._task_id:
            entry["task_id"] = self._task_id
        if self._run_id is not None:
            entry["run_id"] = self._run_id
        self._persist(entry)


def _kanban_role_out_of_scope(
    output: Dict[str, Any],
    allowed_profile: Optional[str],
) -> bool:
    """True when a decision names a ROLE other than the one the caller fixed.

    The ONE definition of the kanban role question, shared by the durable trace
    (``_KanbanShadowLog.record`` stamps ``role_out_of_scope``) and the live return
    path (:func:`_kanban_live_override`, which still hands over the model half).
    Two implementations of the same question is how the trace and the dispatcher
    come to disagree about the same card.

    Out of scope is not refusal: the role axis was never this path's to move, the
    dispatcher's hook applying only model and provider. A rule that should not
    fire for a role at all belongs on the input side, as
    ``when: {assignee: {eq: <role>}}``.
    """
    return bool(
        allowed_profile
        and output.get("profile")
        and output["profile"] != allowed_profile
    )


def _kanban_live_override(
    decision: Dict[str, Any],
    allowed_profile: Optional[str],
) -> Optional[Dict[str, str]]:
    """The ``{"model", "provider"}`` a LIVE hook may hand the dispatcher, or None.

    A decision drives dispatch when it carries a complete head. The role axis is
    NOT a condition: the caller already fixed the role and this path applies only
    model/provider, so a decision whose ``profile`` differs still hands over its
    model half (:func:`_kanban_role_out_of_scope` names that case for the trace).
    A rule that must not fire for a role scopes itself in ``when.assignee``.

    The one condition:

    * a complete head. The head is :func:`plan_head_of` — the first hop of the
      planned chain, the one definition of \"head\" in the repo. When the
      decision carries no ``chain``, the plan IS the declared order (the
      adapter's documented semantics, ``_with_chain``), so the head is the
      declared ``model``/``provider`` pair. Either way both fields must be
      non-empty: a model without a provider is the classic mis-set that strands
      a board (kanban_db._resolve_pre_dispatch_model_override), so it is
      refused with None.
    """
    head = plan_head_of(decision)
    if head is None:
        model = decision.get("model")
        provider = decision.get("provider")
        if not model or not provider:
            return None
        head = (str(model), str(provider))
    model, provider = head
    # ``model`` is guaranteed non-empty here (plan_head_of only returns hops
    # with one, the fallback above checked it); the provider is the field a
    # half-set decision actually misses.
    if not provider:
        return None
    return {"model": model, "provider": provider}


def _kanban_task_text(task: Any) -> str:
    """Title + body of a kanban card — the routing input.

    Title and body are joined because the card's routing signal may live in
    the body (a review card's body IS the PR description the worker will
    read). ``task`` is duck-typed (a kanban_db.Task or a test stand-in).
    """
    title = str(getattr(task, "title", "") or "")
    body = str(getattr(task, "body", "") or "")
    if body:
        return f"{title}\n\n{body}"
    return title


def _read_kanban_task(task_id: str, board: Optional[str]) -> Any:
    """Read a kanban card from the board DB, or None on any failure.

    The hook payload carries identifiers only — the title/body that routing
    needs must be read from the board. Guarded exactly like the plugin's other
    hermes_cli accesses: absent hermes_cli (CI) or a busy DB degrades to None
    and the hook does nothing, which is byte-identical to having no subscriber.
    """
    if not task_id:
        return None
    try:
        from hermes_cli import kanban_db as _kb
    except ImportError:  # pragma: no cover - CI has no hermes_cli
        return None
    try:
        conn = _kb.connect(board=board)
        try:
            return _kb.get_task(conn, task_id)
        finally:
            conn.close()
    except Exception:
        logger.debug("delegate-profile: could not read kanban task %s", task_id,
                     exc_info=True)
        return None


def _on_pre_kanban_dispatch(
    task_id: str = "",
    profile_name: str = "",
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    run_id: Optional[int] = None,
    **_kwargs: Any,
) -> Optional[Dict[str, str]]:
    """``pre_kanban_dispatch`` subscriber — shadow or LIVE, per ``shadow.enabled``.

    Routes the card through the capability router and records the decision in
    the durable trace. In shadow mode (default; ``shadow.enabled`` true or
    absent) the hook NEVER returns a model/provider dict — the dispatcher
    behaves exactly as if no hook subscribed. In live mode
    (``shadow.enabled: false``) it returns the head of the planned chain as
    ``{"model", "provider"}`` when the decision may drive dispatch (see
    :func:`_kanban_live_override`), and the dispatcher applies it to that
    worker's spawn. See the section comment for the two modes.

    Best-effort by contract — a broken callback can never corrupt dispatch, so
    every failure path returns None. The hook is consulted only while the
    card's ``model_override`` is NULL (hard precedence in the dispatcher), so
    ``requested_model`` is "" in practice; the value is still passed through
    so the call is the same shape a live consumer will use.
    """
    try:
        config = _load_router_config()
    except Exception:
        return None
    if not config.get("enabled", False):
        return None
    shadow = config.get("shadow") or {}
    live = shadow.get("enabled") is False

    task = _read_kanban_task(task_id, board)
    if task is None:
        return None
    goal = _kanban_task_text(task)

    # No classifier on this path — see the section comment. Everything below
    # stays inside the try: a broken route (or a malformed decision) degrades
    # to None, byte-identical to having no subscriber.
    try:
        if _LOADED_AS_PACKAGE:
            from .router.adapter import route
            from .router.blocklist import Blocklist
        else:  # direct source loading used by the development test harness
            from router.adapter import route
            from router.blocklist import Blocklist

        blocklist = Blocklist(config)
        # The worker has profile-scoped HERMES_HOME and does not load this
        # plugin. Publish our canonical file path through the dispatcher env
        # before it spawns, so core's executor journal and this durable reader
        # converge without core importing plugin code.
        os.environ["HERMES_ROUTE_ATTEMPTS_FILE"] = str(attempts_path())
        log = _KanbanShadowLog(
            allowed_profile=assignee, live=live, task_id=task_id, run_id=run_id,
        )
        decision = route(
            task=goal,
            config=config,
            requested_model=str(getattr(task, "model_override", None) or ""),
            requested_provider=str(getattr(task, "provider_override", None) or ""),
            classify_fn=None,
            blocklist=blocklist,
            decision_log=log,
            prompt_text=goal,
            assignee=str(assignee or ""),
        )
        if not live:
            return None
        return _kanban_live_override(decision, assignee)
    except Exception:
        logger.debug("delegate-profile: kanban dispatch failed for %s", task_id,
                     exc_info=True)
        return None


def shadow_gate_rate(limit: Optional[int] = None) -> Optional[float]:
    """Fraction of SHADOW card decisions that fell through Stage 0, or None.

    ``None`` means there are no shadow entries yet — nothing has been measured
    and the gate is NOT met (an empty sample proves nothing). Otherwise the
    fraction of ``shadow: True`` trace entries whose steps show a fail_safe
    stage with reason ``no_classifier`` or ``fallthrough`` — the two outcomes
    the shadow exists to measure. The cause field alone cannot be counted:
    ``_KanbanShadowLog`` may have rewritten it (``role_out_of_scope`` today,
    ``profile_ignored`` in traces written before 2026-08-26), but the steps keep
    the pipeline's own record.

    ``limit`` bounds the trace entries READ (shadow entries among the last N
    mixed delegate+shadow entries); None reads everything.
    """
    entries = read_entries(limit)
    shadow_entries = [e for e in entries if e.get("shadow") is True]
    if not shadow_entries:
        return None
    fell = 0
    for entry in shadow_entries:
        for step in entry.get("steps") or []:
            if (
                isinstance(step, dict)
                and step.get("stage") == "fail_safe"
                and (step.get("in") or {}).get("reason")
                in ("no_classifier", "fallthrough")
            ):
                fell += 1
                break
    return fell / len(shadow_entries)


def _shadow_gate_ok(rate: Optional[float] = None) -> bool:
    """True when the shadow measurement is at or below the agreed limit.

    An empty sample (rate is None) is NOT ok: the gate must not read as met
    before a single real card has been measured.
    """
    if rate is None:
        rate = shadow_gate_rate()
    return rate is not None and rate <= _SHADOW_MAX_FALLTHROUGH_RATE


def _provider_of_declared_model(model: str, config: Dict[str, Any]) -> str:
    """Best-effort provider for ``model`` from the policy, or "" if unknown.

    A LAST RESORT, used only when the caller could not name the provider it
    actually attempted (see :func:`_record_breaker_outcome`). Tier PRIMARIES are
    scanned first — they are the unambiguous declaration — and the per-tier
    ``fallback`` hops after them, because an elo that is only ever a fallback hop
    (``gpt-5.6-luna`` in the shipped tiers) is invisible to a primaries-only scan
    and would leave every outcome for it keyed without a provider.

    Defensive rather than raising: the config is HOT and may be half-edited, so a
    non-mapping tier, a missing ``model`` or a non-list ``fallback`` is skipped and
    the scan continues. Ambiguity resolves to the FIRST declaration, matching the
    historical behaviour; the derivation is a fallback, and the attempted provider
    is what should reach this function.
    """
    if not isinstance(config, dict):
        return ""
    tiers = config.get("tiers", {})
    if not isinstance(tiers, dict):
        return ""
    ordered = [tcfg for tcfg in tiers.values() if isinstance(tcfg, dict)]
    for tcfg in ordered:
        if tcfg.get("model") == model:
            return str(tcfg.get("provider") or "")
    for tcfg in ordered:
        hops = tcfg.get("fallback")
        if not isinstance(hops, list):
            continue
        for hop in hops:
            if isinstance(hop, dict) and hop.get("model") == model:
                return str(hop.get("provider") or "")
    return ""


def _record_breaker_outcome(
    profile: str,
    model: str,
    failure_kind: Optional[str],
    provider: str = "",
) -> None:
    """Record delegate_profile outcome in the capability router's auto-breaker.

    Fire-and-forget — errors are logged but never propagated. The breaker
    lives in router/blocklist.py and uses router.yaml config.

    ``provider`` IS THE ONE THAT WAS ATTEMPTED, and it is passed in rather than
    re-derived, because the breaker key and the key the router reads must be the
    same string. ``Blocklist`` keys breaker state as ``model@provider`` and the
    running path asks ``is_blocked(model, provider)``; an outcome recorded under a
    bare ``model`` therefore lands in a cell nothing on the routing path ever
    reads. The rail keeps being attempted after it has exhausted its quota while
    ``breaker_status()`` displays a tripped breaker — the running path and the
    reporting surface disagreeing about one decision. Every fallback hop hit that
    case, since the derivation below only ever saw tier primaries.

    It is the LAST parameter and it has a default, so the historical
    three-argument call shape keeps working; when it is absent the policy scan is
    the best guess available, and "" (breaker keyed by bare model) remains the
    last resort rather than a dropped outcome — a breaker that records nothing
    would be strictly worse than one keyed coarsely.
    """
    if not model:
        return
    try:
        if _LOADED_AS_PACKAGE:
            from .router.blocklist import Blocklist
        else:  # direct source loading used by the development test harness
            from router.blocklist import Blocklist

        config = _load_router_config()
        blocklist = Blocklist(config)

        # The attempted provider wins; the policy scan only fills a caller's gap.
        provider = str(provider or "") or _provider_of_declared_model(model, config)

        if failure_kind is not None:
            blocklist.record_failure(model, provider, failure_kind)
        else:
            blocklist.record_success(model, provider)
    except Exception:
        pass  # breaker is best-effort, never blocks the tool


# ---------------------------------------------------------------------------
# Bounded concurrency + live-child registry (structured-concurrency discipline)
# ---------------------------------------------------------------------------
class _Pool:
    """Caps concurrent subprocesses and tracks live children for cleanup.

    A slot must be acquired before spawning and is released on every exit path.
    The registry lets a parent interpreter exit (atexit) tree-kill every
    outstanding subagent so nothing outlives the process — the subprocess
    analog of a Trio nursery / asyncio TaskGroup.
    """

    def __init__(self, max_concurrent: int) -> None:
        self.capacity = max_concurrent
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._live: Dict[int, Tuple[subprocess.Popen, Optional[int], dict]] = {}
        self._lock = threading.Lock()

    def acquire(self, wait: float) -> bool:
        # timeout=None blocks forever; a positive value bounds the wait.
        if wait <= 0:
            return self._sem.acquire()
        return self._sem.acquire(timeout=wait)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass  # BoundedSemaphore guards against over-release

    def register(self, proc: subprocess.Popen, pgid: Optional[int], meta: dict) -> None:
        with self._lock:
            self._live[proc.pid] = (proc, pgid, meta)

    def unregister(self, pid: int) -> None:
        with self._lock:
            self._live.pop(pid, None)

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [dict(meta, pid=pid) for pid, (_, _, meta) in self._live.items()]

    def kill_all(self, grace: float = _DEFAULT_KILL_GRACE_S) -> None:
        with self._lock:
            items = list(self._live.items())
        for pid, (proc, pgid, _) in items:
            try:
                _kill_tree(proc, pgid, grace)
            except Exception:
                logger.debug("delegate_profile: kill_all failed for pid %s", pid)
            self.unregister(pid)


_POOL: Optional[_Pool] = None
_POOL_LOCK = threading.Lock()


def _get_pool() -> _Pool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            cap = int(_cfg_value(
                "max_concurrent", "HERMES_DELEGATE_PROFILE_MAX_CONCURRENT",
                float(_DEFAULT_MAX_CONCURRENT),
            ))
            _POOL = _Pool(cap)
            atexit.register(_POOL.kill_all)
    return _POOL


# ---------------------------------------------------------------------------
# Tool handler factory
# ---------------------------------------------------------------------------
def _make_handler(
    current_profile: str,
    dispatch_delegate: Callable,
    ctx: Any = None,
) -> Callable:
    """Build the delegate_profile tool handler.

    Captures the active profile (resolved once at register time) and a
    ``dispatch_delegate`` callable that routes same-profile calls through the
    plugin context's ``dispatch_tool`` — which wires ``parent_agent`` onto the
    call, something a direct ``delegate_task(...)`` import cannot do.

    If ``ctx`` is provided and the capability router is configured, tasks
    without an explicit profile are routed through the router (Stage 0 +
    optional Stage 1 classifier) to pick the best profile + model.
    """

    classify_fn = _make_classify_fn(ctx) if ctx is not None else None

    def delegate_profile(args: dict, **_kwargs) -> str:
        # `prompt` is the canonical field (delegate_profile contract); `goal`
        # is kept as a legacy alias so existing callers keep working.
        goal = (args.get("prompt") or args.get("goal") or "").strip()
        context = (args.get("context") or "").strip()
        profile = (args.get("profile") or "").strip()
        model = (args.get("model") or "").strip()
        hard_timeout = _resolve_timeout(args.get("timeout"))

        if not goal:
            return json.dumps({"error": "prompt is required", "failure_kind": "bad_args"})

        # The exact text the child will receive. Composed HERE, before routing,
        # because the router estimates context size from it.
        prompt = _compose_prompt(goal, context)

        # --- Capability router: pick profile+model when not explicitly given ---
        routed_provider = ""
        routed_targets: List[Tuple[str, str]] = []
        if not profile or profile == "auto":
            # "auto" is the sentinel asking the router to choose; it is never a
            # real profile name. Clear it before routing, so a router decline
            # falls through to the "profile is required" branch instead of
            # reaching _profile_exists("auto") and telling the operator to run
            # `hermes profile create auto` - advice that would create a profile
            # shadowing the sentinel.
            profile = ""
            # With no context the composed prompt IS the goal, so the fourth
            # argument is skipped there: identical routing either way, and the
            # historical 3-argument shape stays what a host-patched seam sees.
            routed = (
                _route_task(goal, model, classify_fn, prompt) if context
                else _route_task(goal, model, classify_fn)
            )
            if routed is not None:
                profile = routed.get("profile", "") or profile
                routed_provider = routed.get("provider", "") or ""
                routed_targets = _routed_targets(routed)
                if not model and routed_targets:
                    # The PLANNED head, not the declared tier primary: a vision
                    # turn whose primary cannot see images must not be attempted
                    # first, nor handed to the inline delegate_task path.
                    model, routed_provider = routed_targets[0]

        if not profile:
            return json.dumps({"error": "profile is required", "failure_kind": "bad_args"})

        # Validate the target profile BEFORE the same-profile shortcut, so a
        # typo produces an instant clear error even when it happens to differ
        # from the active profile.
        if not _profile_exists(profile):
            known = _list_known_profiles()
            return json.dumps(
                {
                    "success": False,
                    "failure_kind": "unknown_profile",
                    "retryable": False,
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
                    {"error": f"Inline delegation failed: {exc}",
                     "failure_kind": "inline_error", "retryable": True}
                )

# Cross-profile: spawn a fully independent hermes process tree.
        hermes_bin = _resolve_hermes_bin()

        env = os.environ.copy()
        # Resolve HERMES_HOME like we do so the child finds the real ~/.hermes
        # (silences the wrong-profile warning, issue #18594).
        if "HERMES_HOME" not in env:
            try:
                from hermes_constants import get_hermes_home
                env["HERMES_HOME"] = str(get_hermes_home())
            except Exception:
                pass
        env["HERMES_PROFILE"] = profile          # keep child env consistent with -p
        env["HERMES_DELEGATE_PROFILE_DISABLE"] = "1"   # anti-recursion

        ttfb, idle, hard, grace = _resolve_ladder(hard_timeout)
        pool = _get_pool()
        # queue_wait accepts 0 (0 = wait up to the hard ceiling), so it needs
        # a dedicated resolution that allows zero rather than falling through.
        qw_cfg = _watchdog_cfg().get("queue_wait_seconds")
        if isinstance(qw_cfg, (int, float)) and qw_cfg >= 0:
            queue_wait = float(qw_cfg)
        else:
            queue_wait = _env_float("HERMES_DELEGATE_PROFILE_QUEUE_WAIT", _DEFAULT_QUEUE_WAIT_S)
        if not pool.acquire(queue_wait if queue_wait > 0 else hard):
            return json.dumps({
                "success": False, "failure_kind": "at_capacity", "retryable": True,
                "error": (
                    "Too many concurrent delegate_profile subprocesses "
                    f"(cap={pool.capacity}). "
                    f"Retry shortly or raise {_WATCHDOG_CFG_PATH}.max_concurrent."
                ),
            })

        def _attempt(attempt_model: str, attempt_provider: str) -> dict:
            """Run one spawn+watchdog attempt for a (model, provider) target.

            Returns a result dict (never raises). ``--provider`` is passed to the
            child when set so the router's provider axis actually reaches the
            subprocess (previously dropped). The whole tree is watchdog-guarded
            and tree-killed exactly as before.
            """
            cmd = [hermes_bin, "-p", profile, "chat", "-q", prompt]
            if attempt_model:
                cmd.extend(["-m", attempt_model])
            if attempt_provider:
                cmd.extend(["--provider", attempt_provider])
            subagent_id = f"dp_{uuid.uuid4().hex[:12]}"
            started_at = time.time()
            logger.info(
                "delegate_profile: spawning %s (profile=%s model=%s provider=%s "
                "ttfb=%.0fs idle=%.0fs hard=%.0fs)",
                subagent_id, profile, attempt_model or "-", attempt_provider or "-",
                ttfb, idle, hard,
            )
            proc = None
            pgid = None
            try:
                try:
                    proc = _spawn(cmd, env)
                except FileNotFoundError:
                    return {"success": False, "failure_kind": "binary_not_found",
                            "retryable": False,
                            "error": f"Hermes binary not found: {hermes_bin}. Ensure hermes is on PATH."}
                except Exception as exc:
                    logger.exception("delegate_profile: spawn failed")
                    return {"success": False, "failure_kind": "spawn_error", "retryable": True,
                            "error": f"Subprocess spawn error: {exc}"}
                if not IS_WINDOWS:
                    try:
                        pgid = os.getpgid(proc.pid)
                    except (ProcessLookupError, OSError):
                        pgid = proc.pid
                pool.register(proc, pgid, {"subagent_id": subagent_id, "profile": profile,
                                           "started_at": started_at})
                reason, returncode, stdout, stderr = _run_watched(proc, pgid, ttfb, idle, hard, grace)
            finally:
                if proc is None:
                    # Spawn did not produce a child; there is no process tree
                    # or pool entry to clean up. Keep this explicit because a
                    # FileNotFound/spawn error returns through this finally.
                    logger.debug("delegate_profile: no child process to clean up")
                else:
                    _kill_tree(proc, pgid, grace)
                    pool.unregister(proc.pid)
            elapsed = round(time.time() - started_at, 1)
            failure_kind, retryable = _classify(reason, returncode)
            if _is_exhaustion(f"{stdout}\n{stderr}"):
                failure_kind, retryable = "quota_exhausted", True
            elif failure_kind is None and _reported_agent_failure(stdout, stderr):
                failure_kind, retryable = "agent_error", True
            # The provider is passed POSITIONALLY and is the one this attempt
            # actually used: the breaker key has to be the ``model@provider`` the
            # blocklist reads back, or a failing fallback hop's breaker never
            # binds. Positional keeps the seam a host (or a test) may patch with
            # a ``*args`` stand-in working.
            _record_breaker_outcome(profile, attempt_model, failure_kind,
                                    attempt_provider)
            base = {"subagent_id": subagent_id, "profile": profile,
                    "model": attempt_model, "provider": attempt_provider, "elapsed_s": elapsed}
            if failure_kind == "hard_timeout":
                return {**base, "success": False, "failure_kind": failure_kind, "retryable": retryable,
                        "error": f"Hard timeout after {int(hard)}s.",
                        "stderr": stderr[-_MAX_STDERR_CHARS:] if stderr else ""}
            if failure_kind in ("ttfb_stall", "idle_stall"):
                detail = (f"produced no output within {int(ttfb)}s" if failure_kind == "ttfb_stall"
                          else f"went silent for more than {int(idle)}s")
                return {**base, "success": False, "failure_kind": failure_kind, "retryable": retryable,
                        "error": f"Subagent stalled ({detail}) and was terminated.",
                        "stderr": stderr[-_MAX_STDERR_CHARS:] if stderr else "",
                        "partial_output": stdout[-_MAX_RESULT_CHARS:] if stdout else ""}
            if failure_kind == "quota_exhausted":
                return {**base, "success": False, "failure_kind": failure_kind,
                        "retryable": retryable,
                        "error": "Provider quota exhausted; trying the next fallback target.",
                        "stderr": stderr[-_MAX_STDERR_CHARS:] if stderr else "",
                        "partial_output": stdout[-_MAX_RESULT_CHARS:] if stdout else ""}
            if failure_kind == "agent_error":
                return {**base, "success": False, "failure_kind": failure_kind,
                        "retryable": retryable,
                        "error": "Hermes child reported a failure despite exiting with code 0.",
                        "stderr": stderr[-_MAX_STDERR_CHARS:] if stderr else "",
                        "partial_output": stdout[-_MAX_RESULT_CHARS:] if stdout else ""}
            if failure_kind is not None:
                return {**base, "success": False, "failure_kind": failure_kind, "retryable": retryable,
                        "error": f"Subprocess exited abnormally (code {returncode})",
                        "stderr": stderr[-_MAX_STDERR_CHARS:] if stderr else ""}
            return {**base, "success": True,
                    "result": stdout[-_MAX_RESULT_CHARS:] if stdout else "(no output)"}

        try:
            # Target chain: the router's PLANNED order — capability-filtered and
            # ordered by the tier's fallback strategy — behind the primary. When
            # the caller named a model explicitly that model stays first (it
            # overrides the routing decision) and the plan supplies the tail;
            # otherwise the primary IS the plan's head, so the dedupe leaves the
            # planned order exactly as planned. Retry the NEXT target only on a
            # retryable failure — so a Mac-only primary (Claude Code)
            # transparently fails over to a non-Mac rail, honoring 'Claude Code
            # is never the sole option' at EXECUTION time.
            targets = _dedupe_targets([(model, routed_provider)] + routed_targets)
            attempts_meta = []
            last = None
            for idx, (tm, tp) in enumerate(targets):
                last = _attempt(tm, tp)
                attempts_meta.append({"model": tm, "provider": tp,
                                      "ok": bool(last.get("success")),
                                      "failure_kind": last.get("failure_kind")})
                if last.get("success"):
                    break
                if not last.get("retryable"):
                    break   # bad_args/unknown/binary_not_found — fallback won't help
                if idx + 1 < len(targets):
                    logger.warning("delegate_profile: target %s/%s failed (%s); trying fallback %s/%s",
                                   tp or "-", tm or "-", last.get("failure_kind"),
                                   targets[idx+1][1] or "-", targets[idx+1][0] or "-")
            if len(attempts_meta) > 1:
                last["attempts"] = attempts_meta
            return json.dumps(last, ensure_ascii=False)
        finally:
            pool.release()

    return delegate_profile


# ---------------------------------------------------------------------------
# Post-tool-call hook
# ---------------------------------------------------------------------------
def _on_post_tool_call(
    tool_name: str = "",
    args: dict | None = None,
    result: str = "",
    *,
    params: dict | None = None,
    **_kwargs: Any,
) -> None:
    """Record delegate_profile invocations; warn on delegate_task misuse.

    Post-tool-call audit for this plugin's own tool: every delegate_profile
    call is logged (profile + goal/prompt) so operators can trace subprocess
    delegation from the parent session's log.

    Advisory only — never blocks. The built-in delegate_task *does* accept
    ``profile=`` for in-process delegation; the nudge is for callers who
    actually want subprocess isolation (this plugin's purpose).
    """
    # The host (model_tools._emit_post_tool_call_hook) passes ``args=``, as
    # documented in hermes_cli/hooks.py.  ``params`` is accepted as a legacy
    # alias so older callers and tests keep working.
    payload = args if args is not None else (params or {})
    if tool_name == "delegate_profile":
        if isinstance(payload, dict):
            logger.info(
                "delegate_profile invoked: profile=%r goal=%r",
                payload.get("profile", "auto"),
                payload.get("goal") or payload.get("prompt"),
            )
        return
    if tool_name != "delegate_task":
        return
    if payload and isinstance(payload, dict) and "profile" in payload:
        logger.warning(
            "delegate_profile: delegate_task called with 'profile' param "
            "(in-process delegation). If you want subprocess isolation under "
            "that profile, use delegate_profile instead."
        )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------
# Contexts register() has already serviced, keyed by id(ctx). The host calls
# register() once per plugin load, but the plugin contract (and the tester
# card) requires repeated calls with the SAME ctx to be a no-op: the host's
# register_hook appends callbacks without dedup, so a second pass would
# register post_tool_call twice. Different ctx objects (a fresh plugin host, a
# test's fake) register fresh.
#
# The value holds a STRONG reference to ctx: without it, a short-lived fake
# ctx would be garbage-collected and CPython could hand its id to a later
# ctx, wrongly skipping that registration.
_REGISTERED_CTX: Dict[int, Any] = {}


def register(ctx):
    """Register the delegate_profile tool and post_tool_call hook.

    Idempotent per context: calling register() again with the same ctx object
    is a no-op, so a plugin reload / double-invocation never duplicates the
    hook callbacks or shadows the tool registration.
    """

    ctx_id = id(ctx)
    if ctx_id in _REGISTERED_CTX:
        logger.debug("delegate-profile: register() already ran for ctx %s; skipping", ctx_id)
        return

    current_profile = _get_active_profile_name()

    def _dispatch_delegate(dt_args: dict) -> str:
        return ctx.dispatch_tool("delegate_task", dt_args)

    handler = _make_handler(current_profile, _dispatch_delegate, ctx=ctx)

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
            "is faster. Same-profile calls fall back to delegate_task. The "
            "subprocess is watchdog-guarded (time-to-first-output, idle, and "
            "hard-ceiling timeouts) and tree-killed on stall so it can never "
            "hang or orphan child processes; on failure the result carries a "
            "`failure_kind` and `retryable` flag."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "What the subagent should accomplish. Be specific and "
                        "self-contained — the subagent knows nothing about "
                        "your conversation history."
                    ),
                },
                "goal": {
                    "type": "string",
                    "description": (
                        "Legacy alias for `prompt`, kept for backward "
                        "compatibility with earlier callers. Prefer `prompt`."
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
                    "enum": _profile_schema_enum(),
                    "description": (
                        "Hermes profile name to run the subagent under "
                        "(e.g., 'coder', 'reviewer', 'researcher-a'). The profile "
                        "must exist (validated before spawn). Omit or use 'auto' "
                        "to let the capability router pick the best profile + model "
                        "based on task difficulty (Stage 0 rules + Stage 1 classifier)."
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
                        # Interpolated, not written: this string is read by the
                        # MODEL, which budgets its own work against it. It said
                        # "Default: 300 (5 min)" while the default has been 600 —
                        # a caller sizing a task to fit "5 minutes" was working
                        # from half the real ceiling, and no test could catch a
                        # number that only ever appeared in prose.
                        "Absolute hard-ceiling seconds for the subprocess. "
                        f"Default: {_DEFAULT_TIMEOUT_S} "
                        f"({_DEFAULT_TIMEOUT_S // 60} min). Independent tighter "
                        "watchdogs also apply: no-first-output (TTFB) and "
                        "inter-output idle. Override the ceiling globally with "
                        "HERMES_DELEGATE_PROFILE_TIMEOUT; TTFB/idle via "
                        "HERMES_DELEGATE_PROFILE_TTFB / _IDLE."
                    ),
                },
            },
            "required": ["prompt"],
        },
    }

    ctx.register_tool(
        name="delegate_profile",
        toolset="delegation",
        schema=DELEGATE_PROFILE_SCHEMA,
        handler=handler,
        description=(
            "Spawn a subagent under a specific Hermes profile via "
            "hermes -p <profile> chat -q (watchdog-guarded subprocess isolation)"
        ),
    )

    ctx.register_hook("post_tool_call", _on_post_tool_call)

    # Kanban dispatch routing: shadow mode by default — records what the
    # capability router WOULD choose for each dispatched card, without writing
    # the model/provider field. `shadow: {enabled: false}` in router.yaml
    # switches the SAME hook to live mode, where it also RETURNS the model
    # decision for the dispatcher to apply to that worker's spawn.
    ctx.register_hook("pre_kanban_dispatch", _on_pre_kanban_dispatch)

    # Registration completed — mark this ctx as serviced. Added last so a
    # mid-registration failure leaves the ctx unmarked and a retry re-runs.
    _REGISTERED_CTX[ctx_id] = ctx

    logger.info(
        "delegate-profile plugin registered (profile=%s)", current_profile,
    )
