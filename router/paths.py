"""Where this plugin's state lives on disk — the ONE place that decides.

The rule is one sentence: state is keyed to the Hermes ROOT, never to a profile.

The plugin runs with a PROFILE-SCOPED ``HERMES_HOME``
(``~/.hermes/profiles/<name>``) that varies per delegation, while the sidecar is
pinned to one profile and the CLI to whatever shell invoked it. Resolving state
under the profile therefore split every reader from every writer: a rail failing
for ``trama-engineer`` kept getting traffic from ``coder`` because its cooldown
lived in a file the other profile never read, and the breaker never accumulated to
its threshold at all.

WHY THIS MODULE EXISTS. That resolution was typed TWICE — once in
``blocklist._state_dir`` and once in ``durable_decision_log.routes_path`` — and the
divergence already shipped once: ``d0802d6`` added the peel to the trace path, and
``305a901`` added it to the breaker path FOUR WEEKS LATER, with a commit message
describing the production bug the gap had caused in the meantime. The 2026-08-27
rename then had to touch both literals by hand. Two copies of one path is the same
defect class as two copies of one table.

WHAT IS DELIBERATELY *NOT* HERE. ``HERMES_ROUTE_TRACE_FILE`` stays local to
``routes_path``. It is an override for the TRACE FILE, and breaker state must not
follow it: pointing a test or a unit at another trace file would otherwise orphan
``breaker-state.json`` somewhere with no read fallback, silently emptying every
cooldown. One override, one file.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Directory name under the Hermes root. Renamed from ``delegate-profile`` on
#: 2026-08-27 with the on-disk history migrated by hand and, deliberately, NO read
#: fallback to the old name: one path, one answer.
STATE_DIR_NAME = "hermes-smart-router"


def hermes_root() -> Path:
    """``HERMES_HOME`` with any trailing ``profiles/<name>`` peeled off.

    Defaults to ``~/.hermes``. The peel is what makes every profile and every
    reader converge on one directory.
    """
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home


def state_dir() -> Path:
    """The profile-independent directory holding this plugin's mutable state."""
    return hermes_root() / STATE_DIR_NAME / "state"
