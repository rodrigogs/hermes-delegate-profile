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
