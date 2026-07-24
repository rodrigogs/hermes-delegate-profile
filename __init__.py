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

All thresholds are env-tunable:

  HERMES_DELEGATE_PROFILE_TIMEOUT        hard ceiling seconds (default 300; also the `timeout` arg)
  HERMES_DELEGATE_PROFILE_TTFB           no-first-output kill seconds (default 60)
  HERMES_DELEGATE_PROFILE_IDLE           inter-output idle kill seconds (default 180)
  HERMES_DELEGATE_PROFILE_KILL_GRACE     SIGTERM->SIGKILL grace seconds (default 10)
  HERMES_DELEGATE_PROFILE_MAX_CONCURRENT max concurrent subprocesses (default 4)
  HERMES_DELEGATE_PROFILE_QUEUE_WAIT     seconds to wait for a slot; 0 = up to the hard ceiling

Installation:
    hermes plugins install rodrigogs/hermes-delegate-profile
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

# Result/stderr truncation limits — keep subprocess output from blowing up the
# parent's context window.
_MAX_RESULT_CHARS = 8000
_MAX_STDERR_CHARS = 2000
# Per-stream in-memory cap while a child runs, so a chatty/runaway child can't
# grow the buffer without bound before the hard ceiling reaps it. We keep the
# TAIL (that's what the result/stderr fields report anyway).
_OUTPUT_BUFFER_CAP = 1_000_000

# Timeout ladder defaults (seconds). See module docstring for env overrides.
# Invariant enforced at resolve time: ttfb < idle <= hard.
_DEFAULT_TIMEOUT_S = 300      # absolute hard ceiling (also the `timeout` arg)
_DEFAULT_TTFB_S = 60          # no first byte of output => startup wedged
_DEFAULT_IDLE_S = 180         # no NEW output for this long => mid-run stall
_DEFAULT_KILL_GRACE_S = 10    # SIGTERM -> grace -> SIGKILL (supervisord default)
_DEFAULT_MAX_CONCURRENT = 4   # bounded concurrency (rate-limit friendly)
_SIGKILL_NUM = 9              # numeric to stay importable on Windows


# ---------------------------------------------------------------------------
# Env helpers
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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning("delegate_profile: invalid %s=%r, using %s", name, raw, default)
        return default
    return val if val > 0 else default


def _resolve_timeout(explicit: Any) -> int:
    """Resolve the hard-ceiling timeout: explicit arg > env > default.

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
    return _env_int("HERMES_DELEGATE_PROFILE_TIMEOUT", _DEFAULT_TIMEOUT_S)


def _resolve_ladder(hard: int) -> Tuple[float, float, float, float]:
    """Return (ttfb, idle, hard, grace), coerced to a sane, ordered ladder.

    The child cannot legitimately be silent longer than the hard ceiling, and
    TTFB is meaningless once it exceeds idle, so clamp both under the ceiling
    and keep ttfb <= idle. This makes the three watchdogs strictly nested.
    """
    ttfb = _env_float("HERMES_DELEGATE_PROFILE_TTFB", _DEFAULT_TTFB_S)
    idle = _env_float("HERMES_DELEGATE_PROFILE_IDLE", _DEFAULT_IDLE_S)
    grace = _env_float("HERMES_DELEGATE_PROFILE_KILL_GRACE", _DEFAULT_KILL_GRACE_S)
    hard_f = float(hard)
    idle = min(idle, hard_f)
    ttfb = min(ttfb, idle)
    return ttfb, idle, hard_f, grace


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
    """
    return "API call failed after 3 retries:" in f"{stdout}\n{stderr}"


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
# Capability Router integration
# ---------------------------------------------------------------------------

_ROUTER_SENTINEL = "HERMES_ROUTER_CLASSIFYING"


def _load_router_config() -> Dict[str, Any]:
    """Load router.yaml from the plugin directory. Returns {} on failure."""
    try:
        import yaml
        plugin_dir = Path(__file__).resolve().parent
        config_path = plugin_dir / "router.yaml"
        if not config_path.exists():
            return {}
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _make_classify_fn(ctx: Any) -> Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]]:
    """Build a classify_fn that uses the host's LLM for difficulty classification.

    Returns None if the router is disabled or ctx lacks llm. The classifier
    pins provider=zai + model=glm-5.2 (trusted-streaming, temp=0, token-capped).
    Requires allow_provider_override + allow_model_override in plugin config.
    """
    config = _load_router_config()
    if not config.get("enabled", False):
        return None
    if ctx is None or not hasattr(ctx, "llm"):
        return None

    cls_conf = config.get("classifier", {})
    provider = cls_conf.get("provider", "zai")
    model = cls_conf.get("model", "glm-5.2")
    temperature = float(cls_conf.get("temperature", 0))
    max_tokens = int(cls_conf.get("max_tokens", 128))
    timeout = int(cls_conf.get("timeout_seconds", 8))

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
            purpose="capability-router.classify",
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


