"""Static contract tests for the Hermes One extension assets.

Plus the dashboard plugin API's SHAPE PARITY with ``RouterService`` (bottom of
the file). The extension mounts the console and the console reads the plugin API,
so a key the service reports and the plugin API drops renders as an empty panel —
which an operator reads as "the feature is broken". The same goes for a plan: the
two surfaces must answer the same question at the same instant, or the operator
is comparing a preview against a production route that never matched it. Those
tests therefore live next to the extension contract they hold up.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "webui_extension" / "hermes-one-capability-router"


def _node() -> str:
    """The node binary, or a skip.

    A bare "node" in subprocess.run raises FileNotFoundError, which reads as a
    failing test rather than an unavailable tool - and the deployment keeps node
    in ~/.local/bin, which is not on the PATH of every runner that reaches these
    tests. A syntax check that cannot run is a skip; it is not a syntax error.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node

def test_extension_manifest_declares_token_v1_sidecar():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "hermes-one-capability-router"
    assert manifest["scripts"] == ["router-nav.js"]
    assert manifest["stylesheets"] == ["router-nav.css"]
    assert manifest["sidecar"] == {
        "type": "loopback",
        "origin": "http://127.0.0.1:8791",
        "health_path": "/health",
        "proxy_auth": "token-v1",
    }


