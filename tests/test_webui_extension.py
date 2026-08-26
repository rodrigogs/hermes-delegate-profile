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
import importlib
import inspect
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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


def test_wall_clock_is_read_in_exactly_one_place():
    """DESIGN.md §7: nowUtc() is the ONE wall-clock reader.

    Every time-dependent value takes the instant as a parameter — ``ago``
    receives it from the caller — so a second reader (a formatter that called
    ``Date.now()``, or a bare ``new Date()`` anywhere else) would make every
    rendering test pass at 05:00 UTC and fail at 07:00. ``new Date(x)`` with an
    argument is a timestamp CONVERSION, not a read, and stays legal.
    """
    import re

    script = _console_inline_script()
    assert "Date.now()" not in script, (
        "Date.now() must not appear anywhere in the console script; the only "
        "wall-clock read is the bare new Date() inside nowUtc()"
    )
    # A `new Date` not followed by an argument list is a wall-clock read too
    # (e.g. `(new Date).getTime()`); only the constructor-with-argument form
    # converts a timestamp.
    assert not re.search(r"new Date\s*(?!\()", script), (
        "a paren-less new Date is a wall-clock read; convert with new Date(x)"
    )
    bare = [m.start() for m in re.finditer(r"new Date\s*\(\s*\)", script)]
    assert len(bare) == 1, (
        "the bare new Date() (no args) must appear exactly once, inside "
        f"nowUtc() — found {len(bare)}"
    )
    definition = script.index("const nowUtc = ")
    assert definition < bare[0] < definition + 200, (
        "the one bare new Date() must live inside the nowUtc definition"
    )


def test_console_html_declares_its_three_screens_and_their_surfaces():
    """The console is three screens; each must exist with the hooks its screen
    needs. Both Pipeline and Routes read as ordered vertical sequences — a policy
    is a first-match table and a trace is a short path, so neither is a free-form
    canvas and the operator learns one way of reading this console."""


def test_console_html_declares_its_two_screens_and_their_surfaces():
    """The console is TWO screens, split by what the operator is doing; each must
    exist with the hooks its screen needs. Both read as ordered vertical sequences —
    a policy is a first-match table and a trace is a short path, so neither is a
    free-form canvas and the operator learns one way of reading this console.

    It was three screens named by NOUN (Tarefas / Modelos / Decisões), and no noun
    says where a setting lives: the rule list, the file editor and the compaction
    action were under the first while the presets and the group chains were under the
    second, which is the whole of the operator's "I don't know where I can edit the
    settings". Configuração holds everything writable; Operação holds the runtime.
    """
    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    for tab in ("pipeline", "routes"):
        assert f'data-tab="{tab}"' in html
        assert f'id="panel-{tab}"' in html
    assert 'data-tab="health"' not in html, "the third destination is gone, not hidden"
    assert 'id="panel-health"' not in html
    assert 'id="sheet"' in html, "the Pipeline screen is the ordered decision sheet"
    assert 'id="probeTask"' in html, "an operator must be able to try a task"
    assert 'id="ladder"' in html, "the capability ladder shows where tasks can land"
    assert 'id="failSafeBox"' in html, "§1.2 item 5: the last resort has its own block on the Modelos tab"
    assert 'id="routesTable"' in html
    assert 'id="replayPath"' in html, "replay lists the steps a real decision took"
    assert "<svg" not in html, "no canvas survives: both screens are read as lists"


def test_console_tabs_are_named_by_what_you_do_there():
    """The two tabs are Configuração / Operação, and the names are the point.

    CA1 of the earlier spec asked for three tabs named Tarefas / Modelos / Decisões.
    That criterion is OVERTURNED here, out loud, on the operator's own verdict: three
    nouns partition the screen by which object you are looking at, and the thing an
    operator arrives wanting is a place to CHANGE something. The rule list, the file
    editor and the compaction action were under Tarefas; the presets and the group
    chains were under Modelos; the classifier and the groups had no edit path at all.
    Configuração now holds everything writable, so "where do I edit the settings" has
    exactly one answer, and Operação holds the runtime the policy produced.

    The ids stay ``tab-pipeline`` / ``tab-routes`` because router-nav.js and the JS
    suite match by id: a re-partition must not also be a rename.
    """
    import re

    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    nav = re.search(r'<nav class="tabs".*?</nav>', html, re.S)
    assert nav, "the console declares its tab list"

    order = re.findall(r'id="(tab-\w+)".*?<span class="tab-name">([^<]+)</span>', nav.group(0), re.S)
    assert [tab for tab, _ in order] == ["tab-pipeline", "tab-routes"], (
        "Configuração leads; ids keep their historic names"
    )
    assert [label for _, label in order] == ["Configuração", "Operação"]

    # Born selected: the markup itself carries the state, not a script pass.
    first = re.search(r'<button class="tab" id="tab-pipeline"[^>]*>', nav.group(0))
    assert first and 'aria-selected="true"' in first.group(0), (
        "Configuração is the tab an operator lands on"
    )
    line = re.search(r'<button class="tab" id="tab-routes"[^>]*>', nav.group(0))
    assert line and 'aria-selected="false"' in line.group(0)

    panel = re.search(r'<section class="screen active" id="panel-pipeline"', html)
    assert panel, "the Configuração panel is born active"
    assert re.search(r'<section class="screen" id="panel-routes"', html), "Operação starts inactive"

    # The script state agrees with the markup it is born into.
    assert re.search(r"tab: 'pipeline',", html), "state.tab starts on Configuração"

    # And the sidebar says the same two words in the same order. The source escapes
    # non-ASCII, so compare the escaped spellings it actually ships.
    nav_js = (EXTENSION / "router-nav.js").read_text(encoding="utf-8")
    sections = re.search(r"for \(const \[tab, label\] of \[\[(.*?)\]\]\)", nav_js, re.S)
    assert sections, "the sidebar declares its section list"
    pairs = re.findall(r"\['(\w+)', '([^']+)'\]", sections.group(0))
    assert pairs == [
        ("pipeline", "Configura\\u00e7\\u00e3o"), ("routes", "Opera\\u00e7\\u00e3o"),
    ], "the sidebar mirrors the tabs: same words, same order"


def test_console_transcribes_the_classifier_tier_anchors():
    """The four group names on screen are the classifier's rubric, not a copy of it.

    `T1`..`T4` mean nothing to someone opening this console, and that was the
    operator's complaint: the screen named groups it never explained. The authority
    already exists in the engine — ``classify.TIER_ANCHORS`` is the text the
    classifier scores against — so the console transcribes it instead of minting a
    second vocabulary that can drift from the one that routes.

    Asserted against the module rather than against a copy of the phrases, so adding
    a tier to the rubric, or rewording one, fails HERE until the screen follows. A
    key outside the rubric is legal in ``tiers`` and the classifier can never pick
    it, so the console needs a sentence for that case too.
    """
    import re
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from router import classify

    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    block = re.search(r"const TIER_MEANING = \{(.*?)\n\s*\};", html, re.S)
    assert block, "the console must declare TIER_MEANING to explain the groups"

    entries = dict(re.findall(r"(\w+): \{ label: '([^']+)'", block.group(1)))
    assert set(entries) == set(classify.TIER_ANCHORS), (
        "the screen's groups and the classifier's rubric must be the same set: "
        f"screen={sorted(entries)} rubric={sorted(classify.TIER_ANCHORS)}"
    )
    for key in classify.TIER_ANCHORS:
        assert entries[key], f"{key} needs a short label"
        what = re.search(rf"{key}: \{{ label: '[^']+', what: '([^']+)'", block.group(1))
        assert what and len(what.group(1)) > 30, (
            f"{key} needs the rubric's own description, not just a label"
        )

    assert "TIER_UNKNOWN_WHAT" in html, (
        "a tier key outside the rubric is legal, so the screen owes it a sentence"
    )


import re as _re

# The second argument of ``el(tag, cls, text)`` is a class name, and so is every
# argument of the classList calls. Matched on what PRECEDES the literal, because the
# only reliable difference between `tier-fact rails` and a sentence is where it sits.
CLASS_POSITION = _re.compile(
    r"""(?:
          el\(\s*['"`][a-z0-9]+['"`]\s*,\s*
        | classList\.(?:add|remove|toggle)\(\s*
        | className\s*(?:=|\+=)\s*
        | dataset\.[A-Za-z_$][\w$]*\s*=\s*
        )$""",
    _re.X,
)


