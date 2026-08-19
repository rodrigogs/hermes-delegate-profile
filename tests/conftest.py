"""Test isolation: never let a routing decision reach the real ssh subprocess.

The handler tests exercise routing/validation logic; a cross-profile route must
NOT invoke the real `_spawn` (it SSHes to the Mac gate / an out-of-credit rail and
would hang or bill). This autouse fixture stubs the spawn + watchdog on every live
copy of the plugin module so every test runs offline and fast, and FAILS the test
when it cannot stub — a guard that is quietly absent is worse than no guard,
because the suite reports green while dispatching real agents against a billed
rail.

What it intercepts is an *agent dispatch* (`hermes -p ... chat -q ...`), not every
subprocess: the `_spawn` / `_run_watched` / `_kill_tree` unit tests spawn local
`bash -c` children on purpose and assert on the real process group, so those pass
straight through to the real primitive. It deliberately does NOT touch
`_profile_exists` — individual tests set that to assert existence behavior.

Exactly one test is allowed to reach a real dispatch, and it has to ask twice: the
`real_spawn` marker (which test) plus `DELEGATE_PROFILE_E2E=1` (operator consent).
See `_e2e_opt_in` for why that scope is per test and not per process.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path, PurePath
from typing import Dict, List

import pytest


@pytest.fixture(autouse=True, scope="session")
def _seed_live_router_config():
    """Reproduce the production seeding: live router.yaml is generated from
    router.example.yaml on first load (_load_router_config does the same).

    In a fresh CI checkout router.yaml is absent (gitignored); tests that read
    the live policy (test_adapter._live_config, test_classifier_trust,
    test_one_sidecar_e2e) expect it to exist. Seeding here keeps the checkout
    clean (router.yaml stays gitignored) and the tests hermetic.
    """
    root = Path(__file__).resolve().parent.parent
    live = root / "router.yaml"
    example = root / "router.example.yaml"
    if not live.exists() and example.exists():
        live.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    yield


class _FakeProc:
    pid = 424242
    returncode = 0
    stdout = None
    stderr = None
    def poll(self): return 0
    def wait(self, timeout=None): return 0
    def kill(self): pass


class _SpawnGuard:
    """The installed guard, and the evidence that it is installed.

    Yielded by `_no_real_spawn` so a test can assert the *property* rather than
    trusting that a fixture ran: `guard.stub_for(dp) is dp._spawn` proves the stub
    landed on the copy the test actually calls, and `guard.blocked` is the argv of
    every agent dispatch that was intercepted instead of executed.

    `allow_real_dispatch` is this test's answer to "may a dispatch through", decided
    once at setup from the marker + the env opt-in and never re-read per call, so the
    scope of the escape hatch is a test and not the process. `dispatched` is the argv
    of every dispatch that was let through, which is empty for every test but the one
    that asked — assert it and the scope itself is under test.
    """

    def __init__(self, allow_real_dispatch: bool = False):
        self.modules: List[types.ModuleType] = []
        self.blocked: List[List[str]] = []
        self.dispatched: List[List[str]] = []
        self.allow_real_dispatch = allow_real_dispatch
        self._stubs: Dict[int, object] = {}

    def stub_for(self, mod):
        """The `_spawn` stub this guard installed on `mod`, or None."""
        return self._stubs.get(id(mod))


# The marker a test uses to say it wants the real `_spawn`; the grant the guard
# reads. `_E2E_ENV_VAR` is the operator's consent to honour that marker at all.
_REAL_SPAWN_MARKER = "real_spawn"
_E2E_ENV_VAR = "DELEGATE_PROFILE_E2E"


def _e2e_opt_in() -> bool:
    """Operator consent for a real cross-profile spawn — necessary, not sufficient.

    `test_e2e_cross_profile_spawn` is skipped unless `DELEGATE_PROFILE_E2E=1`, and it
    must not pass against a stub: that is a vacuous green, a rail test that never
    touched a rail. So the guard does have to stand down for it.

    What it must not do is stand down for the other 1666 tests. Reading this variable
    *inside* the stub, per dispatch, did exactly that: the variable ungated the whole
    process, so an operator asking for one deliberate spawn silently re-armed the
    original incident — with it set, `test_auto_profile_triggers_router` and
    `test_no_profile_triggers_router` would have run
    `hermes -p coder chat -q Debug race condition -m claude-opus --provider anthropic`
    and the `glm-5.2-fast` sibling for real, the first on a rail with no usable
    credential on the box, which hangs instead of failing fast.

    Scope therefore comes from the `real_spawn` marker (which test), and this variable
    is only consent to honour it (whether at all). Both are required, so neither one
    alone can un-guard a neighbour.
    """
    return os.environ.get(_E2E_ENV_VAR) == "1"


def _wants_real_spawn(node) -> bool:
    """True when this test carries the `real_spawn` marker."""
    return node.get_closest_marker(_REAL_SPAWN_MARKER) is not None


def _declares_e2e_opt_in(item) -> bool:
    """True when the item gates itself on `DELEGATE_PROFILE_E2E` in its own skipif.

    The condition is a plain bool by the time pytest sees it (`skipif` evaluates
    `os.environ.get(...) != "1"` at decoration), so the reason string — the operator-
    facing half of that gate — is what is left to read.
    """
    return any(
        _E2E_ENV_VAR in str(mark.kwargs.get("reason", ""))
        for mark in item.iter_markers("skipif")
    )


def pytest_configure(config):
    """Register `real_spawn` so a mis-spelled marker is an error, not a no-op.

    `--strict-markers` is on, so `@pytest.mark.real_spwan` fails collection instead
    of leaving the test quietly stubbed. A grant whose typo mode is silence is the
    same disease as a guard whose absence is silence.
    """
    config.addinivalue_line(
        "markers",
        f"{_REAL_SPAWN_MARKER}: this test wants the real `_spawn` (a real agent "
        f"dispatch); honoured only when {_E2E_ENV_VAR}=1, ignored otherwise.",
    )


def pytest_collection_modifyitems(config, items):
    """Put `real_spawn` on the tests that already gate themselves on the opt-in.

    Scope, not policy. The guard reads the marker; this hook gives it to the test
    whose own `skipif` reason names `DELEGATE_PROFILE_E2E`, which is the test that
    declared it wants a rail. Deriving the grant from that declaration rather than a
    hardcoded node id means the marker cannot drift onto a neighbour, and an explicit
    `@pytest.mark.real_spawn` in a test file is honoured as-is (this becomes a no-op
    for it).

    If the derivation ever stops matching, the failure would be silent in the worst
    direction — the opt-in test runs against a stub and passes — so the mismatch is a
    hard collection error rather than a skip. The name check is only a tripwire here;
    it never grants anything.
    """
    marked = [i.nodeid for i in items if _wants_real_spawn(i)]
    for item in items:
        if not _wants_real_spawn(item) and _declares_e2e_opt_in(item):
            item.add_marker(getattr(pytest.mark, _REAL_SPAWN_MARKER))
            marked.append(item.nodeid)
    if _e2e_opt_in() and not marked:
        suspects = [i.nodeid for i in items if "e2e" in i.name]
        if suspects:
            raise pytest.UsageError(
                f"{_E2E_ENV_VAR}=1 asks for a real spawn, but no collected test "
                f"claims one: {suspects} look like the opt-in test yet carry "
                f"neither @pytest.mark.{_REAL_SPAWN_MARKER} nor a skipif reason "
                f"naming {_E2E_ENV_VAR}. They would run against the guard's stub "
                f"and pass without touching a rail. Refusing to report that green."
            )


def _is_agent_dispatch(mod, cmd) -> bool:
    """True when argv is a hermes agent dispatch — the one thing that must not run.

    Every dispatch the plugin builds is ``[<...>/hermes, "-p", profile, "chat", ...]``
    (`_resolve_hermes_bin` returns either the interpreter-adjacent `hermes` or the
    bare name for a PATH lookup), so the program's basename decides it. `HERMES_BIN`
    is read off the module rather than hardcoded, so a test that repoints it is
    still covered.

    Anything else is a local child a test created on purpose — the `_spawn` /
    `_run_watched` / `_kill_tree` unit tests spawn `bash -c ...` and then assert on
    the real process group. Those were never covered here (the guard never ran at
    all), and stubbing them out would replace eight real assertions with vacuous
    passes, which is the same disease as widening `failure_kind`.
    """
    try:
        program = str(list(cmd)[0])
    except (TypeError, ValueError, IndexError):
        return True  # unreadable argv: refuse to run it rather than guess
    names = {"hermes", "hermes.exe", str(getattr(mod, "HERMES_BIN", "hermes"))}
    return PurePath(program).name in {PurePath(n).name for n in names}


def _make_spawn_stub(mod, real_spawn, guard: _SpawnGuard):
    """`_spawn` replacement for one plugin copy: block dispatches, pass the rest.

    The dispatch verdict reads `guard.allow_real_dispatch`, which this test's setup
    already decided; it is deliberately not an env lookup, because a lookup here is
    per dispatch and would answer the same for every test in the process.
    """
    def _stub(cmd, env):
        if _is_agent_dispatch(mod, cmd):
            if not guard.allow_real_dispatch:
                guard.blocked.append(list(cmd))
                return _FakeProc()
            guard.dispatched.append(list(cmd))
        return real_spawn(cmd, env)
    return _stub


def _make_watch_stub(real_run_watched):
    """`_run_watched` replacement: canned verdict for a blocked spawn only.

    A `_FakeProc` came from this guard, so there is nothing to watch and the
    stubbed 4-tuple stands in. A real process reaching here belongs to a watchdog
    unit test, which needs the real ladder.
    """
    def _stub(proc, pgid, ttfb, idle, hard, grace):
        if isinstance(proc, _FakeProc):
            return ("exited", 0, "(stubbed)", "")
        return real_run_watched(proc, pgid, ttfb, idle, hard, grace)
    return _stub


# Identity of the module this guard protects is its source file, never its name.
_PLUGIN_INIT = Path(__file__).resolve().parent.parent / "__init__.py"

# The name to register under when nothing has loaded the plugin yet — the one the
# rest of the suite looks for.
_PLUGIN_MODULE_NAME = "delegate_profile_plugin"

# Resolving every `__file__` in sys.modules on every test is thousands of stats;
# the answer per raw path string never changes within a run.
_IS_PLUGIN_PATH: Dict[str, bool] = {}

_UNSAFE_RUN = (
    "REAL SPAWNS ARE POSSIBLE — treat this run as UNSAFE and do not trust a green "
    "result. The _no_real_spawn guard could not stub delegate_profile's `_spawn`, "
    "so a routing decision in this test can reach the real subprocess: it SSHes to "
    "the Mac gate / a billed rail, and will hang or spend money. "
)

# Same headline, different cause: the stubs went on, but the escape hatch they honour
# is wider than the one test that asked for it.
_UNSAFE_SCOPE = (
    "REAL SPAWNS ARE POSSIBLE — treat this run as UNSAFE and do not trust a green "
    "result. The _no_real_spawn guard stubbed delegate_profile's `_spawn` but with "
    "the wrong scope, so a routing decision in this test can reach the real "
    "subprocess: it SSHes to the Mac gate / a billed rail, and will hang or spend "
    "money. "
)

# A dispatch-shaped argv whose program cannot exist, used to exercise each installed
# stub at setup. `_is_agent_dispatch` matches on the basename, so this is an agent
# dispatch as far as the guard is concerned, while reaching the real primitive with
# it can only ever fail to exec — the canary proves which side of the fence the stub
# is on without ever starting a process.
_CANARY_ARGV = ["/nonexistent-delegate-profile-canary/hermes", "-p", "canary",
                "chat", "-q", "canary"]


def _reap_canary(proc) -> None:
    """Kill, reap and close a process the canary somehow started.

    The canary's program cannot exist, so on a real `_spawn` this is unreachable; it
    exists so that a harness (or a test) which redirects the exec to something real
    cannot leave a child or an open pipe behind. `Popen.wait` reaps but deliberately
    leaves the parent-side fds open, and `filterwarnings = error` turns a leaked one
    into a failure attributed to whatever test runs next.
    """
    for step in (lambda: proc.kill(), lambda: proc.wait(timeout=5)):
        try:
            step()
        except Exception:
            pass
    for pipe in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            pipe.close()
        except Exception:
            pass


def _canary_reaches_real_spawn(stub, forced_opt_in: bool) -> bool:
    """Call `stub` with `_CANARY_ARGV` and report whether the real `_spawn` ran.

    `forced_opt_in` temporarily sets `DELEGATE_PROFILE_E2E=1` for the duration of the
    call, and is used for the unmarked tests. The opt-in is the input that used to
    un-guard the entire process, so the check has to be made with it set — otherwise
    it would pass against the very code it exists to detect.
    """
    prev = os.environ.get(_E2E_ENV_VAR)
    if forced_opt_in:
        os.environ[_E2E_ENV_VAR] = "1"
    try:
        proc = stub(_CANARY_ARGV, {})
    except Exception:
        # FileNotFoundError from exec'ing a path that cannot exist: whatever raised,
        # the call left the stub and ran the real primitive. (KeyboardInterrupt is
        # deliberately not caught — a Ctrl-C during setup is not a verdict.)
        return True
    else:
        if isinstance(proc, _FakeProc):
            return False
        _reap_canary(proc)
        return True
    finally:
        if forced_opt_in:
            if prev is None:
                os.environ.pop(_E2E_ENV_VAR, None)
            else:
                os.environ[_E2E_ENV_VAR] = prev


def _assert_scope_holds(mod, stub, guard: _SpawnGuard) -> None:
    """Prove, per test and per copy, that the escape hatch has the scope claimed.

    The two failures this catches are the two that report green:

    * an unmarked test whose stub lets a dispatch through *when the opt-in is set*.
      That is the defect this scope fix repairs — the opt-in was read per dispatch,
      so one operator asking for one deliberate spawn re-armed the whole suite. The
      canary forces the variable on, so the check fails on any regression to that
      shape even in a normal run where the operator set nothing.
    * a marked+consented test whose stub still fakes the dispatch. Then
      `test_e2e_cross_profile_spawn` passes without touching a rail, which is the
      vacuous green the opt-in exists to avoid.

    Asserting the property beats asserting that the fixture ran: this is the same
    stub, on the same module object, that the test body will call.
    """
    reached = _canary_reaches_real_spawn(stub, forced_opt_in=not guard.allow_real_dispatch)
    # The canary is guard bookkeeping, not something the test did.
    for seen in (guard.blocked, guard.dispatched):
        if seen and seen[-1] == _CANARY_ARGV:
            seen.pop()
    if guard.allow_real_dispatch and not reached:
        pytest.fail(
            f"This test asked for a real dispatch (@pytest.mark.{_REAL_SPAWN_MARKER} "
            f"+ {_E2E_ENV_VAR}=1) but the guard's stub on {mod.__name__!r} "
            f"({mod.__file__}) faked it anyway. A rail test that cannot reach a rail "
            f"passes vacuously — fix the guard rather than trusting this green.",
            pytrace=False,
        )
    if not guard.allow_real_dispatch and reached:
        pytest.fail(
            f"{_UNSAFE_SCOPE}Scope regression: with {_E2E_ENV_VAR}=1 set, the stub on "
            f"{mod.__name__!r} ({mod.__file__}) passed an agent dispatch through for "
            f"a test that never asked for one. The opt-in belongs to the single test "
            f"carrying @pytest.mark.{_REAL_SPAWN_MARKER}, not to the process.",
            pytrace=False,
        )


def _is_plugin_module(obj: object) -> bool:
    """True when `obj` is a module whose source file IS the plugin `__init__.py`."""
    if not isinstance(obj, types.ModuleType):
        return False
    raw = getattr(obj, "__file__", None)
    if not raw:
        return False
    known = _IS_PLUGIN_PATH.get(raw)
    if known is None:
        try:
            known = Path(raw).resolve() == _PLUGIN_INIT
        except OSError:  # pragma: no cover - unreadable path, definitionally not ours
            known = False
        _IS_PLUGIN_PATH[raw] = known
    return known


def _plugin_copies(request) -> List[types.ModuleType]:
    """Every live copy of the plugin the current test could route through.

    The old guard did `sys.modules.get("delegate_profile_plugin")`. Nothing in the
    suite ever registers that name: every test file builds the module with
    `spec_from_file_location` + `module_from_spec` + `exec_module`, which does not
    touch `sys.modules`. So the lookup returned None and the guard stubbed nothing,
    for every test in every file — silently. Hence: match on resolved source path,
    and search the three name-agnostic places a copy can be hiding.

    * `sys.modules` — the copies that *are* registered: the `hermes_plugins.*`
      names some tests create deliberately to exercise the package import shape,
      and the `hdp2` package copy pytest imports itself. There is legitimately
      more than one, and each has its own `_spawn` global, so stub them all.
    * `request.node.funcargs` — fixture-held copies.
      `tests/test_router_integration.py` builds its copy in a module-scoped `dp`
      fixture. Module scope is what makes it reachable: pytest sets higher-scoped
      fixtures up before function-scoped ones, so `dp` is already built and cached
      by the time this guard runs, and its value is the object those tests call.
    * the test module's globals — `test_delegate_profile.py`,
      `test_classifier_trust.py` and `test_delegate_profile_runtime.py` each exec
      the plugin into a module-level `dp` / `_dp` at import time.
    """
    candidates: List[object] = list(sys.modules.values())
    candidates.extend((getattr(request.node, "funcargs", None) or {}).values())
    module = getattr(request, "module", None)
    if module is not None:
        candidates.extend(vars(module).values())

    # Dedupe by identity: the same copy shows up under several of the routes above.
    unique: Dict[int, types.ModuleType] = {}
    for obj in candidates:
        if _is_plugin_module(obj):
            unique.setdefault(id(obj), obj)  # type: ignore[arg-type]
    return list(unique.values())


def _load_plugin_module() -> types.ModuleType:
    """Load + register the plugin ourselves when nothing else has loaded it.

    `submodule_search_locations` mirrors how the installed plugin is imported, so
    the relative `router.*` imports inside it resolve. Registering before
    `exec_module` is what the import system does; on failure we take the name back
    out rather than leave a half-executed module for the next test to find.
    """
    spec = importlib.util.spec_from_file_location(
        _PLUGIN_MODULE_NAME,
        _PLUGIN_INIT,
        submodule_search_locations=[str(_PLUGIN_INIT.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"no import spec for {_PLUGIN_INIT}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PLUGIN_MODULE_NAME] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(_PLUGIN_MODULE_NAME, None)
        raise
    return mod


@pytest.fixture(autouse=True)
def _no_real_spawn(request, monkeypatch):
    """Stub spawn + watchdog on every live plugin copy, or fail the test loudly.

    Yields the `_SpawnGuard`, so a test can assert the guard actually landed on the
    copy it will call (`_no_real_spawn.stub_for(dp) is dp._spawn`), that no dispatch
    was attempted (`_no_real_spawn.blocked == []`), and that this test is not one of
    the ones allowed a rail (`_no_real_spawn.allow_real_dispatch is False`).

    The escape hatch is resolved here, once, from *this* node's marker and the env
    opt-in — so an operator setting `DELEGATE_PROFILE_E2E=1` moves exactly one test
    off the stub and leaves the rest of the suite guarded.
    """
    guard = _SpawnGuard(
        allow_real_dispatch=_e2e_opt_in() and _wants_real_spawn(request.node)
    )

    targets = _plugin_copies(request)
    if not targets:
        try:
            targets = [_load_plugin_module()]
        except BaseException as exc:
            pytest.fail(
                f"{_UNSAFE_RUN}Nothing had loaded the plugin and loading "
                f"{_PLUGIN_INIT} failed: {exc!r}",
                pytrace=False,
            )

    for mod in targets:
        real_spawn = getattr(mod, "_spawn", None)
        real_watch = getattr(mod, "_run_watched", None)
        if not callable(real_spawn) or not callable(real_watch):
            # Either this is not the module we think it is, or the plugin renamed
            # its seams. Both mean the guard can no longer promise anything.
            pytest.fail(
                f"{_UNSAFE_RUN}{mod.__name__!r} ({mod.__file__}) has no callable "
                f"_spawn/_run_watched to stub: {real_spawn!r} / {real_watch!r}.",
                pytrace=False,
            )
        # Per copy, because each stub falls back to *its own* module's `_spawn`:
        # the real one reads `IS_WINDOWS` and `subprocess` from its own globals,
        # which is what the primitive's unit tests monkeypatch.
        spawn_stub = _make_spawn_stub(mod, real_spawn, guard)
        watch_stub = _make_watch_stub(real_watch)
        try:
            monkeypatch.setattr(mod, "_spawn", spawn_stub, raising=False)
            monkeypatch.setattr(mod, "_run_watched", watch_stub, raising=False)
        except Exception as exc:
            pytest.fail(
                f"{_UNSAFE_RUN}Could not set the stubs on {mod.__name__!r} "
                f"({mod.__file__}): {exc!r}",
                pytrace=False,
            )
        # A setattr that "succeeded" but left the real function reachable is exactly
        # the silent no-op this fixture exists to prevent, so check, don't assume.
        if mod._spawn is not spawn_stub or mod._run_watched is not watch_stub:
            pytest.fail(
                f"{_UNSAFE_RUN}Stubs did not stick on {mod.__name__!r} "
                f"({mod.__file__}): _spawn is {mod._spawn!r}.",
                pytrace=False,
            )
        # ...and a stub that stuck but has the wrong scope is the defect this fixture
        # was just repaired for, so exercise it rather than reason about it.
        _assert_scope_holds(mod, spawn_stub, guard)
        guard.modules.append(mod)
        guard._stubs[id(mod)] = spawn_stub

    yield guard


@pytest.fixture(autouse=True)
def _isolate_route_trace(tmp_path, monkeypatch):
    """Keep test routing decisions out of the operator's live trace.

    ``durable_decision_log.routes_path()`` resolves to a profile-independent file
    under HERMES_HOME so the plugin and the sidecar converge on one log. That is
    right in production and wrong under test: the suite drives real routing
    decisions, so every run appended to the live file. Measured: one run of
    tests/test_router_integration.py added 10 entries, and the operator's trace
    held 517 of them across 10 distinct synthetic tasks - a replay surface showing
    almost nothing but test fixtures, which is worse than showing nothing.

    HERMES_ROUTE_TRACE_FILE is the override the module already honours, so
    pointing it at tmp_path needs no production change.

    Tests that steer the path themselves - by setting HERMES_HOME to assert the
    profile-peeling logic - must win over this blanket guard, so they delete the
    variable. monkeypatch.delenv in the test unwinds cleanly at teardown.
    """
    monkeypatch.setenv("HERMES_ROUTE_TRACE_FILE", str(tmp_path / "routes.jsonl"))
    # Isolate watchdog config from the operator's live config.yaml.
    # The plugin resolves watchdog params via cfg_get(plugins.entries.delegate-profile.watchdog);
    # tests expect module defaults (or env overrides), not the operator's live values.
    # Stub hermes_cli.config.cfg_get (imported lazily inside _watchdog_cfg) to return {}.
    try:
        import hermes_cli.config as _hcfg
        monkeypatch.setattr(_hcfg, "cfg_get", lambda *a, **k: {}, raising=False)
    except Exception:
        pass
    yield