def test_extension_script_mounts_the_console_instead_of_duplicating_it():
    """The extension script is navigation plus a mount, not a second UI.

    It once reimplemented every read the console does, so the two surfaces could
    disagree. What is load-bearing now is the mount itself: the WebUI grants its
    CSRF token only to pages it renders, so the console can only write when it
    runs inside the host document — hence srcdoc (the served page sends
    X-Frame-Options DENY, so it cannot be framed by URL).
    """
    script_path = EXTENSION / "router-nav.js"
    script = script_path.read_text(encoding="utf-8")
    for block in ("THESIS:", "OWN-WORLD:", "STORY:", "FIRST VIEWPORT:", "FORM:"):
        assert block in script
    assert "srcdoc" in script, "the console must be framed same-origin"
    assert "main-view" in script, "it mounts in the host's central panel"
    assert "/console" in script
    assert "frame.src =" not in script, "a URL-framed console is refused by the sidecar"
    # The duplicate reader UI must not creep back.
    for gone in ("/status", "/blocklist", "renderPolicyTab", "postJSON"):
        assert gone not in script, f"{gone} belongs to the console, not to the mount"
    # This file's own accessibility surface is the error state it renders — the one
    # thing it puts on screen. The nav BUTTON's aria-label and the MutationObserver
    # that installs it moved to the shared hermes-panel-nav module, where all three
    # surfaces get them from one implementation; asserting them here would pin a
    # copy that no longer exists and quietly stop testing the real one.
    for accessibility_hook in ("aria-live", "setAttribute('role', 'alert')"):
        assert accessibility_hook in script
    assert "HermesPanelNav" in script, "navigation comes from the shared module"
    assert "window.HermesPanelNav" in script and "console.error" in script, (
        "a missing shared module must say so: without the guard the script dies "
        "before installing anything and the symptom is a button that is not there"
    )
    for destructive_pattern in (
        "document.body.innerHTML",
        "document.querySelector('main').innerHTML",
        "document.querySelector(\"main\").innerHTML",
        "eval(",
        "new Function",
    ):
        assert destructive_pattern not in script
    assert "textContent" in script
    checked = subprocess.run(
        [_node(), "--check", str(script_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


def _console_inline_script() -> str:
    """Return the single inline <script> body of the impeccable console."""
    import re

    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    assert match, "console.html must contain exactly one inline <script>"
    return match.group(1)


def test_console_html_is_xss_safe_and_syntax_valid(tmp_path):
    """The console renders persisted, attacker-influenceable route/task text in
    replay, so its inline script must never use raw-markup sinks and must render
    via textContent. This guards the highest-XSS-surface code in the project.
    """
    script = _console_inline_script()
    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "eval(", "new Function", "document.write"):
        assert forbidden not in script, f"console.html inline script must not use {forbidden}"
    assert "textContent" in script
    # The replay wiring must be present: it reads real recorded traces and draws
    # the path they took, with no charting dependency.
    for token in ("drawPath", "/routes", "stepOutcome", "renderStep"):
        assert token in script, f"console.html must wire {token}"
    # Syntax must be valid (write the extracted body to a temp file for node --check).
    script_file = tmp_path / "console_inline.js"
    script_file.write_text(script, encoding="utf-8")
    checked = subprocess.run(
        [_node(), "--check", str(script_file)],
        text=True, capture_output=True, check=False,
    )
    assert checked.returncode == 0, checked.stderr


def test_console_html_declares_its_three_screens_and_their_surfaces():
    """The console is three screens; each must exist with the hooks its screen
    needs. Both Pipeline and Routes read as ordered vertical sequences — a policy
    is a first-match table and a trace is a short path, so neither is a free-form
    canvas and the operator learns one way of reading this console."""
    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    for tab in ("health", "pipeline", "routes"):
        assert f'data-tab="{tab}"' in html
        assert f'id="panel-{tab}"' in html
    assert 'id="sheet"' in html, "the Pipeline screen is the ordered decision sheet"
    assert 'id="probeTask"' in html, "an operator must be able to try a task"
    assert 'id="ladder"' in html, "the capability ladder shows where tasks can land"
    assert 'id="routesTable"' in html
    assert 'id="replayPath"' in html, "replay lists the steps a real decision took"
    assert "<svg" not in html, "no canvas survives: both screens are read as lists"


def test_extension_css_only_dresses_the_nav_button():
    """This stylesheet's whole job is the rail button.

    It used to style a five-tab panel; then it styled the panel and the frame. Both
    of those now live in the shared hermes-panel.css that every surface loads, so
    anything about .hermes-panel, .hp-frame or .hp-error asserted HERE would pin a
    duplicate — which is precisely how the three surfaces drifted into looking like
    three different products.
    """
    import re

    css = (EXTENSION / "router-nav.css").read_text(encoding="utf-8")

    assert ".hermes-one-capability-router-nav" in css
    assert "hover" in css, "the nav button needs a hover state"
    assert "@media" in css, "the sidebar label collapses on a narrow shell"
    # Colour means state in this system, so a nav button uses contrast, not a hue.
    assert "var(--muted)" in css and "var(--text)" in css
    assert "#8fb2ff" not in css, "the hard-coded brand blue is gone"

    # This button lives in the HOST document, not inside a panel, so it can only
    # use tokens the host itself defines. Measured live on the running WebUI, the
    # host sets --bg, --text, --muted and --accent at :root and does NOT define
    # --surface-raised or --faint — so a rule written against those resolved to
    # nothing and the hover state silently did nothing at all. The panel tokens
    # are only in scope inside .hermes-panel.
    HOST_DEFINES = {"--bg", "--text", "--muted", "--accent"}
    used = set(re.findall(r"var\((--[a-z-]+)", css))
    assert used <= HOST_DEFINES, (
        f"{used - HOST_DEFINES} are panel tokens, undefined in the host document"
    )

    # The shared contract must NOT be restated here.
    for shared in (".hp-frame", ".hp-error", "main > .main-view", "prefers-reduced-motion"):
        assert shared not in css, f"{shared} belongs to hermes-panel.css, not here"


# ── Dashboard plugin API ↔ RouterService shape parity ────────────────
#
# fastapi is a Hermes-dashboard dependency, not one of this plugin's (pyyaml
# only), so these skip cleanly where the dashboard is not installed rather than
# forcing a new dependency on the router package.

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from dashboard import plugin_api  # noqa: E402
from router.decision_log import empty_chain_plan  # noqa: E402
from router.service import RouterService  # noqa: E402


# Valid policy (lint returns []) that nonetheless produces ONE advisory warning:
# T2 names an elo the capability registry has never heard of, which is
# unverifiable, not wrong. This is the config that proves warnings and
# validation_errors are separate axes.
_WARNING_BUT_VALID = {
    "enabled": True,
    "default": {"model": "T1"},
    "rules": [],
    "tiers": {
        "T1": {
            "model": "glm-4.7",
            "provider": "zai",
            "billing_mode": "plan",
            "fallback_strategy": "sequential",
            "pin_primary": True,
            "requirements": {"tool_calling": True},
            "time_policy": {"avoid_peak": ["zai", "deepseek"], "prefer": ["mimo-v2.5"]},
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [
                {"model": "deepseek-v4-flash", "provider": "deepseek",
                 "billing_mode": "metered"},
            ],
        },
        "T2": {"model": "made-up-elo-9000", "provider": "acme", "fallback": []},
        "T3": {"model": "glm-4.7", "provider": "zai"},
        "T4": {"model": "glm-4.7", "provider": "zai"},
    },
}

# The row router.yaml invites operators to enable: heavy work is pushed down a
# tier during the 06:00-10:00 UTC peak, when deepseek and zai both bill at 2.0x.
# It is keyed on utc_hour, which signals.extract() cannot produce — the edge
# INJECTS it — so this policy is the direct probe for "did the clock arrive?".
_TIME_KEYED = {
    "enabled": True,
    "default": {"model": "T1"},
    "rules": [
        {
            "id": "defer-heavy-work-off-peak",
            "when": {"utc_hour": {"gte": 6, "lt": 10}, "verb_class": {"eq": "hard"}},
            "then": {"model": "T3"},
        },
    ],
    "tiers": {
        "T1": {
            "model": "glm-4.7",
            "provider": "zai",
            "billing_mode": "plan",
            "fallback_strategy": "cheapest_now",
            "time_cap": {"max_multiplier": 1.5},
            "fallback": [
                {"model": "gpt-5.6-luna", "provider": "openai-codex"},
                {"model": "mimo-v2.5", "provider": "xiaomi"},
            ],
        },
        "T2": {"model": "glm-5.3", "provider": "zai"},
        "T3": {"model": "mimo-v2.5", "provider": "xiaomi"},
        "T4": {"model": "glm-4.7", "provider": "zai"},
    },
}

# verb_class == "hard", so the time-keyed row's second clause holds and only the
# hour decides whether it fires.
_HARD_TASK = "Debug a race condition across 3 files in the scheduler"

# verb_class == "trivial": the time-keyed row never matches it, so it falls through
# to the default T1 — the tier carrying cheapest_now and the 1.5x ceiling, which is
# where the price window is observable.
_TRIVIAL_TASK = "fix typo in the code function"

# Monday 07:30 UTC — inside the peak, and a WEEKDAY, because the zai peak is
# weekday-gated. 07:30 rather than 07:00 so the hour truncation is exercised too.
_PEAK = "2026-08-17T07:30:00Z"
_OFF_PEAK = "2026-08-17T15:00:00Z"


@pytest.fixture
def plugin_config(tmp_path, monkeypatch):
    """Point the plugin API at a throwaway router.yaml and return a writer."""
    path = tmp_path / "router.yaml"

    def write(config):
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return path

    write(_WARNING_BUT_VALID)
    monkeypatch.setattr(plugin_api, "_CONFIG_PATH", path)
    return write


def _service_explain(task, at):
    """``RouterService.explain`` for the same task and the same instant.

    ``at`` is forwarded only when the installed service declares it, so this
    parity assertion still MEANS something against a service predating the
    injected clock instead of dying on an unexpected keyword — the plugin and the
    service are then compared as the two time-agnostic surfaces they both are.
    """
    service = RouterService(plugin_api._CONFIG_PATH)
    if "at" in inspect.signature(service.explain).parameters:
        return service.explain(task, at=at)
    return service.explain(task)


def _plan_of(payload):
    """The chain plan of an explain payload, from either surface's shape."""
    for candidate in (payload.get("chain_plan"),
                      (payload.get("decision") or {}).get("chain_plan")):
        if isinstance(candidate, dict):
            return candidate
    return empty_chain_plan()


def test_plugin_status_carries_warnings_and_errors_as_separate_axes(plugin_config):
    """A warning informs; only validation_errors may flip valid.

    The console renders the two in different places. Merged — or with warnings
    absent, as this endpoint shipped — an advisory finding either reads as a
    broken router or vanishes entirely.
    """
    status = asyncio.run(plugin_api.api_status())

    assert "validation_errors" in status and "warnings" in status
    assert status["validation_errors"] == []
    assert status["valid"] is True, "a warning must never flip valid to false"
    assert isinstance(status["warnings"], list)
    assert any("made-up-elo-9000" in w for w in status["warnings"]), (
        "an elo the capability registry cannot describe is advisory, not an error"
    )
    # Parity with the service the console's other surface reads.
    service_status = RouterService(plugin_api._CONFIG_PATH).status()
    assert set(service_status) <= set(status)
    for key in ("valid", "validation_errors", "enabled", "tiers"):
        assert status[key] == service_status[key]
    assert status["warnings"] == service_status.get("warnings", [])
    # Legacy keys the bundled dashboard UI reads are still served.
    assert status["rules_count"] == 0
    assert status["banned_models"] == []
    assert "classifier_model" in status


def test_plugin_status_never_lets_a_warning_flip_valid(plugin_config, monkeypatch):
    """A non-empty advisory list is passed through and changes nothing else.

    Stubbing the service's warnings is the only way to pin THIS module's half of
    the contract — that it neither drops the list nor lets it reach ``valid`` —
    independently of which findings the current registry happens to produce.
    """
    real_status = RouterService.status
    advisory = ["model 'made-up-elo-9000': not in the capability registry"]

    def stubbed(self):
        reported = dict(real_status(self))
        reported["warnings"] = list(advisory)
        return reported

    monkeypatch.setattr(RouterService, "status", stubbed)
    status = asyncio.run(plugin_api.api_status())
    assert status["warnings"] == advisory
    assert status["validation_errors"] == []
    assert status["valid"] is True


def test_plugin_status_reports_a_broken_policy_instead_of_raising(plugin_config):
    """Read paths never raise: an invalid policy is a diagnostic, not a 500."""
    plugin_config({"enabled": True, "rules": []})  # no default, no tiers
    status = asyncio.run(plugin_api.api_status())
    assert status["valid"] is False
    assert status["validation_errors"], "the operator must be told what is wrong"
    assert status["warnings"] == []


def test_plugin_status_survives_a_corrupt_config(plugin_config, tmp_path):
    """Unparseable YAML degrades to empty defaults plus an error string."""
    (tmp_path / "router.yaml").write_text("enabled: [unclosed\n", encoding="utf-8")
    status = asyncio.run(plugin_api.api_status())
    assert status["valid"] is False
    assert status["banned_models"] == [] and status["tiers"] == []


def test_plugin_policy_exposes_the_per_tier_knobs(plugin_config):
    """Every tier field reaches the console, including the time layer.

    The tier mapping is copied whole precisely so the next knob added does not
    need this endpoint edited — and so the console can render the ones added
    this phase instead of an empty panel.
    """
    policy = asyncio.run(plugin_api.api_rules())
    tier = policy["tiers"]["T1"]
    for knob in ("fallback_strategy", "pin_primary", "billing_mode",
                 "requirements", "time_policy", "time_cap"):
        assert knob in tier, f"policy must expose tier knob '{knob}'"
    assert tier["time_cap"] == {"max_multiplier": 1.5}
    assert tier["time_policy"]["avoid_peak"] == ["zai", "deepseek"]
    assert policy == RouterService(plugin_api._CONFIG_PATH).policy()
    # Historical keys the bundled UI reads.
    assert policy["rules"] == [] and policy["default"] == {"model": "T1"}


def test_plugin_explain_returns_and_records_the_chain_plan(plugin_config, monkeypatch):
    """A recorded decision carries chain_plan, and the response lifts it.

    Without the recorded plan, replaying a decision cannot show which elos were
    eligible, which were rejected and why, or in what order they would be tried.
    """
    log = plugin_api.DecisionLog()
    monkeypatch.setattr(plugin_api, "_log", log)  # never leak into other tests
    result = asyncio.run(plugin_api.api_explain(task="refactor the parser module"))

    assert isinstance(result["chain_plan"], dict)
    for key in ("chain", "requirements", "rejected", "strategy"):
        assert key in result["chain_plan"]
    entry = log.tail(1)[0]
    assert "chain_plan" in entry, "a recorded decision must carry chain_plan"
    assert entry["chain_plan"]["chain"] == result["chain_plan"]["chain"]


def test_plugin_explain_preview_is_stable_across_polls(plugin_config, monkeypatch):
    """The dashboard polls /explain, so the previewed chain must not churn.

    Stability is promised WITHIN the hour, not across it: the clock is pinned to a
    fixed minute here only to prove that two polls seconds apart cannot differ,
    which is the property the fixed preview seed plus the hour truncation buy.
    """
    monkeypatch.setattr(
        plugin_api, "_utc_now",
        lambda: datetime(2026, 8, 17, 7, 44, 12, tzinfo=timezone.utc),
    )
    first = asyncio.run(plugin_api.api_explain(task="write a unit test"))
    second = asyncio.run(plugin_api.api_explain(task="write a unit test"))
    assert first == second


def test_plugin_explain_still_answers_on_an_invalid_policy(plugin_config):
    """Unlike the write-gated service, this preview must not refuse.

    A broken config is exactly when an operator needs to see where a task lands;
    /status already reports the errors, so refusing here only hides information.
    """
    plugin_config({"enabled": True, "rules": []})
    result = asyncio.run(plugin_api.api_explain(task="anything"))
    assert "chain_plan" in result and "output" in result


def test_plugin_lint_keeps_warnings_out_of_valid(plugin_config):
    result = asyncio.run(plugin_api.api_lint())
    assert result["valid"] is True and result["errors"] == []
    assert isinstance(result["warnings"], list)
    assert result["warnings"] == (
        RouterService(plugin_api._CONFIG_PATH).status().get("warnings", [])
    )


# ── The clock: /explain must ask production's question ───────────────


def test_plugin_explain_fires_a_time_keyed_rule(plugin_config):
    """utc_hour reaches the matcher, so an hour-keyed row is live here.

    This endpoint used to build the feature vector from signals.extract() alone.
    utc_hour is INJECTED, never extracted, so the clause could not be satisfied
    and this row was permanently inert on the dashboard while firing in
    production — the operator's preview answered a question production never asks.
    """
    plugin_config(_TIME_KEYED)

    inside = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_PEAK))
    assert inside["matched_rule_id"] == "defer-heavy-work-off-peak"
    assert inside["matched_clauses"]["utc_hour"] == {"gte": 6, "lt": 10}
    assert inside["output"]["model"] == "mimo-v2.5", "T3 is where the peak defers to"
    assert inside["evaluated_at"]["utc_hour"] == 7
    assert inside["evaluated_at"]["utc_weekday"] == 0, "Monday: the zai peak is gated"
    assert inside["evaluated_at"]["at_source"] == "explicit"

    outside = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_OFF_PEAK))
    assert outside["matched_rule_id"] is None, "off peak the row must NOT fire"
    assert outside["output"]["model"] == "glm-4.7"
    assert outside["evaluated_at"]["utc_hour"] == 15