def _console_rendered_text() -> list[tuple[str, int, str]]:
    """Every string this console can put on screen, as ``(where, line, text)``.

    Four things are deliberately NOT rendered text, and counting them would force
    changes the spec forbids elsewhere:

    * HTML comments and the ``<style>`` block — this file keeps its design rationale
      in comments, two of which discuss the word ``rail`` on purpose.
    * ``${...}`` bodies inside template literals, which are code. Without this,
      ``reach${stale ? ' is-stale' : ''}`` reads as prose carrying ``stale`` when all
      it carries is a CSS class and a variable.
    * Single-token literals: identifiers, endpoint paths and the lint error codes the
      server sends. ``eloRow`` may not be renamed (spec §5.5 freezes the exported
      surface), the id ``#staleBanner`` is required by §4.2 and ``call('/blocklist')``
      is a route.
    * Anything in a class-name position. ``el(tag, cls, text)`` puts the class second
      and the text third, so the second argument is excluded by POSITION rather than
      by how it looks: ``el('div', `tier-fact rails${…}`)`` carries a space and would
      otherwise read as a sentence, and ``.tier-fact.rails`` is a real rule in the
      ``<style>`` — a class name is not something a reader is shown.
    * Attribute values that are not shown to anyone (``data-*``, ``class``, ``id``).
      ``placeholder``, ``title`` and ``aria-label`` ARE shown, so they are included.
    """
    import re

    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    script = re.search(r"<script>\n?(.*?)\n?\s*</script>", html, re.S)
    assert script, "the console must carry exactly one inline <script>"

    def blank(mo):
        return "\n" * mo.group(0).count("\n")

    markup = html[: script.start()] + html[script.end():]
    markup = re.sub(r"<style>.*?</style>", blank, markup, flags=re.S)
    markup = re.sub(r"<!--.*?-->", blank, markup, flags=re.S)

    out: list[tuple[str, int, str]] = []
    for attr in ("placeholder", "title", "aria-label"):
        for mo in re.finditer(attr + r'="([^"]+)"', markup):
            out.append(("markup", markup[: mo.start()].count("\n") + 1, mo.group(1)))
    for number, line in enumerate(markup.split("\n"), 1):
        stripped = re.sub(r"<[^>]*>", " ", line).strip()
        if stripped:
            out.append(("markup", number, stripped))

    # Hand-rolled because a regex cannot tell a quote inside a comment from a real
    # one, and this file has apostrophes in prose and quotes inside regex literals.
    body = script.group(1)
    start_line = html[: script.start(1)].count("\n") + 1
    index, size, line_number = 0, len(body), start_line
    while index < size:
        char = body[index]
        if char == "\n":
            line_number += 1
            index += 1
        elif char == "/" and body[index + 1:index + 2] == "/":
            while index < size and body[index] != "\n":
                index += 1
        elif char == "/" and body[index + 1:index + 2] == "*":
            index += 2
            while index + 1 < size and body[index:index + 2] != "*/":
                line_number += body[index] == "\n"
                index += 1
            index += 2
        elif char in "'\"`":
            quote, opened, buffer = char, line_number, []
            before = body[max(0, index - 48):index]
            index += 1
            while index < size:
                if body[index] == "\\":
                    buffer.append(body[index:index + 2])
                    index += 2
                    continue
                if body[index] == quote:
                    index += 1
                    break
                line_number += body[index] == "\n"
                buffer.append(body[index])
                index += 1
            if not CLASS_POSITION.search(before):
                out.append(("script", opened, "".join(buffer)))
        else:
            index += 1

    def without_interpolations(text: str) -> str:
        kept, depth, cursor = [], 0, 0
        while cursor < len(text):
            if text.startswith("${", cursor):
                depth += 1
                cursor += 2
                continue
            if depth and text[cursor] == "{":
                depth += 1
            elif depth and text[cursor] == "}":
                depth -= 1
                cursor += 1
                continue
            if not depth:
                kept.append(text[cursor])
            cursor += 1
        return "".join(kept)

    prose = []
    for where, line, text in out:
        clean = without_interpolations(text).strip() if where == "script" else text.strip()
        if " " in clean:  # a single token is a key, a class or a path — not prose
            prose.append((where, line, clean))
    return prose


def _console_ui_strings() -> list[tuple[str, int, str]]:
    """Every string this console can put on screen, single tokens included.

    ``_console_rendered_text`` deliberately drops single-token literals (keys,
    classes, paths). This guard needs them: ``el('button', 'btn go', 'Apply')``
    is a single token and exactly the label the §4.7 rule forbids. Comments and
    the ``<style>`` block are skipped for the same reasons the shared extractor
    skips them — this file keeps its rationale in comments, and a comment is
    not something the reader is shown.
    """

    import re

    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    script = re.search(r"<script>\n?(.*?)\n?\s*</script>", html, re.S)
    assert script, "the console must carry exactly one inline <script>"

    def blank(mo):
        return "\n" * mo.group(0).count("\n")

    markup = html[: script.start()] + html[script.end():]
    markup = re.sub(r"<style>.*?</style>", blank, markup, flags=re.S)
    markup = re.sub(r"<!--.*?-->", blank, markup, flags=re.S)

    out: list[tuple[str, int, str]] = []
    for attr in ("placeholder", "title", "aria-label"):
        for mo in re.finditer(attr + r'="([^"]+)"', markup):
            out.append(("markup", markup[: mo.start()].count("\n") + 1, mo.group(1)))
    for number, line in enumerate(markup.split("\n"), 1):
        stripped = re.sub(r"<[^>]*>", " ", line).strip()
        if stripped:
            out.append(("markup", number, stripped))

    body = script.group(1)
    start_line = html[: script.start(1)].count("\n") + 1
    index, size, line_number = 0, len(body), start_line
    while index < size:
        char = body[index]
        if char == "\n":
            line_number += 1
            index += 1
        elif char == "/" and body[index + 1:index + 2] == "/":
            while index < size and body[index] != "\n":
                index += 1
        elif char == "/" and body[index + 1:index + 2] == "*":
            index += 2
            while index + 1 < size and body[index:index + 2] != "*/":
                line_number += body[index] == "\n"
                index += 1
            index += 2
        elif char in "'\"`":
            quote, opened, buffer = char, line_number, []
            index += 1
            while index < size:
                if body[index] == "\\":
                    buffer.append(body[index:index + 2])
                    index += 2
                    continue
                if body[index] == quote:
                    index += 1
                    break
                line_number += body[index] == "\n"
                buffer.append(body[index])
                index += 1
            out.append(("script", opened, "".join(buffer)))
        else:
            index += 1
    return out


def test_console_vocabulario():
    """CA7: the words the screen says are the glossary's, and nobody else's.

    ``elo``, ``hop``, ``shadowed`` and ``stale`` are not in DESIGN.md's domain list
    (rule 6), so they are invented vocabulary that the rule itself forbids; ``rail``
    IS in the list, as a synonym of ``provider``, and the screen picks one of the two
    — **provedor**. Matching is on whole words: a literal ``grep -oi elo`` also hits
    ``modelo`` and ``pelo``, which are required Portuguese, so the criterion can only
    mean the word standing on its own.

    The five domain words that ARE allowed appear only glossed, in parentheses right
    after the Portuguese term, exactly as §4.6 writes them — the screen may say
    "Grupo de modelos (tier)" and may not say "tier" alone.
    """
    import re

    prose = _console_rendered_text()
    assert len(prose) > 200, "the extractor stopped seeing the screen's own sentences"

    for word in ("elo", "hop", "rail", "shadowed", "stale"):
        # The plural is the same invented word: the file said "these elos" and "3
        # hops" before this criterion, and a guard blind to the -s would have let
        # both back in. Verified by mutation: without the `s?` the plural passes.
        pattern = re.compile(r"(?<![\w-])" + word + r"s?(?![\w-])", re.I)
        found = [(w, ln, t) for w, ln, t in prose if pattern.search(t)]
        assert not found, (
            f"'{word}' is not this screen's vocabulary (§4.6), and it reaches the "
            f"reader in {len(found)} place(s): "
            + "; ".join(f"{w}:{ln} {t[:70]!r}" for w, ln, t in found[:6])
        )

    for word in ("tier", "breaker", "fail-safe", "blocklist", "profile"):
        pattern = re.compile(r"(?<![\w-])" + re.escape(word) + r"(?![-\w])", re.I)
        glossed = re.compile(r"\(\s*" + re.escape(word) + r"\s*\)", re.I)
        bare = [
            (w, ln, t) for w, ln, t in prose
            if pattern.search(t) and not glossed.search(t)
        ]
        assert not bare, (
            f"'{word}' is a domain word §4.6 allows only as a gloss in parentheses "
            f"after the Portuguese term, and it stands alone in {len(bare)} place(s): "
            + "; ".join(f"{w}:{ln} {t[:70]!r}" for w, ln, t in bare[:6])
        )


