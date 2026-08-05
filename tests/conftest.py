"""Test isolation: never let a routing decision reach the real ssh subprocess.

The handler tests exercise routing/validation logic; a cross-profile route must
NOT invoke the real `_spawn` (it SSHes to the Mac gate / an out-of-credit rail and
would hang or bill). This autouse fixture stubs the spawn + watchdog on the shared
plugin module so every test runs offline and fast. It deliberately does NOT touch
`_profile_exists` — individual tests set that to assert existence behavior.
"""
import sys
import pytest


class _FakeProc:
    pid = 424242
    returncode = 0
    stdout = None
    stderr = None
    def poll(self): return 0
    def wait(self, timeout=None): return 0
    def kill(self): pass


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    mod = sys.modules.get("delegate_profile_plugin")
    if mod is not None:
        monkeypatch.setattr(mod, "_spawn", lambda cmd, env: _FakeProc(), raising=False)
        monkeypatch.setattr(
            mod, "_run_watched",
            lambda proc, pgid, ttfb, idle, hard, grace: ("exited", 0, "(stubbed)", ""),
            raising=False,
        )
    yield

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
