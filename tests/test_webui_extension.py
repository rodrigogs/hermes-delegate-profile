"""Static contract tests for the Hermes One extension assets."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "webui_extension" / "capability-router"


def test_extension_manifest_declares_token_v1_sidecar():
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "capability-router"
    assert manifest["scripts"] == ["router-nav.js"]
    assert manifest["stylesheets"] == ["router-nav.css"]
    assert manifest["sidecar"] == {
        "type": "loopback",
        "origin": "http://127.0.0.1:8791",
        "health_path": "/health",
        "proxy_auth": "token-v1",
    }


def test_extension_script_is_safe_accessible_and_syntax_valid():
    script_path = EXTENSION / "router-nav.js"
    script = script_path.read_text(encoding="utf-8")
    for block in ("THESIS:", "OWN-WORLD:", "STORY:", "FIRST VIEWPORT:", "FORM:"):
        assert block in script
    for endpoint in ("/status", "/policy", "/blocklist", "/explain"):
        assert endpoint in script
    for accessibility_hook in ("aria-label", "aria-live", "setAttribute('role', 'alert')"):
        assert accessibility_hook in script
    for destructive_pattern in (
        "document.body.innerHTML",
        "document.querySelector('main').innerHTML",
        "document.querySelector(\"main\").innerHTML",
        "eval(",
        "new Function",
    ):
        assert destructive_pattern not in script
    assert "textContent" in script
    assert "MutationObserver" in script
    assert "observer.disconnect" in script
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
    # The Pipeline/replay wiring must be present.
    for token in ("renderPipeline", "/routes", "svgEl", "createElementNS", "renderReplayStep"):
        assert token in script, f"console.html must wire {token}"
    # Syntax must be valid (write the extracted body to a temp file for node --check).
    script_file = tmp_path / "console_inline.js"
    script_file.write_text(script, encoding="utf-8")
    checked = subprocess.run(
        ["node", "--check", str(script_file)],
        text=True, capture_output=True, check=False,
    )
    assert checked.returncode == 0, checked.stderr


def test_console_html_declares_pipeline_tab_and_svg_canvas():
    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    assert 'data-tab="pipeline"' in html
    assert 'id="panel-pipeline"' in html
    assert 'id="pipelineSvg"' in html
    assert 'id="routesTable"' in html


def test_extension_css_inherits_host_tokens_and_handles_mobile():
    css = (EXTENSION / "router-nav.css").read_text(encoding="utf-8")
    for token in ("var(--bg)", "var(--surface)", "var(--text)", "var(--muted)", "var(--border)", "var(--accent)"):
        assert token in css
    assert ".capability-router-panel[hidden]" in css
    assert "@media" in css
    assert ":focus-visible" in css