def test_plugin_explain_agrees_with_the_service_at_the_same_instant(plugin_config):
    """The two operator surfaces must render ONE plan for one task and instant.

    Agreement is the actual requirement — not any particular chain — because the
    plan RouterService composes is the one production attempts. When the plugin
    omitted the clock the two diverged on exactly the material an operator uses to
    decide: the chain order, the price multipliers in force, and which rails a
    time_cap refused.
    """
    plugin_config(_TIME_KEYED)

    plugin = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    service = _service_explain(_TRIVIAL_TASK, _PEAK)
    decision = service["decision"]

    for key in ("matched_rule_id", "output", "cause", "matched_clauses"):
        assert plugin[key] == decision[key], f"the two surfaces disagree on {key}"

    plugin_plan, service_plan = _plan_of(plugin), _plan_of(service)
    for key in ("chain", "multipliers", "capped", "strategy", "time_agnostic"):
        assert plugin_plan.get(key) == service_plan.get(key), (
            f"chain_plan.{key} differs between the dashboard and the service"
        )
    assert plugin_plan == service_plan
    assert plugin["evaluated_at"] == service["evaluated_at"]

    # Two blank plans also "agree", so pin that this instant produced REAL time
    # material — otherwise the assertions above would still pass with the clock
    # dropped on both surfaces. At Monday 07:00 UTC zai bills glm-4.7 at 2.0x,
    # over T1's declared ceiling of 1.5, so the primary is refused and named.
    assert plugin_plan["time_agnostic"] is False, "the clock must reach the planner"
    assert plugin_plan["multipliers"], "the multipliers in force must be reported"
    capped = {entry["model"]: entry["multiplier"] for entry in plugin_plan["capped"]}
    assert "glm-4.7" in capped, "the over-priced primary must be reported as capped"
    assert capped["glm-4.7"] > _TIME_KEYED["tiers"]["T1"]["time_cap"]["max_multiplier"]
    assert "glm-4.7" not in [hop.get("model") for hop in plugin_plan["chain"]]
    assert plugin_plan["chain"], "a cap may never empty the chain"