def test_console_write_surface_speaks_one_pt_br_vocabulary():
    """§4.7/§6.10: the write surface has no English labels or messages.

    The same write used to carry two vocabularies on one screen: the editor and
    the presets said the §4.7 literals while the inspector said Apply/Preview/
    Revert with Rejected/Invalid/Checking…/Written. messages. Every one of the
    seven English forms is pinned here as a label or a UI message, so the second
    vocabulary cannot come back as a rename or a copy.

    Matching is whole-word and case-sensitive: ``/apply`` is a route and
    ``doPreview`` a symbol — neither is rendered — while the words as the spec's
    table spells them (capitalised) are what the operator would read. The plural
    check covers every surface: ``el()`` labels, ``setMsg`` messages, markup
    text and placeholders alike, single tokens included.
    """

    import re

    strings = _console_ui_strings()
    assert len(strings) > 100, "the extractor stopped seeing the console's strings"
    for word in ("Apply", "Preview", "Revert", "Rejected", "Invalid", "Checking", "Written"):
        pattern = re.compile(r"(?<![\w-])" + re.escape(word) + r"(?![-\w])")
        found = [(w, ln, t) for w, ln, t in strings if pattern.search(t)]
        assert not found, (
            f"'{word}' is not this screen's write vocabulary (§4.7/§6.10), and it "
            f"reaches the reader in {len(found)} place(s): "
            + "; ".join(f"{w}:{ln} {t[:70]!r}" for w, ln, t in found[:6])
        )


def test_console_english_terms_of_this_card_stay_out():
    """§4.1/§3.2/§3.3/§6.10: the eight English forms this card removed never
    come back as rendered text.

    Each term is matched the way an operator would read it, and the two
    extractors already say what is NOT rendered: comments and the ``<style>``
    block are stripped, and identifiers are never string literals.

    * Single-token labels (``Refresh``, ``Refreshing``, ``Edit``, ``Done``,
      ``banned``, ``left``, ``Routing``) are matched whole-word and
      case-sensitive over ``_console_ui_strings()`` — the extractor that keeps
      single tokens. ``_console_rendered_text`` drops them, and a bare
      ``'banned'`` returning as ``el('span', 'state', 'banned')`` is exactly
      the regression to catch. Whole-word, case-sensitive matching is what
      keeps ``editMode``/``refresh`` (identifiers), ``Editar`` (a different
      word) and ``ArrowLeft`` (capital L) out of the result.
    * ``Stop editing`` is a phrase, matched as such.
    * ``on``/``off`` count only as EXACT standalone script literals — the
      shape the routing fact's value had. The same letters legitimately build
      CSS classes (``' on'``/``' off'`` fragments) and the ``autocomplete``
      attribute, so a substring or whole-word sweep would ban innocent uses;
      the fact value is an exact string, and that is the position pinned.
    * ``ago`` is whole-word: the pt-BR form this replaced ("há 5m") carries
      no English word to trip on, and "5m ago" has the word standing alone.

    The one location this test cannot see is the `` of `` inside the
    cap-bypass sentence: it sits in a nested template inside a template
    interpolation, which both extractors stop at — that preposition is pinned
    by the render test in test_console_logic.js (the "de 1,5×" sentence).
    """

    import re

    strings = _console_ui_strings()
    assert len(strings) > 100, "the extractor stopped seeing the console's strings"
    for word in ("Refresh", "Refreshing", "Edit", "Done", "banned", "left", "Routing", "ago"):
        pattern = re.compile(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])")
        found = [(w, ln, t) for w, ln, t in strings if pattern.search(t)]
        assert not found, (
            f"'{word}' is English an operator would read (§4.1/§3.2/§6.10), and it "
            f"reaches the reader in {len(found)} place(s): "
            + "; ".join(f"{w}:{ln} {t[:70]!r}" for w, ln, t in found[:6])
        )
    phrase = re.compile(r"(?<![\w-])Stop editing(?![\w-])")
    found = [(w, ln, t) for w, ln, t in strings if phrase.search(t)]
    assert not found, (
        "'Stop editing' is English an operator would read (§4.1), and it reaches "
        f"the reader in {len(found)} place(s): "
        + "; ".join(f"{w}:{ln} {t[:70]!r}" for w, ln, t in found[:6])
    )
    exact = [(w, ln, t) for w, ln, t in strings if w == "script" and t in ("on", "off")]
    assert not exact, (
        "the routing fact's value must be 'ligado'/'desligado', not English: "
        + "; ".join(f"{ln} {t!r}" for _, ln, t in exact)
    )


def test_console_lint_error_sentence_lives_once_in_the_map():
    """§4.7: the lint-error sentence exists once — in the WRITE map.

    The literal scan (comment #92) found it twice: the map's template (fill()
    at the preset banner) and a same-head inline copy in renderWarnings that
    spelled its own plural. The count is on the sentence's HEAD, up to the
    interpolation — a re-spelled copy ("2 erros no arquivo" instead of
    "2 erro(s) no arquivo") would still trip it, which is exactly how the
    second copy was born. The extractor strips comments and the ``<style>``
    block, so a mention in prose does not count.
    """

    strings = _console_ui_strings()
    head = "Não é possível salvar enquanto houver erro"
    found = [(w, ln, t) for w, ln, t in strings if head in t]
    assert len(found) == 1, (
        f"the lint sentence's head must exist once (the WRITE map), found {len(found)}: "
        + "; ".join(f"{w}:{ln} {t[:70]!r}" for w, ln, t in found[:6])
    )


def test_console_text_editor_warning_is_present_once_from_the_map():
    """§4.7: the whole-file editor's warning is present, once, from the map.

    The literal scan (comment #92) found the sentence missing from the
    ``<details>`` entirely. It quotes "Ver o que muda" and "Salvar", so it
    cannot re-spell them: the sentence lives in the WRITE map and boot stamps
    the empty ``#jsonNote`` paragraph, exactly like the three write buttons.
    The script therefore carries exactly one copy of the sentence (the map),
    and the markup carries none outside it.
    """

    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    # "Editar O ARQUIVO como texto": the twisty needs the noun now that every ROW has
    # its own Editar — without it, two controls a screen apart read as the same offer.
    details = html.split("<summary>Editar o arquivo como texto</summary>", 1)[1].split("</details>", 1)[0]
    assert 'id="jsonNote"' in details, "the warning's paragraph lives inside the <details>"
    strings = _console_ui_strings()
    sent = [(w, ln, t) for w, ln, t in strings if "Aqui você edita o arquivo de política inteiro" in t]
    assert len(sent) == 1, (
        "the whole-file warning must exist once, in the WRITE map — found "
        + f"{len(sent)}: " + "; ".join(f"{w}:{ln} {t[:70]!r}" for w, ln, t in sent[:6])
    )
    script = _console_inline_script()
    assert "WRITE.textEdit" in script, "boot must stamp the warning from the map"
    assert "['refresh', WRITE.refresh]" in script, "the refresh button is stamped from the map too"


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


def test_embedded_console_hides_its_own_title_and_tabs():
    """Inside the Hermes One panel the shell already names the surface twice —
    the rail label and the sidebar's panel head — so the console's own masthead
    and tab row must not be drawn a second time. The review counted "Capability
    Router" three times on one screen (router-01-abertura.png). The
    .is-embedded class is set by the init path (window.self !== window.top),
    and the standalone page at /capability-router still draws both."""
    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    assert ".is-embedded nav.tabs { display: none; }" in html, "tabs stay in the DOM but are not drawn twice"
    assert ".is-embedded .view-title { display: none; }" in html, "the shell names the surface; the title must not repeat it"


def test_health_badge_counts_exceptions_and_wears_amber():
    """The Health badge counts bans + breaker cooldowns (zero hides it) and
    wears the amber attention colour. Counting elos made the badge FALL from
    8 to 1 the moment a problem appeared; the elo total lives in the lede."""
    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    assert ".count.is-warn { color: var(--warn-text); }" in html
    assert "bans.length + breakers.length, true" in html
    assert "|| models.length" not in html, "the badge must not fall back to the elo inventory"


def test_health_facts_are_only_the_two_that_exist_nowhere_else():
    """The summary keeps ROUTING and CLASSIFIER — the rules count repeated the
    sheet's numbered list and the invalid count repeated the lint banner."""
    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    # Both labels come from the WRITE map now: `classifier` was the last
    # English label the screen said, and §4.7 keeps a phrase in one place.
    assert "fact(WRITE.routing" in html and "fact(WRITE.classifier" in html
    assert "fact('rules'" not in html and "fact('invalid'" not in html


# ── Dashboard plugin API ↔ RouterService shape parity ────────────────
#
# fastapi is a Hermes-dashboard dependency, not one of this plugin's (pyyaml
# only), so these skip cleanly where the dashboard is not installed rather than
# forcing a new dependency on the router package.

pytest.importorskip("fastapi")

from fastapi import FastAPI, HTTPException  # noqa: E402

