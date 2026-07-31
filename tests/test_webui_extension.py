"""Static contract tests for the Hermes One extension assets."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "webui_extension" / "hermes-one-capability-router"


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
        ["node", "--check", str(script_path)],
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
        ["node", "--check", str(script_file)],
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