def test_plugin_explain_defaults_to_the_current_utc_hour(plugin_config, monkeypatch):
    """With no ``at`` the plan is evaluated at NOW, truncated to the hour.

    A time-agnostic default would put the endpoint back where it started: every
    multiplier 1.0 and every hour-keyed row inert. The truncation is what makes
    two polls in the same hour byte-identical while the next hour may differ.
    """
    plugin_config(_TIME_KEYED)
    monkeypatch.setattr(
        plugin_api, "_utc_now",
        lambda: datetime(2026, 8, 17, 7, 44, 12, tzinfo=timezone.utc),
    )
    result = asyncio.run(plugin_api.api_explain(task=_HARD_TASK))
    assert result["evaluated_at"]["at_source"] == "now"
    assert result["evaluated_at"]["at"] == "2026-08-17T07:00:00+00:00"
    assert result["matched_rule_id"] == "defer-heavy-work-off-peak"


def test_plugin_explain_refuses_an_unusable_at(plugin_config):
    """An unparseable clock is the CALLER's error: a 400, never a wrong hour.

    Falling back to "now" would answer a different question than the one asked
    and record it as though it were the answer — on an audit surface that is worse
    than refusing.
    """
    with pytest.raises(HTTPException) as raised:
        asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at="tuesday-ish"))
    assert raised.value.status_code == 400
    assert "ISO-8601" in str(raised.value.detail)


def test_plugin_explain_treats_a_blank_at_as_now(plugin_config):
    """An empty query string is an absent parameter, not a bad one.

    A form that submits ``at=`` must not 400 the panel that renders the plan.
    """
    plugin_config(_TIME_KEYED)
    result = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at="  "))
    assert result["evaluated_at"]["at_source"] == "now"
