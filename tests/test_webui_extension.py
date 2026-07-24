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


def test_extension_css_inherits_host_tokens_and_handles_mobile():
    css = (EXTENSION / "router-nav.css").read_text(encoding="utf-8")
    for token in ("var(--bg)", "var(--surface)", "var(--text)", "var(--muted)", "var(--border)", "var(--accent)"):
        assert token in css
    assert ".capability-router-panel[hidden]" in css
    assert "@media" in css
    assert ":focus-visible" in css