from dashboard import plugin_api  # noqa: E402
from router.decision_log import DecisionLog, empty_chain_plan  # noqa: E402
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


def _service_explain(task, at, prompt_text=""):
    """``RouterService.explain`` for the same task, instant and prompt.

    Each parameter is forwarded only when the installed service declares it, so
    this parity assertion still MEANS something against a service predating the
    injected clock or the composed prompt instead of dying on an unexpected
    keyword — the plugin and the service are then compared as the two equally
    time-agnostic (or equally goal-sized) surfaces they both are.
    """
    service = RouterService(plugin_api._CONFIG_PATH)
    declared = inspect.signature(service.explain).parameters
    kwargs = {}
    if "at" in declared:
        kwargs["at"] = at
    if "prompt_text" in declared:
        kwargs["prompt_text"] = prompt_text
    return service.explain(task, **kwargs)


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
    # over T1's declared ceiling of 1.5.
    ceiling = _TIME_KEYED["tiers"]["T1"]["time_cap"]["max_multiplier"]
    assert plugin_plan["time_agnostic"] is False, "the clock must reach the planner"
    assert plugin_plan["multipliers"]["glm-4.7"] > ceiling, (
        "the multipliers in force at this hour must be reported"
    )
    # T1's ceiling is a DOLLAR ceiling and glm-4.7 is plan-billed, so the cap
    # cannot evict it (see capabilities.apply_time_cap: a credit multiplier adds no
    # dollars, and paying metered money to dodge a sunk cost is not a cost
    # control). What the cap governs on this tier is the metered tail, and neither
    # hop is over the ceiling at this hour — hence nothing removed.
    chain_models = [hop.get("model") for hop in plugin_plan["chain"]]
    assert "glm-4.7" in chain_models, "a dollar ceiling may not evict a plan rail"
    # Whatever the cap DID remove must be gone from the chain, unless it gave way
    # entirely — the one invariant that holds under either unit regime.
    if not plugin_plan.get("time_cap_bypassed"):
        for entry in plugin_plan["capped"]:
            assert entry["model"] not in chain_models
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


# ── The size: /explain must measure the turn production sends ────────
#
# The clock was the first half of this endpoint answering a question production
# does not ask; the SIZE was the second. ``task`` is the goal line and
# ``prompt_text`` is the composed context + goal the child really receives, which
# is what ``est_input_tokens`` — and therefore every context-conditional rule and
# the derived ``min_context`` requirement — has to be measured from.

# A context-keyed row: the shape router.yaml ships for long reads. It is keyed on
# est_input_tokens, which is measured from the TEXT, so this policy is the direct
# probe for "was the turn sized from the prompt or from the goal?".
_CONTEXT_KEYED = {
    "enabled": True,
    "default": {"model": "T1"},
    "rules": [
        {
            "id": "huge-context-read",
            "when": {"est_input_tokens": {"gt": 20000}},
            "then": {"model": "T3"},
        },
    ],
    "tiers": {
        "T1": {"model": "glm-4.7", "provider": "zai"},
        "T2": {"model": "glm-5.3", "provider": "zai"},
        "T3": {"model": "gpt-5.6-terra", "provider": "openai-codex",
               "requirements": {"min_context": 200000}},
        "T4": {"model": "gpt-5.5", "provider": "openai-codex"},
    },
}

# A goal line that matches no row on its own (6-ish estimated tokens) plus a
# context that production really would send. 120k chars is ~33k tokens at the
# router's 3.6-chars-per-token ratio, so it clears the row's 20k threshold.
_TRIVIAL_GOAL = "summarise this log"
_BIG_CONTEXT = "WARN retry scheduled for the nightly job\n" * 3000
_COMPOSED = f"Context: {_BIG_CONTEXT.strip()}\n\nTask: {_TRIVIAL_GOAL}"


def test_plugin_explain_sizes_the_turn_from_the_prompt_not_the_goal(plugin_config):
    """A context-heavy turn must preview as the route production takes.

    Sized from the goal line the same turn measures ~6 estimated tokens: no row
    matches, the plan derives a trivial ``min_context``, and the operator is shown
    a plan that never existed. Both calls go through the same endpoint, so the
    ONLY difference is which text was measured.
    """
    plugin_config(_CONTEXT_KEYED)

    sized = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_OFF_PEAK, prompt_text=_COMPOSED,
    ))
    assert sized["matched_rule_id"] == "huge-context-read"
    assert sized["matched_clauses"]["est_input_tokens"] == {"gt": 20000}
    assert sized["output"]["model"] == "gpt-5.6-terra", "T3 is the long-context tier"
    assert sized["preview"]["sized_from"] == "prompt_text"
    assert sized["preview"]["prompt_chars"] == len(_COMPOSED)
    sized_plan = _plan_of(sized)
    assert sized_plan["requirements"]["min_context"] > 20000, (
        "the derived floor must come from the real input size"
    )
    assert sized_plan["chain"], "a filter may never empty the chain"

    # The same goal with no prompt: the historical behaviour, now LABELLED.
    goal_only = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, at=_OFF_PEAK))
    assert goal_only["matched_rule_id"] is None, "6 tokens matches no context row"
    assert goal_only["output"]["model"] == "glm-4.7", "it falls through to T1"
    assert goal_only["preview"]["sized_from"] == "task"
    assert goal_only["preview"]["prompt_chars"] == len(_TRIVIAL_GOAL)
    assert _plan_of(goal_only)["requirements"]["min_context"] < 1000


def test_plugin_explain_agrees_with_the_service_on_the_same_prompt(plugin_config):
    """One turn, one instant, one prompt — the two surfaces must agree.

    Agreement is the assertion, not any particular chain: the plan
    ``RouterService`` composes is the one production attempts. Asserting the
    dashboard's own answer alone is exactly how this endpoint shipped twice with a
    plan production would never produce — first with no clock, then with the turn
    sized from the goal line.
    """
    plugin_config(_CONTEXT_KEYED)

    plugin = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    ))
    service = _service_explain(_TRIVIAL_GOAL, _PEAK, prompt_text=_COMPOSED)
    decision = service["decision"]

    for key in ("matched_rule_id", "output", "cause", "matched_clauses"):
        assert plugin[key] == decision[key], f"the two surfaces disagree on {key}"
    assert _plan_of(plugin) == _plan_of(service)
    assert plugin["evaluated_at"] == service["evaluated_at"]
    # The size disclaimer is shared material too: a console rendering either
    # surface must read the same "this measured the real turn" note.
    assert plugin["preview"] == service["preview"]
    # Two goal-sized plans would also "agree", so pin that this call really
    # measured the composed prompt on BOTH surfaces.
    assert plugin["preview"]["sized_from"] == "prompt_text"
    assert plugin["matched_rule_id"] == "huge-context-read"


def test_plugin_explain_refuses_an_unusable_prompt_text(plugin_config):
    """An unusable prompt is the CALLER's error: a 400, never a wrong size.

    Truncating or coercing it would answer with a smaller ``est_input_tokens``
    than the turn really has — a confidently wrong plan, which is the precise
    failure this parameter exists to fix — and the bound is what keeps an
    unauthenticated read path from being made to cost arbitrary CPU.
    """
    plugin_config(_CONTEXT_KEYED)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=17))
    assert raised.value.status_code == 400
    assert "prompt_text" in str(raised.value.detail)

    oversized = "x" * (1_048_576 + 1)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=oversized))
    assert raised.value.status_code == 400
    assert "prompt_text" in str(raised.value.detail)
    # The refusal is the service's own, so the two surfaces refuse the same input.
    with pytest.raises(ValueError):
        _service_explain(_TRIVIAL_GOAL, _OFF_PEAK, prompt_text=oversized)


def test_plugin_explain_treats_an_empty_prompt_text_as_the_task(plugin_config):
    """An empty field is an absent parameter; whitespace is text, as production has it.

    A form that submits ``prompt_text=`` must not change the answer, and must not
    400 the panel. Whitespace is NOT normalised away — ``adapter.route`` sizes the
    turn with the same falsy test — so that case is asserted against the SERVICE
    rather than against a number this file made up.
    """
    plugin_config(_CONTEXT_KEYED)

    empty = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=""))
    assert empty["preview"]["sized_from"] == "task"
    assert empty["preview"]["prompt_chars"] == len(_TRIVIAL_GOAL)

    blank = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_OFF_PEAK, prompt_text="   ",
    ))
    service = _service_explain(_TRIVIAL_GOAL, _OFF_PEAK, prompt_text="   ")
    assert blank["preview"] == service["preview"]