def _route_task(
    goal: str,
    requested_model: str,
    classify_fn: Optional[Callable],
) -> Optional[Dict[str, Any]]:
    """Run the capability router on a goal string.

    Returns {profile, model?, provider?} or None if routing failed / router
    unavailable / blocklist veto / recursion guard active.

    Best-effort: routing failure → caller falls through to normal delegation.
    Never blocks — all errors are caught.
    """
    # Recursion guard: don't re-enter the router during a classifier dispatch
    if os.environ.get(_ROUTER_SENTINEL):
        return None

    os.environ[_ROUTER_SENTINEL] = "1"
    try:
        if _LOADED_AS_PACKAGE:
            from .router.adapter import route
            from .router.blocklist import Blocklist
        else:  # direct source loading used by the development test harness
            from router.adapter import route
            from router.blocklist import Blocklist

        config = _load_router_config()
        if not config.get("enabled", False):
            return None

        blocklist = Blocklist(config)
        result = route(
            task=goal,
            config=config,
            requested_model=requested_model,
            classify_fn=classify_fn,
            blocklist=blocklist,
        )

        # Blocklist veto or pending classify action → no concrete target
        if result.get("deny") or result.get("action") == "classify":
            return None

        # Must have a profile to be useful
        if not result.get("profile"):
            return None

        return result
    except Exception as exc:
        logger.debug("capability-router: _route_task failed: %s", exc)
        return None
    finally:
        os.environ.pop(_ROUTER_SENTINEL, None)


def _record_breaker_outcome(
    profile: str,
    model: str,
    failure_kind: Optional[str],
) -> None:
    """Record delegate_profile outcome in the capability router's auto-breaker.

    Fire-and-forget — errors are logged but never propagated. The breaker
    lives in router/blocklist.py and uses router.yaml config.
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

        # Determine provider from the router config tiers
        provider = ""
        tiers = config.get("tiers", {})
        for _tier, tcfg in tiers.items():
            if tcfg.get("model") == model:
                provider = tcfg.get("provider", "")
                break

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
            _POOL = _Pool(_env_int("HERMES_DELEGATE_PROFILE_MAX_CONCURRENT",
                                   _DEFAULT_MAX_CONCURRENT))
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
        goal = (args.get("goal") or "").strip()
        context = (args.get("context") or "").strip()
        profile = (args.get("profile") or "").strip()
        model = (args.get("model") or "").strip()
        hard_timeout = _resolve_timeout(args.get("timeout"))

        if not goal:
            return json.dumps({"error": "goal is required", "failure_kind": "bad_args"})

        # --- Capability router: pick profile+model when not explicitly given ---
        routed_provider = ""
        routed_fallbacks: list = []
        if not profile or profile == "auto":
            routed = _route_task(goal, model, classify_fn)
            if routed is not None:
                profile = routed.get("profile", "") or profile
                if not model and routed.get("model"):
                    model = routed["model"]
                routed_provider = routed.get("provider", "") or ""
                fb = routed.get("fallback")
                if isinstance(fb, list):
                    routed_fallbacks = [x for x in fb if isinstance(x, dict) and x.get("model")]

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
        prompt = f"Context: {context}\n\nTask: {goal}" if context else goal

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
        queue_wait = _env_float("HERMES_DELEGATE_PROFILE_QUEUE_WAIT", 0.0)
        if not pool.acquire(queue_wait if queue_wait > 0 else hard):
            return json.dumps({
                "success": False, "failure_kind": "at_capacity", "retryable": True,
                "error": (
                    "Too many concurrent delegate_profile subprocesses "
                    f"(cap={_env_int('HERMES_DELEGATE_PROFILE_MAX_CONCURRENT', _DEFAULT_MAX_CONCURRENT)}). "
                    "Retry shortly or raise HERMES_DELEGATE_PROFILE_MAX_CONCURRENT."
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
            _record_breaker_outcome(profile, attempt_model, failure_kind)
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
            # Target chain: primary (routed/explicit) then the router's cross-rail
            # fallbacks. Retry the NEXT target only on a retryable failure — so a
            # Mac-only primary (Claude Code) transparently fails over to a non-Mac
            # rail, honoring 'Claude Code is never the sole option' at EXECUTION time.
            targets = [(model, routed_provider)] + [
                (fb.get("model", ""), fb.get("provider", "")) for fb in routed_fallbacks
            ]
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
def _on_post_tool_call(tool_name: str, params: dict, result: str, **_kwargs: Any) -> None:
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
                        "Absolute hard-ceiling seconds for the subprocess. "
                        "Default: 300 (5 min). Independent tighter watchdogs "
                        "also apply: no-first-output (TTFB) and inter-output "
                        "idle. Override the ceiling globally with "
                        "HERMES_DELEGATE_PROFILE_TIMEOUT; TTFB/idle via "
                        "HERMES_DELEGATE_PROFILE_TTFB / _IDLE."
                    ),
                },
            },
            "required": ["goal"],
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

    logger.info(
        "delegate-profile plugin registered (profile=%s)", current_profile,
    )
