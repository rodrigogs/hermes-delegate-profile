"""Run the JavaScript test suites, so they cannot rot unnoticed.

This exists because they did. tests/test_router_nav_mount.js sat 4/4 failing on
disk and on the deployed box — it had been written against a router-nav.js that
owned its own navigation, that responsibility moved to the shared
hermes-panel-nav module, and the file kept passing CI for the only reason that
matters: nothing ran it. pytest collects `test_*.py`, and the JS suites are
invoked by hand, which means they are invoked when someone remembers.

Discovery is by glob rather than by list, so a new suite is picked up without an
edit here — the failure mode being avoided is precisely a suite that exists and is
never executed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


TESTS = Path(__file__).resolve().parent
SUITES = sorted(TESTS.glob("test_*.js"))


def test_javascript_suites_are_discovered():
    """A guard on the guard: if the glob finds nothing, this file is theatre."""
    assert SUITES, f"no JavaScript suites found in {TESTS}"


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.name)
def test_javascript_suite_passes(suite: Path):
    node = shutil.which("node")
    if node is None:  # pragma: no cover - node is present in this deployment
        pytest.skip("node is not installed")

    # cwd is the plugin root: the suites read their subject with paths relative to
    # it (e.g. 'webui_extension/capability-router/console.html').
    result = subprocess.run(
        [node, "--test", str(suite)],
        cwd=TESTS.parent,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{suite.name} failed:\n{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
    )