def test_plugin_explain_post_is_the_same_answer_through_a_wider_pipe(plugin_config):
    """A 120k-char prompt does not fit in a URL, so the body carries it.

    The two forms must be one handler: same names, same validation, same payload
    for the same input. A POST that answered differently would be the two-surfaces
    defect inside a single file — and a GET-only endpoint could not carry the
    prompt this parameter exists for at all, which is a preview that silently goes
    back to measuring the goal line.
    """
    plugin_config(_CONTEXT_KEYED)

    posted = asyncio.run(plugin_api.api_explain_post(
        {"task": _TRIVIAL_GOAL, "at": _OFF_PEAK, "prompt_text": _COMPOSED}
    ))
    got = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_OFF_PEAK, prompt_text=_COMPOSED,
    ))
    assert posted == got
    assert posted["matched_rule_id"] == "huge-context-read"
    assert posted["preview"]["prompt_chars"] == len(_COMPOSED)
    assert len(_COMPOSED) > 100_000, "the pipe has to be wider than a query string"

    # An explicit null is the one thing a query string cannot express: it reads as
    # "not supplied", not as an unusable value.
    nulled = asyncio.run(plugin_api.api_explain_post(
        {"task": _TRIVIAL_GOAL, "at": _OFF_PEAK, "prompt_text": None}
    ))
    assert nulled["preview"]["sized_from"] == "task"

    # Fail-closed on the caller's own input, with the sidecar's wording.
    for body, fragment in (
        ({"task": _TRIVIAL_GOAL, "at": 7}, "at must be a string"),
        ({"task": _TRIVIAL_GOAL, "prompt_text": 17}, "prompt_text must be a string"),
        ({"at": _OFF_PEAK}, "task is required"),
        ("not an object", "must be a JSON object"),
        # No body at all is "no task", not a TypeError: the parameter defaults to
        # None and an absent task is the caller's error, refused like any other.
        (None, "task is required"),
    ):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(plugin_api.api_explain_post(body))
        assert raised.value.status_code == 400
        assert fragment in str(raised.value.detail)


def test_plugin_explain_reads_a_datetime_at_exactly_as_its_iso_spelling(plugin_config):
    """``at`` may arrive as a datetime, and must mean the same instant.

    The HTTP layer only ever produces text, but this module's helpers are called
    in process too (the sidecar and the CLI pass datetimes to the service), and a
    surface where ``07:30Z`` and ``datetime(7, 30, tzinfo=utc)`` answer differently
    is the same drift in miniature. A naive value is taken to already BE UTC —
    the reading ``rules`` and ``capabilities`` use — rather than localised, because
    localising it would silently move the hour every price window is keyed on.
    """
    plugin_config(_TIME_KEYED)

    spelled = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    aware = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_TASK, at=datetime(2026, 8, 17, 7, 30, tzinfo=timezone.utc),
    ))
    naive = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_TASK, at=datetime(2026, 8, 17, 7, 30),
    ))
    assert aware == spelled and naive == spelled
    # Another zone, the same instant: converted, not re-read as a local hour.
    shifted = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_TASK,
        at=datetime(2026, 8, 17, 9, 30, tzinfo=timezone(timedelta(hours=2))),
    ))
    assert shifted == spelled
    assert spelled["evaluated_at"]["utc_hour"] == 7


# ── Every read path, over the install an operator actually has ────────
#
# "No route may raise over the ROUTER's state" is this module's own contract, and
# it is not a nicety: an operator opens this panel BECAUSE the config is broken,
# and a panel that 500s tells them nothing about why. So every route is asserted
# over the states a router.yaml is really found in, and the degraded shapes are
# pinned — a console that receives {} where it expected a list renders "undefined"
# and reads as a feature that is missing rather than a file that is wrong.

_BROKEN_CONFIGS = {
    "unparseable": "enabled: [unclosed\n",
    "scalar_root": "just a string\n",
    "sequence_root": "- glm-4.7\n- mimo-v2.5\n",
    "empty": "",
}

# Every field name that would be a credential if this surface ever grew one.
# "token" is deliberately absent: est_input_tokens and max_input_tokens are
# routing material, and a substring rule that flagged them would have to be
# weakened until it caught nothing.
_CREDENTIAL_SHAPED = (
    "api_key", "apikey", "secret", "password", "passwd", "credential",
    "authorization", "bearer", "private_key", "access_token", "auth_token",
)

# A policy carrying real bans, so the two ban surfaces have something to agree on.
_BANNED = {
    **_TIME_KEYED,
    "blocklist": {
        "manual_ban": [
            {"model": "glm-5.3", "provider": "zai", "reason": "quota exhausted"},
            {"model": "gpt-5.5", "provider": "openai-codex", "reason": "billing"},
        ],
        "fallback_chain": ["glm-4.7", "mimo-v2.5"],
        # Enabled so /blocklist really reaches for persisted breaker state, which
        # is what gives "a read path never writes it" something to prove.
        "auto_breaker": {"enabled": True, "threshold": 3, "cooldown_seconds": 900},
    },
}


