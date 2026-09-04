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


#: Env override for the POLICY file, mirroring ``HERMES_CORE_CONFIG_FILE`` for the
#: agent config and ``HERMES_ROUTE_TRACE_FILE`` for the trace. Explicit always wins.
POLICY_ENV = "HERMES_ROUTER_CONFIG_FILE"


def resolve_policy_path(plugin_dir: Path) -> Path:
    """Which ``router.yaml`` this install routes on — the ONE place that decides.

    THE DEFECT THIS CLOSES, measured on the docker stack 2026-09-04 within minutes of
    the plugin loading for the first time: the plugin read ``<plugin_dir>/router.yaml``
    while the sidecar was started with ``--config /data/hermes/router.yaml``. In that
    stack the plugin directory is a SYMLINK into the image's source clone, where no
    policy exists, so the plugin seeded one from ``router.example.yaml`` and routed on
    it. The trace proves it: five decisions naming ``gpt-5.6-terra/openai-codex``,
    ``glm-5.3-flash/zai`` and ``mimo-v2.5/xiaomi`` — the example's rails, none of them
    reachable on that install and none of them in the policy the operator was editing.

    So the console showed one document, the write path wrote it, and dispatch obeyed a
    different one. Nothing detected it because nothing had ever compared them: until the
    manifest name was fixed, the plugin never loaded at all.

    Precedence, and each rung is load-bearing on a real layout:

    1. ``HERMES_ROUTER_CONFIG_FILE`` — explicit wins, always, existing or not, so a test
       or a unit can point at a file it is about to create.
    2. ``hermes_root()/router.yaml`` WHEN IT EXISTS — the docker layout. HERMES_HOME is
       already the authority every other path here derives from (``state_dir``, the
       trace, the breaker), and that is exactly the file the stack's sidecar is pointed
       at.
    3. ``<plugin_dir>/router.yaml`` — the WSL layout, where this IS the operator's
       policy and the sidecar unit passes that very path. Measured there: no
       ``~/.hermes/router.yaml`` exists, so rung 2 misses and this one answers, leaving
       that install byte-for-byte unaffected.

    Rung 3 is also the SEED TARGET when neither exists, which keeps the documented
    first-run behaviour (seed from the tracked example, then it belongs to the operator).
    """
    explicit = os.environ.get(POLICY_ENV)
    if explicit:
        return Path(explicit)
    rooted = hermes_root() / "router.yaml"
    if rooted.exists():
        return rooted
    return Path(plugin_dir) / "router.yaml"