@pytest.fixture
def hermetic_state(tmp_path, monkeypatch):
    """Point breaker state at a throwaway HERMES_HOME and return its path.

    /blocklist reads the REAL persisted breaker state, so without this the suite
    reads (and a regression could write) the operator's own state file. The
    returned path is also the assertion: a read path must never create it.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return tmp_path / "hermes" / "delegate-profile" / "state" / "breaker-state.json"


def _reads():
    """Every route on this surface except /explain, which needs a task."""
    return {
        "status": plugin_api.api_status,
        "rules": plugin_api.api_rules,
        "blocklist": plugin_api.api_blocklist,
        "log": plugin_api.api_log,
        "lint": plugin_api.api_lint,
    }


@pytest.mark.parametrize("flavour", [*sorted(_BROKEN_CONFIGS), "missing"])
def test_plugin_read_paths_answer_over_a_broken_install(
    plugin_config, hermetic_state, monkeypatch, flavour
):
    """No route raises over the router's state, whatever state that is.

    Four real ones plus an absent file: a half-typed flow sequence (the shape a
    hand edit leaves behind), a scalar and a sequence root (both load fine and are
    both the wrong shape, so a type guard rather than the parser has to catch
    them), and an empty file — which is what a truncated atomic write leaves.
    """
    monkeypatch.setattr(plugin_api, "_log", plugin_api.DecisionLog())
    path = plugin_config(_TIME_KEYED)
    if flavour == "missing":
        path.unlink()
    else:
        path.write_text(_BROKEN_CONFIGS[flavour], encoding="utf-8")

    served = {name: asyncio.run(read()) for name, read in _reads().items()}

    status, lint = served["status"], served["lint"]
    assert status["valid"] is False
    assert status["validation_errors"], "the operator must be told what is wrong"
    # The panel's light and the write gate refuse for the SAME reason. They read
    # one loader, so a divergence here would mean an operator staring at a green
    # panel while every apply is refused.
    assert lint["errors"] == status["validation_errors"]
    assert lint["valid"] == status["valid"]
    assert status["warnings"] == [], "a load failure is an error, never an advisory"
    assert status["tiers"] == [] and status["banned_models"] == []
    assert status["classifier_model"] == ""
    assert served["rules"] == {
        "rules": [], "default": {}, "tiers": {}, "fail_safe": {},
    }
    assert served["blocklist"] == RouterService(plugin_api._CONFIG_PATH).blocklist()
    assert served["blocklist"]["manual_bans"] == []
    assert served["log"] == {"entries": []}

    # /explain deliberately still answers, and its plan keeps the shape the
    # console branches on — including time_agnostic, which is what stops the
    # browser pricing a clockless plan against its own hour.
    explained = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK))
    assert explained["output"] == {} and explained["matched_rule_id"] is None
    plan = explained["chain_plan"]
    for key in ("chain", "requirements", "rejected", "strategy", "time_agnostic"):
        assert key in plan, f"the degraded plan must still carry {key}"
    assert plan["chain"] == [], "no policy, nothing to attempt"
    # The decision was still recorded, so replay does not go blind on a bad file.
    assert asyncio.run(plugin_api.api_log(tail=1))["entries"][0]["task"] == (
        _TRIVIAL_TASK
    )
    assert not hermetic_state.exists(), "a read path must not create breaker state"
    # ...and a PREVIEW is not a route: the operator's replay trace must not fill
    # up with dashboard polls. This plugin records to its own in-memory log only.
    assert not Path(os.environ["HERMES_ROUTE_TRACE_FILE"]).exists()


def _key_names_and_strings(payload):
    """Every key name and every string value anywhere in a served payload."""
    keys, values, stack = [], [], [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                keys.append(str(key))
                stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            values.append(item)
    return keys, values


def test_plugin_serves_no_credential(plugin_config, hermetic_state, monkeypatch):
    """Only non-secret operational state, asserted rather than assumed.

    This panel is reachable from a browser and its answers get pasted into
    issues, so a token echoed once is leaked for good. Two halves: no field this
    surface serves is credential-shaped, and nothing it serves came from the
    process environment, which is where the provider keys actually live — the
    router config holds none, and a read path that started resolving them would
    be a leak no shape assertion would catch.
    """
    plugin_config(_BANNED)
    monkeypatch.setenv("ZAI_API_KEY", "sk-canary-must-not-be-served")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-canary-must-not-be-served")

    served = {name: asyncio.run(read()) for name, read in _reads().items()}
    served["explain"] = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_TASK, at=_PEAK, prompt_text=_COMPOSED,
    ))
    # A traversal that reached nothing would pass every assertion below, so pin
    # that it descends into the nested material first.
    policy_keys, policy_values = _key_names_and_strings(served["rules"])
    assert "time_cap" in policy_keys and "glm-4.7" in policy_values

    for name, payload in served.items():
        keys, values = _key_names_and_strings(payload)
        assert keys, f"/{name} served nothing to check"
        for key in keys:
            assert not any(shape in key.lower() for shape in _CREDENTIAL_SHAPED), (
                f"/{name} serves a credential-shaped field: {key}"
            )
        for value in values:
            assert "sk-canary" not in value, f"/{name} echoed an environment secret"

    # /status invents exactly the two legacy keys the bundled UI reads on top of
    # the service's snapshot — nothing else, secret or otherwise, is added here.
    service_status = RouterService(plugin_api._CONFIG_PATH).status()
    assert set(served["status"]) - set(service_status) == {
        "banned_models", "classifier_model",
    }


def test_plugin_blocklist_and_status_name_the_same_bans(plugin_config, hermetic_state):
    """The chip list and the blocklist endpoint are one fact served twice.

    ``banned_models`` is projected BY HAND in this module while ``manual_bans``
    comes from ``Blocklist``; the bundled dashboard UI reads the first and the
    console reads the second. Asserting either alone would let them drift into
    disagreeing about which rails are banned — which is the whole recurring
    defect, scoped to one file.
    """
    plugin_config(_BANNED)

    status = asyncio.run(plugin_api.api_status())
    blocked = asyncio.run(plugin_api.api_blocklist())

    assert blocked == RouterService(plugin_api._CONFIG_PATH).blocklist()
    assert status["banned_models"] == [ban["model"] for ban in blocked["manual_bans"]]
    assert status["banned_models"] == ["glm-5.3", "gpt-5.5"]
    assert blocked["fallback_chain"] == ["glm-4.7", "mimo-v2.5"]
    # One config field, two projections on one surface.
    assert status["breaker_enabled"] == blocked["breaker_enabled"] is True
    assert blocked["breaker_cooldowns"] == [], "no persisted state, no cooldowns"
    assert not hermetic_state.exists(), "reading the blocklist must not write state"


def test_plugin_status_drops_a_ban_row_it_cannot_name(plugin_config):
    """A ban row without a model is omitted, not rendered as a blank chip.

    ``manual_ban`` is hand-edited YAML, so the rows really do arrive malformed.
    The chip list can only show models, so a row that names none is dropped here
    while /blocklist still reports it verbatim for the console to describe.
    """
    plugin_config({**_TIME_KEYED, "blocklist": {"manual_ban": [
        "glm-5.3", {"reason": "someone forgot the model"}, {"model": "gpt-5.5"},
    ]}})

    status = asyncio.run(plugin_api.api_status())
    assert status["banned_models"] == ["gpt-5.5"]
    assert asyncio.run(plugin_api.api_blocklist())["manual_bans"] == [
        "glm-5.3", {"reason": "someone forgot the model"}, {"model": "gpt-5.5"},
    ]


def test_plugin_log_serves_the_decision_explain_returned(plugin_config, monkeypatch):
    """One decision, described twice: the response and the recorded entry.

    /log is the surface that DISPLAYS what /explain ran, so a divergence here is
    the recurring defect at its smallest scale. Two ways it could arise, both
    asserted: ``DecisionLog.record`` rewrites any cause outside its closed set to
    ``fail_safe_strong``, and the recorded attempted head comes off the PLAN while
    ``output.model`` is the declared tier primary — a replay reading the wrong one
    names a rail the planner never chose.
    """
    log = plugin_api.DecisionLog()
    monkeypatch.setattr(plugin_api, "_log", log)
    plugin_config(_TIME_KEYED)

    explained = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    served = asyncio.run(plugin_api.api_log())

    assert served["entries"] == log.tail(50)
    entry = served["entries"][-1]
    assert entry["cause"] == explained["cause"], "the log must not relabel the cause"
    assert entry["rule_id"] == explained["matched_rule_id"]
    assert entry["output"]["model"] == explained["output"]["model"]
    head = explained["chain_plan"]["chain"][0]
    assert entry["output"]["attempted_model"] == head["model"]
    assert entry["output"]["attempted_provider"] == head["provider"]
    assert entry["chain_plan"]["chain"] == explained["chain_plan"]["chain"]
    assert entry["task"] == _TRIVIAL_TASK

    # ``tail`` bounds the window for the browser AND has a real Python default.
    # Spelled ``tail: int = Query(50, ...)`` the default an in-process caller
    # received was fastapi's Query sentinel, and DecisionLog.tail died on
    # ``-Query(...)`` — a TypeError out of the one surface that promises never to
    # raise, invisible to the HTTP layer that substitutes the default itself. Both
    # halves are asserted, because losing the bound to fix the default is no fix.
    for _ in range(3):
        asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    assert len(asyncio.run(plugin_api.api_log(tail=2))["entries"]) == 2
    assert asyncio.run(plugin_api.api_log()) == {"entries": log.tail(50)}
    assert inspect.signature(plugin_api.api_log).parameters["tail"].default == 50
    # The bound as the BROWSER is told it, read off the mounted schema rather than
    # off the parameter object, so this passes only while the HTTP contract holds.
    app = FastAPI()
    app.include_router(plugin_api.router)
    declared = next(
        parameter
        for parameter in app.openapi()["paths"]["/log"]["get"]["parameters"]
        if parameter["name"] == "tail"
    )
    assert declared["required"] is False
    assert declared["schema"]["default"] == 50
    assert (declared["schema"]["minimum"], declared["schema"]["maximum"]) == (1, 500)


# ── The router/ vintages this file can be deployed beside ─────────────
#
# dashboard/ and router/ are deployed by FILE COPY, so this module can land next
# to a router/ that predates any helper it delegates to. That is why each
# delegation has a local mirror — and why the mirrors have to be exercised: an
# unexercised mirror is a second implementation of the composition that produced
# the missing clock in the first place, and nobody would know if it drifted.

# The helpers this surface delegates to RouterService for.
_DELEGATED = (
    "_resolve_prompt", "_explain_features", "_explain_decision",
    "_chain_plan_of", "_evaluated_at", "_preview_note",
)

# task, at, prompt_text — one input sized from the goal and one from a composed
# prompt, so both arms of the mirrored prompt resolution are compared.
_MIRROR_INPUTS = (
    (_TRIVIAL_TASK, _PEAK, None),
    (_TRIVIAL_GOAL, _PEAK, _COMPOSED),
)


@pytest.mark.parametrize("service_clock", [True, False],
                         ids=["with_clock_helper", "without_clock_helper"])
def test_plugin_mirrors_agree_with_the_helpers_they_mirror(
    plugin_config, monkeypatch, service_clock
):
    """The local mirrors must answer exactly what the delegates answer.

    "Behaviourally equivalent" is the claim the mirrors are documented with, and
    it is the only thing that makes them safe: an operator on an older router/
    must not be shown a different plan from the one this surface shows on a
    current one. Asserted against the delegated payload rather than against
    literals, so the mirror cannot pass by agreeing with a copy of itself.

    The one deliberate difference is the preview NOTE, which degrades to the two
    keys this route resolved itself — an absent ``sized_from`` renders as
    "undefined" and reads as a preview that measured nothing at all.
    """
    plugin_config(_TIME_KEYED)
    delegated = [
        asyncio.run(plugin_api.api_explain(task=task, at=at, prompt_text=prompt))
        for task, at, prompt in _MIRROR_INPUTS
    ]

    for name in _DELEGATED:
        monkeypatch.delattr(RouterService, name)
    if not service_clock:
        # A router/ predating the time layer entirely: the clock features are the
        # EDGE's job, so their absence downstream may not make this endpoint
        # time-blind again.
        monkeypatch.delattr(plugin_api._service_mod, "_clock_features")

    for (task, at, prompt), expected in zip(_MIRROR_INPUTS, delegated):
        mirrored = asyncio.run(
            plugin_api.api_explain(task=task, at=at, prompt_text=prompt)
        )
        assert mirrored["preview"] == {
            "sized_from": expected["preview"]["sized_from"],
            "prompt_chars": expected["preview"]["prompt_chars"],
        }
        for key, value in expected.items():
            if key == "preview":
                continue
            assert mirrored[key] == value, f"the mirror disagrees on {key}"
        # Two time-blind answers would also "agree", so pin that the clock still
        # reached the planner through the mirrored composition.
        assert mirrored["chain_plan"]["time_agnostic"] is False
        assert mirrored["evaluated_at"]["utc_hour"] == 7


def test_plugin_mirror_refuses_an_unusable_prompt_in_the_same_words(plugin_config):
    """Both vintages refuse a non-string prompt identically.

    The refusal is the service's when the service has one, and the mirror's when
    it does not. A mirror that coerced instead would size the turn from ``str(17)``
    and answer confidently about a plan that never existed.
    """
    plugin_config(_CONTEXT_KEYED)
    with pytest.raises(HTTPException) as delegated:
        asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=17))

    with pytest.MonkeyPatch.context() as patch:
        patch.delattr(RouterService, "_resolve_prompt")
        with pytest.raises(HTTPException) as mirrored:
            asyncio.run(plugin_api.api_explain(task=_TRIVIAL_GOAL, prompt_text=17))

    assert mirrored.value.status_code == delegated.value.status_code == 400
    assert str(mirrored.value.detail) == str(delegated.value.detail)
    assert "prompt_text must be a string" in str(mirrored.value.detail)


def test_plugin_reports_that_the_clock_did_not_land_instead_of_claiming_an_hour(
    plugin_config, monkeypatch
):
    """Beside a rules.py that cannot take a clock, the two halves must agree.

    ``evaluated_at.time_aware`` is read back OFF THE PLAN, so "here is the hour I
    asked about, and no, it was not used" is a pair that cannot come apart. This
    is the honest form of the exact report the missing clock produced falsely: a
    ``cheapest_now`` tier degraded to declared order because prices could not be
    compared, which is a lie when a clock WAS injected and the truth when it was
    not.
    """
    plugin_config(_TIME_KEYED)
    for name in _DELEGATED:
        monkeypatch.delattr(RouterService, name)
    monkeypatch.setattr(plugin_api, "_EXPLAIN_ACCEPTS_RNG", False)
    monkeypatch.setattr(plugin_api, "_EXPLAIN_ACCEPTS_WHEN", False)

    result = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))
    plan, evaluated = result["chain_plan"], result["evaluated_at"]

    assert plan["time_agnostic"] is True
    assert evaluated["time_aware"] is False, "the plan saw no clock; say so"
    assert plan["multipliers"] == {}, "no hour, no multipliers to report"
    assert plan["strategy_declared"] == "cheapest_now"
    assert plan["strategy"] == "sequential" and plan["strategy_degraded"] is True
    assert "no clock" in plan["strategy_degraded_reason"]
    # The hour asked about is still named — an audit record without it cannot be
    # told apart from one that answered a different question.
    assert (evaluated["at"], evaluated["utc_hour"]) == (
        "2026-08-17T07:00:00+00:00", 7
    )
    # A time-keyed RULE is a feature-vector question, not a planner one, so it
    # still fires: the vector is built at the edge either way.
    hard = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_PEAK))
    assert hard["matched_rule_id"] == "defer-heavy-work-off-peak"
    assert hard["evaluated_at"]["time_aware"] is False


def test_plugin_bypasses_a_planner_helper_it_cannot_call_correctly(
    plugin_config, monkeypatch
):
    """A helper whose parameters this surface cannot satisfy is not called at all.

    Both halves of the resolution are the point. A pre-clock helper (no ``when``)
    must be BYPASSED — calling it would silently drop the clock, which is the
    original defect — while a helper that simply predates the ``features``
    argument must still be called, by keyword, against what it declares: the
    helper has grown a parameter once already, and a positional call would turn
    the next such addition into a TypeError inside a read path.
    """
    plugin_config(_TIME_KEYED)
    real = RouterService._explain_decision
    delegated = asyncio.run(plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK))

    pre_clock_calls = []

    def pre_clock(task, features, config):
        # Never reached; recorded so the assertion below can prove that.
        pre_clock_calls.append(task)
        return {}

    monkeypatch.setattr(RouterService, "_explain_decision", staticmethod(pre_clock))
    assert asyncio.run(
        plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK)
    ) == delegated
    assert pre_clock_calls == [], "a clockless helper must not plan for this surface"

    # Keyword-ONLY on purpose: a positional call would raise TypeError here, which
    # is what pins that the plugin calls this helper against its declared names.
    pre_features_calls = []

    def pre_features(*, task, config, when):
        pre_features_calls.append(task)
        return real(task=task, config=config, when=when,
                    features=RouterService._explain_features(task, when))

    monkeypatch.setattr(RouterService, "_explain_decision",
                        staticmethod(pre_features))
    assert asyncio.run(
        plugin_api.api_explain(task=_TRIVIAL_TASK, at=_PEAK)
    ) == delegated
    assert pre_features_calls == [_TRIVIAL_TASK]


@pytest.mark.parametrize("helper, unusable", [
    ("_resolve_prompt", "not a (text, sized_from) pair"),
    ("_explain_features", ["not", "a", "feature", "vector"]),
    ("_chain_plan_of", None),
    ("_evaluated_at", "not a mapping"),
])
def test_plugin_ignores_a_helper_answering_in_a_shape_it_cannot_use(
    plugin_config, monkeypatch, helper, unusable
):
    """An unusable answer degrades to the mirror, not to a broken panel.

    Every delegation is type-guarded because the helper on the other side belongs
    to a separately deployed file. The guard is only worth having if what it
    degrades TO is the same answer, so that is what is asserted.
    """
    plugin_config(_TIME_KEYED)
    delegated = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    ))

    if helper == "_resolve_prompt":  # an instance method, not a static one
        monkeypatch.setattr(
            RouterService, helper, lambda self, task, prompt_text: unusable
        )
    else:
        monkeypatch.setattr(RouterService, helper,
                            staticmethod(lambda *args: unusable))

    assert asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    )) == delegated


def test_plugin_injects_the_clock_even_when_the_service_helper_is_unusable(
    plugin_config, monkeypatch
):
    """The clock features are the edge's contribution and cannot be lost downstream.

    ``signals.extract()`` is pure, so ``utc_hour`` exists in the vector only
    because this edge puts it there. A service whose ``_clock_features`` answers in
    a shape this surface cannot use does not get to make the endpoint time-blind
    again — that is precisely the state a time-keyed rule was inert in.
    """
    plugin_config(_TIME_KEYED)
    delegated = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_PEAK))

    monkeypatch.delattr(RouterService, "_explain_features")
    monkeypatch.setattr(plugin_api._service_mod, "_clock_features",
                        lambda when: "not a mapping")

    result = asyncio.run(plugin_api.api_explain(task=_HARD_TASK, at=_PEAK))
    assert result == delegated
    assert result["matched_rule_id"] == "defer-heavy-work-off-peak"
    assert result["evaluated_at"]["utc_hour"] == 7


def test_plugin_preview_note_always_names_the_text_it_measured(
    plugin_config, monkeypatch
):
    """Whatever the note helper is, the preview says which text produced the plan.

    A note that predates the two size keys keeps its own material and gains
    them; a note this surface cannot use at all degrades to exactly the two keys
    this route resolved itself. The failure both cases exist to prevent is the
    same: a preview sized from the goal line is byte-shaped like one sized from
    the real turn, so an unlabelled note answers a different question invisibly.
    """
    plugin_config(_CONTEXT_KEYED)

    monkeypatch.setattr(RouterService, "_preview_note", staticmethod(
        lambda decision, plan: {"seed": 0, "reproducible_within": "utc_hour"}
    ))
    older = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    ))
    assert older["preview"] == {
        "seed": 0,
        "reproducible_within": "utc_hour",
        "sized_from": "prompt_text",
        "prompt_chars": len(_COMPOSED),
    }

    monkeypatch.setattr(RouterService, "_preview_note",
                        staticmethod(lambda *args: "not a note"))
    unusable = asyncio.run(plugin_api.api_explain(
        task=_TRIVIAL_GOAL, at=_PEAK, prompt_text=_COMPOSED,
    ))
    assert unusable["preview"] == {
        "sized_from": "prompt_text", "prompt_chars": len(_COMPOSED),
    }
    # The rest of the answer is the delegated one either way.
    assert older["matched_rule_id"] == unusable["matched_rule_id"] == (
        "huge-context-read"
    )


# ── The two module layouts this file is deployed into ─────────────────


def test_plugin_binds_the_sibling_router_under_a_package_layout(tmp_path, monkeypatch):
    """Imported as a package, the plugin must bind its SIBLING router modules.

    Resolving the absolute ``router`` name first did technically work under
    Hermes's ``hermes_plugins.<slug>`` shape — the sys.path insertion at the top
    of the module makes the plugin root importable — but it bound a SECOND,
    independent copy of the router package under the top-level name, so this read
    path saw different module-level state (its own rule caches, its own breaker)
    than the write path did. Two copies of one router is the same defect as two
    views of one decision, so the binding is asserted instead of assumed.

    The insertion itself is asserted here too, in the layout that needs it: the
    plugin root has to become importable EXACTLY once, since a duplicate sys.path
    entry is another way a second copy appears.
    """
    package = tmp_path / "hermes_plugins_probe"
    (package / "dashboard").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "dashboard" / "__init__.py").write_text("", encoding="utf-8")
    # Symlinked, not copied: the file under test must be the one that ships.
    (package / "dashboard" / "plugin_api.py").symlink_to(
        ROOT / "dashboard" / "plugin_api.py"
    )
    (package / "router").symlink_to(ROOT / "router", target_is_directory=True)

    plugin_dir = str(plugin_api._PLUGIN_DIR)
    monkeypatch.setattr(
        sys, "path", [entry for entry in sys.path if entry != plugin_dir]
    )
    sys.path.insert(0, str(tmp_path))
    try:
        packaged = importlib.import_module(
            "hermes_plugins_probe.dashboard.plugin_api"
        )
        sibling = importlib.import_module("hermes_plugins_probe.router.service")
        assert packaged.RouterService is sibling.RouterService
        assert packaged.RouterService is not RouterService, (
            "the top-level copy is a different module with its own state"
        )
        assert sys.path.count(plugin_dir) == 1, (
            "the plugin root must become importable exactly once"
        )
    finally:
        for name in [n for n in sys.modules if n.startswith("hermes_plugins_probe")]:
            del sys.modules[name]


def test_plugin_makes_its_own_plugin_root_importable_in_the_flat_layout(monkeypatch):
    """In the shipped flat layout the absolute import needs that sys.path entry.

    ``dashboard`` is itself top-level there, so ``..router`` is beyond the
    top-level package and ``router`` is the only name that can resolve — which it
    can only do while the plugin root is on sys.path. Asserted by importing this
    module with the entry REMOVED, which is how it arrives in the dashboard's
    plugin loader: it must put the entry back, exactly once (a duplicate entry is
    one of the ways a second copy of the router package appears), and still bind
    the very modules the write path uses rather than fresh ones.

    Reloaded rather than imported under a second name on purpose: a second name
    would BE the duplicate-copy defect this test exists to rule out.
    """
    plugin_dir = str(plugin_api._PLUGIN_DIR)
    monkeypatch.setattr(
        sys, "path", [entry for entry in sys.path if entry != plugin_dir]
    )

    reloaded = importlib.reload(plugin_api)

    assert sys.path.count(plugin_dir) == 1, "the plugin root must be reachable again"
    assert reloaded is plugin_api
    assert reloaded.RouterService is RouterService, (
        "one router package for both paths, not a second top-level copy"
    )
    assert reloaded.DecisionLog is DecisionLog
    assert reloaded._CONFIG_PATH == reloaded._PLUGIN_DIR / "router.yaml"


def test_json_actions_markup_is_what_the_js_harness_mirrors():
    """CA8: the write buttons live in ``#jsonActions``, UNLABELLED — the labels are the map's.

    ``tests/test_console_logic.js`` measures whether a ``Salvar`` exists **in the DOM**
    while the file carries a lint error — absent, not disabled (DESIGN.md:435-463). Its
    DOM stub has no markup, so the test seeds the box the way this file writes it
    (``seedJsonActions``). That mirror is only honest while the markup really is this,
    which is what this test pins: rename the container or move a button out of it and
    the JS test would keep passing over a fiction until this one fails.

    §4.7 decision 2: the LABELS are not in the markup at all. They live in the WRITE
    map — the single source — and boot stamps them (``stampWriteLabels``); a label in
    the markup would be the second copy the rule exists to prevent, and the JS mirror
    seeds the very labels the map spells.
    """
    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    assert '<div class="actions" id="jsonActions">' in html, (
        "the write buttons need a named container for a test to ask what is inside it"
    )
    box = html.split('<div class="actions" id="jsonActions">', 1)[1].split("</div>", 1)[0]
    for element_id in ("jsonApply", "jsonPreview", "jsonRevert"):
        assert f'id="{element_id}"' in box, f"{element_id} must live inside #jsonActions"
    # §4.7: no write vocabulary in the markup — the WRITE map is the only copy,
    # and stampWriteLabels hands the labels to these very buttons at boot.
    for label in ("Salvar", "Ver o que muda", "Voltar à versão anterior"):
        assert label not in box, (
            f"{label!r} is a §4.7 literal; hardcoded in the markup it would be a "
            "second copy of the map the console stamps from"
        )
    # jsonApply first in the file, so the console can put it back where it was after
    # detaching it: the JS test asserts that order and it comes from here.
    assert box.index('id="jsonApply"') < box.index('id="jsonPreview"'), (
        "Salvar precedes Ver o que muda in the restored order, and the markup is "
        "the order the console restores it to"
    )
    script = _console_inline_script()
    assert "function stampWriteLabels()" in script, (
        "boot stamps the static buttons from the map; without it the editor's "
        "buttons would render empty"
    )
    for key in ("WRITE.save", "WRITE.plan", "WRITE.revert"):
        assert key in script, f"the map must be the source of {key}"


def test_console_gates_the_save_button_on_the_lint_errors():
    """The gate exists, and it DETACHES rather than disabling.

    A static read, because the behaviour is measured dynamically in the JS suite: what
    this pins is that the console never reaches for ``$('jsonApply')`` unguarded, which
    would throw the moment the node is detached — the failure mode that turns a lint
    error into a blank screen.
    """
    script = _console_inline_script()
    assert "function syncSaveButtons()" in script
    assert "removeChild(save)" in script, "absent, not disabled (DESIGN.md:435-463)"
    assert "function saveButton()" in script, (
        "one accessor for a node that may be detached; getElementById cannot find it then"
    )
    assert "$('jsonApply').disabled" not in script, (
        "an unguarded read of a detachable node is the crash this accessor prevents"
    )


def _panel(html: str, name: str) -> str:
    """The markup of one screen, from its <section> to the next one."""
    start = html.index(f'id="panel-{name}"')
    rest = html[start:]
    end = rest.index("</section>")
    return rest[:end]


def test_console_reads_in_the_order_the_spec_fixes():
    """Each screen carries its own subject, in the order the reader needs it.

    The earlier §1.2 put the groups of models and the presets on a third tab and the
    rule list on the first. That split is overturned: every one of those is something
    the operator CHANGES, so all of them are read on Configuração, in the order a
    change is decided — the rules first (what the tab is for), then the probe that
    checks one case against them, then the groups those rules point at, then the
    preset that rewrites the groups wholesale, then the two settings that are neither.

    Two moves survive from the earlier reading and are still pinned here:

    * **The list comes before the probe.** With the probe first the screen opened with
      an empty box above the answer, which reads as "type something to see anything".
    * **The presets come before the groups they rewrite** — choose the strategy, then
      read what it produced.
    """
    html = (EXTENSION / "console.html").read_text(encoding="utf-8")
    config = _panel(html, "pipeline")
    operation = _panel(html, "routes")

    assert 'id="sheet"' in config
    assert config.index('id="sheet"') < config.index('id="probeForm"'), (
        "the ordered list of task types is the answer; the probe checks one case against it"
    )
    assert 'id="ladder"' in config, "the groups are configuration, not observation"
    assert 'id="presetBox"' in config
    assert 'id="settings"' in config, "the classifier finally has a home, and it is here"
    assert config.index('id="probeForm"') < config.index('id="ladder"') < config.index('id="presetBox"'), (
        "rules, then the probe, then the groups the rules point at, then what rewrites them"
    )
    assert config.index('id="presetBox"') < config.index('id="settings"')
    # The file editor is last: it is the escape hatch, not the way in.
    assert config.index('id="jsonActions"') > config.index('id="settings"')

    # Operação is the runtime, and nothing on it is editable.
    assert 'id="models"' in operation and 'id="routesTable"' in operation
    assert operation.index('id="models"') < operation.index('id="routesTable"'), (
        "what can be reached, then what it decided with it"
    )
    assert 'id="jsonActions"' not in operation and 'id="presetBox"' not in operation

    # The ids themselves are the contract router-nav.js and the JS suite match on, so
    # a move must never be a rename.
    for element_id in ("sheet", "probeTask", "ladder", "routesTable", "replayPath", "chainPlan", "clockbar"):
        assert f'id="{element_id}"' in html, f"{element_id} keeps its historic id"
