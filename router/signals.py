"""Signal extraction — compute a flat feature vector from a task turn.

No IO, no state, no model calls. Deterministic, depth ≤ 1.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Verb classification — cheap keyword-based, no model
# ---------------------------------------------------------------------------

_TRIVIAL_VERBS: set[str] = {
    "rename", "format", "typo", "indent", "spelling", "lint",
    "whitespace", "sort imports", "fix typo", "add comment",
    "remove dead code", "bump version", "update changelog",
}

_HARD_VERBS: set[str] = {
    "debug", "refactor", "secure", "concurrent", "prove", "optimize",
    "race condition", "deadlock", "thread-safe", "memory leak",
    "vulnerability", "exploit", "injection", "overflow",
    "redesign", "rewrite", "migrate schema", "data migration",
}

_CODE_KEYWORDS: set[str] = {
    "def ", "class ", "function", "method", "import ", "from ",
    "module", "package", "library", "api", "endpoint", "route",
    "middleware", "handler", "controller", "service", "repository",
    "code", "file", "script", ".py", ".ts", ".js", ".go", ".rs",
    "patch", "diff", "commit", "pull request", "pr",
}

_REVIEW_KEYWORDS: set[str] = {
    "review", "audit", "inspect", "check", "assess", "evaluate",
}

# ---------------------------------------------------------------------------
# Context-size heuristics — tunable knobs, NOT measurements
#
# _CHARS_PER_TOKEN is the working ratio for mixed prose-and-code English; real
# tokenizers vary per model and per content mix. The two allowances below exist
# because what drives context need is the material a turn *references* rather
# than the material it *contains*: a one-line "refactor these 6 files" turn is
# cheap to read and expensive to serve. Both numbers are order-of-magnitude
# guesses meant to be re-tuned from decision-log data, not to be exact.
# ---------------------------------------------------------------------------

_CHARS_PER_TOKEN: float = 3.6
_TOKENS_PER_REFERENCED_FILE: int = 4000
_WHOLE_REPO_TOKEN_ALLOWANCE: int = 40000


def chars_per_token() -> float:
    """The working chars-per-token ratio, for the one other module that needs it.

    Exposed rather than duplicated: :mod:`router.classify` has to convert a model's
    context WINDOW (tokens) into a character budget for the prompt it builds, which is
    the same conversion this module performs in the opposite direction when estimating
    ``est_input_tokens``. A second copy of the number would be free to drift, and the
    two would then disagree about how big the same text is.

    A function, not the bare constant, so a caller cannot rebind it — the same reason
    :func:`router.classify.classifier_defaults` returns a copy.
    """
    return _CHARS_PER_TOKEN

_WHOLE_REPO_MARKERS: frozenset[str] = frozenset({
    "entire repo", "whole codebase", "every file", "all files",
    "across the repo",
})

# ---------------------------------------------------------------------------
# Capability hints — what the turn implies the model must be able to do
# ---------------------------------------------------------------------------
#
# Vision inference is deliberately two-tiered, because `needs_vision` is the
# most expensive signal to get wrong: it selects the vision rule, and the
# capability filter then drops every elo that cannot see, which on this
# registry can collapse a 3-hop tier to a single hop on one subscription rail.
# A false vision positive therefore costs an independent rail, so this detector
# asks whether the turn implies visual *input*, not whether it mentions a
# visual noun.
#
#   Tier 1 — unambiguous: a token that can only be a supplied image (a file
#   extension, a screenshot, a design-tool artefact). The noun alone fires,
#   but on a WORD BOUNDARY — see `_compile_marker_re`, because containment made
#   "libpng-dev" a visual turn.
#
#   Tier 2 — ambiguous nouns ("chart", "diagram", "image", "design", "plot"):
#   these appear just as often as something the model is asked to PRODUCE or
#   DISCUSS ("plot a chart from the csv", "the flow chart of the module",
#   "rebuild the docker image") as something it is asked to LOOK AT. They fire
#   only in proximity to an attachment or deictic cue ("the attached diagram",
#   "look at this chart"), never on the noun alone.

_VISION_MARKERS: frozenset[str] = frozenset({
    "screenshot", "screen shot", "png", "jpg", "jpeg", "webp",
    "figma", "mockup", "wireframe", "see attached", "the ui looks",
})

# Nouns that name a visual artefact but do not by themselves say the artefact
# was supplied to the model. Matched as whole words, plural tolerated.
#
# The two halves differ in how weak a cue may promote them. A bare deictic
# ("this X") is enough for the nouns that are almost always an artefact when
# pointed at, but NOT for "design" and "plot": "this design" is one of the most
# common phrases in a coding turn ("let's simplify this design") and "this
# plot" reads the same way, so promoting them on a deictic alone strands
# text-only work on the single vision rail. They need positive evidence of
# supply — an attachment, look-at or trailing cue ("look at this design", "the
# design attached").
_VISION_DEICTIC_NOUNS: frozenset[str] = frozenset({
    "chart", "diagram", "image",
})
_VISION_SUPPLIED_ONLY_NOUNS: frozenset[str] = frozenset({
    "design", "plot",
})
_VISION_AMBIGUOUS_NOUNS: frozenset[str] = (
    _VISION_DEICTIC_NOUNS | _VISION_SUPPLIED_ONLY_NOUNS
)

# Compounds in which an ambiguous noun modifies another noun and so names
# something that is not an image at all: a design system is a token set, a
# design doc is prose, a docker image is a tarball. They are erased before the
# proximity test, so a cue that lands beside the compound cannot promote it
# ("check out the design system tokens in tokens.css" is not visual input).
# Deliberately short: it lists the compounds this codebase's own turns use, and
# is not an attempt at general noun-modifier detection.
_VISION_NON_VISUAL_COMPOUNDS: frozenset[str] = frozenset({
    "design system", "design doc", "design document", "design pattern",
    "design review", "design decision", "docker image", "container image",
    "base image", "image tag", "image name",
})

# Cues that turn an ambiguous noun into visual input. Deictic cues must sit
# within one word of the noun ("this flow chart" yes, "in this file the
# diagram" no); attachment cues get two, because they take more qualifiers
# ("the attached wiring diagram"). Trailing cues are attachment-only: "these"
# after the noun ("plot these points") is an argument, not an attachment.
_VISION_DEICTIC_CUES: frozenset[str] = frozenset({
    "this", "these", "the following",
})
_VISION_ATTACHMENT_CUES: frozenset[str] = frozenset({
    "attached", "uploaded", "pasted", "enclosed", "provided", "included",
})
_VISION_LOOK_CUES: frozenset[str] = frozenset({
    "look at", "looking at", "see", "check out", "here is", "here's",
    "here are", "have a look at",
})
_VISION_TRAILING_CUES: frozenset[str] = frozenset({
    "attached", "above", "below", "enclosed", "uploaded", "pasted",
    "provided", "i sent", "i pasted", "i attached",
})

_STRUCTURED_OUTPUT_MARKERS: frozenset[str] = frozenset({
    "json schema", "structured output", "return json", "as json",
    "response_format", "strict schema", "typed output",
})

_TOOL_MARKERS: frozenset[str] = frozenset({
    "run", "execute", "edit", "write", "create", "delete", "install",
    "commit", "test", "build", "deploy", "search", "fetch",
    "read the file", "apply",
})

# The negative half of the tool detector. `needs_tools` is asymmetric on
# purpose (see `_detect_tools`): an action verb is enough to say True, but only
# positive evidence of a *pure question* — explanatory or interrogative
# phrasing, no action verb anywhere, and no file or path reference — is enough
# to say False. Everything else keeps the fail-closed default.
_QUESTION_MARKERS: frozenset[str] = frozenset({
    "explain", "describe", "summarize", "summarise", "clarify",
    "compare", "tell me about", "walk me through",
    "what is", "what are", "what does", "what do", "what's",
    "why is", "why are", "why does", "why do",
    "how is", "how are", "how does", "how do", "how would",
    "difference between", "pros and cons", "trade-offs between",
})

# A turn that names a concrete file, path or the repository is asking about
# material only a tool can reach, however interrogative its phrasing.
_PATH_WORD_MARKERS: frozenset[str] = frozenset({
    "file", "directory", "folder", "path", "repo", "repository",
    "codebase", "branch", "worktree",
})
_PATH_LIKE_RE = re.compile(
    r"(?:[\w.\-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|go|rs|java|rb|cs|c|h|cpp|"
    r"sh|sql|json|ya?ml|toml|ini|cfg|md|txt|html|css|log|diff|patch)\b)"
    r"|(?:[\w.\-]+/[\w.\-/]+)"
)

# Fail-closed default for needs_tools: this router only ever sees agent turns,
# so a false negative would silently route agent work to a model that cannot
# call tools (the turn then fails outright), while a false positive only
# narrows the eligible set slightly. Bias to True when nothing matches.
_TOOLS_DEFAULT: bool = True

# Markers are deliberately narrow, and matched on word boundaries rather than
# by containment (`_compile_marker_re`): bare "log" would otherwise fire on
# "logic"/"login"/"logging", bare "diff" on "different" and bare "png" on
# "libpng-dev". The ambiguous kinds are additionally keyed on file extensions
# and multi-word phrases only. A dotted marker (".gif") stays literal: the dot
# is its boundary and the bare form is deliberately not accepted, whereas
# boundary-matched "png" already covers both "png" and "shot.png", so the
# dotted duplicates the image kind used to carry are gone.
_ATTACHMENT_MARKERS: Dict[str, frozenset[str]] = {
    "image": frozenset({
        "screenshot", "image", "png", "jpg", "jpeg", "webp", ".gif", ".svg",
    }),
    "pdf": frozenset({"pdf", ".pdf"}),
    "csv": frozenset({"csv", ".csv", "spreadsheet"}),
    "log": frozenset({".log", "log file", "logfile", "log output", "logs"}),
    "diff": frozenset({
        ".diff", "git diff", "diff --git", "unified diff", "diff output",
        ".patch", "patch file",
    }),
    "html": frozenset({"html", ".htm"}),
}

# ---------------------------------------------------------------------------
# Feature vocabulary — PUBLIC SURFACE WITH A CONSUMER
#
# EXTRACTED_FEATURE_NAMES is exactly the key set `extract()` returns. It is
# exported rather than kept local because `rules.py`'s `when.<field>` lint
# imports it to reject a rule keyed on a field no signal ever produces (a typo
# there is a silently dead rule). Duplicating the list in the linter is how the
# two would drift, so there is one list and it lives next to the code that
# builds the dict. `test_signals.py` asserts the two stay identical.
#
# INJECTED_FEATURE_NAMES are the time features the *caller* adds at the edge
# (see docs/superpowers/specs/2026-08-17-time-windowed-routing-addendum.md):
# the clock is a parameter, never read here, so `extract()` does not and must
# not produce them — but a rule may legitimately key on them, so the linter
# needs to know they exist. KNOWN_FEATURE_NAMES is the union, and is the set a
# field-name lint should validate against.
# ---------------------------------------------------------------------------

EXTRACTED_FEATURE_NAMES: frozenset[str] = frozenset({
    "char_len",
    "has_code",
    "size_lines",
    "num_files",
    "has_stacktrace",
    "num_requirements",
    "verb_class",
    "lang",
    "keywords",
    "est_input_tokens",
    "needs_vision",
    "needs_structured_output",
    "needs_tools",
    "attachment_kinds",
})

INJECTED_FEATURE_NAMES: frozenset[str] = frozenset({
    "utc_hour",
    "utc_weekday",
    # The role the CALLER already fixed — a kanban card's assignee, injected by
    # the dispatch hook the same way the clock is. It is an INPUT to the decision
    # and never an output of it: the dispatcher's hook applies only
    # ("model", "provider"), so no decision can move a worker's role, and a rule
    # that is only correct for one role says so in `when` instead of naming the
    # role in `then` and being unhonorable there.
    #
    # Absent means absent: a `when` clause on a field the features do not carry
    # never matches, so a role-scoped rule is inert on every path that fixes no
    # role rather than matching arbitrarily there.
    "assignee",
})

KNOWN_FEATURE_NAMES: frozenset[str] = EXTRACTED_FEATURE_NAMES | INJECTED_FEATURE_NAMES

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(turn: str) -> Dict[str, Any]:
    """Compute a flat, depth-\u22641 feature vector from a single task turn.

    Returns a dict with:
      char_len: int
      has_code: bool
      size_lines: int       # inferred line count; 0 if no explicit count
      num_files: int        # inferred file count; 0 if no mention
      has_stacktrace: bool
      num_requirements: int # bullet points / numbered items
      verb_class: str       # \"trivial\" | \"hard\" | \"unknown\"
      lang: str             # detected programming language hint or \"\"
      keywords: list[str]   # matched keyword strings for rule matching

    Context and capability features (additive; every key above keeps its
    existing name, type and value):
      est_input_tokens: int         # heuristic context need, see below
      needs_vision: bool            # turn implies visual input
      needs_structured_output: bool # turn implies JSON / schema-constrained output
      needs_tools: bool             # turn implies acting, not only answering
      attachment_kinds: list[str]   # sorted, de-duplicated inferred kinds

    The returned key set is exactly EXTRACTED_FEATURE_NAMES; the two are kept
    in step by a test, because `rules.py`'s field-name lint reads that set.

    No key is time-dependent. `utc_hour` / `utc_weekday` are supplied by the
    caller at the edge (INJECTED_FEATURE_NAMES), because this module is pure
    and must never read the wall clock.

    est_input_tokens is a heuristic, not a measurement. It is
    ceil(char_len / 3.6) — 3.6 chars per token being the working ratio for
    mixed prose-and-code English — plus two allowances for material the turn
    only references: num_files * 4000 when a file count was inferred, and a
    flat 40000 when a whole-repo or whole-codebase read is implied. All three
    numbers are tunable knobs (see the constants near the top of this module).

    needs_tools is bidirectional but asymmetric, and the asymmetry is the
    point. Any action verb says True. Only a *pure question* — explanatory or
    interrogative phrasing with no action verb and no file or path reference —
    says False. Anything else keeps the fail-closed default of True: this
    router only ever sees agent turns, so a false negative would silently route
    agent work to a model that cannot call tools at all, whereas a false
    positive only narrows the eligible model set slightly.

    needs_vision means the turn implies visual *input*. Mentioning a visual
    noun is not enough: "plot a chart from the csv" produces a chart and "look
    at this chart" supplies one. Nor is containing one: markers match on word
    boundaries, so "libpng-dev" is a build dependency, not an image. See the
    marker tables for the two tiers.
    """
    lower = turn.lower()
    lines = turn.split("\n")
    num_files = _infer_file_count(turn)

    return {
        "char_len": len(turn),
        "has_code": _detect_code(lower),
        "size_lines": _infer_line_count(lower),
        "num_files": num_files,
        "has_stacktrace": _detect_stacktrace(turn),
        "num_requirements": _count_requirements(lines),
        "verb_class": _classify_verb(lower),
        "lang": _detect_language(lower),
        "keywords": _keyword_hits(lower),
        "est_input_tokens": _estimate_input_tokens(len(turn), num_files, lower),
        "needs_vision": _detect_vision(lower),
        "needs_structured_output": _detect_structured_output(lower),
        "needs_tools": _detect_tools(lower),
        "attachment_kinds": _infer_attachment_kinds(lower),
    }


# ---------------------------------------------------------------------------
# Internal detectors — one purpose each
# ---------------------------------------------------------------------------

def _detect_code(lower: str) -> bool:
    return any(kw in lower for kw in _CODE_KEYWORDS)


def _infer_line_count(lower: str) -> int:
    # Look for patterns like "40 lines", "~200 LOC", "500-line"
    m = re.search(r"(\d+)\s*(?:lines?|loc)", lower)
    return int(m.group(1)) if m else 0


def _infer_file_count(turn: str) -> int:
    # "2 files", "3-5 files", "across 4 modules"
    m = re.search(r"(\d+)[-–]\s*(\d+)\s*files?", turn, re.IGNORECASE)
    if m:
        return int(m.group(2))  # upper bound
    m = re.search(r"(\d+)\s*files?", turn, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _detect_stacktrace(turn: str) -> bool:
    """True when the turn carries an error-report marker. Containment, not words.

    ``"runtime error"`` used to be written ``" runtime error"``, with a leading
    space, and it was the only marker here that had one. Matched by containment, a
    leading space means the marker CANNOT fire at offset 0 or right after a
    newline — which is precisely where a pasted stack trace begins. Measured:
    ``"runtime error in the pool"`` and ``"boom\nruntime error here"`` both
    answered False while ``"a runtime error in the pool"`` answered True.

    ``has_stacktrace`` reaches the classifier prompt, so this was not inert: the
    common paste shape scored as having no error report.

    These markers deliberately do NOT go through ``_compile_marker_re``. That
    helper appends ``s?\b`` for plural tolerance, and ``\b`` after a colon can
    never match — so ``error:``, ``panic:`` and ``exception:`` would all become
    dead. Containment is the right matcher for a punctuation-bearing marker.
    """
    markers = [
        "traceback", "stack trace", "exception:", "error:",
        "panic:", "segfault", "segmentation fault", "null pointer",
        "index out of", "key error", "type error", "attribute error",
        "syntax error", "runtime error",
    ]
    lower = turn.lower()
    return any(m in lower for m in markers)


def _count_requirements(lines: List[str]) -> int:
    count = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r"^[\-\*]\s", stripped) or re.match(r"^\d+[\.\)]\s", stripped):
            count += 1
    return count


def _classify_verb(lower: str) -> str:
    if any(v in lower for v in _HARD_VERBS):
        return "hard"
    if any(v in lower for v in _TRIVIAL_VERBS):
        return "trivial"
    return "unknown"


def _detect_language(lower: str) -> str:
    lang_markers = [
        ("python", [".py", "python", "django", "flask", "fastapi"]),
        ("typescript", [".ts", ".tsx", "typescript", "react", "next.js", "angular"]),
        ("javascript", [".js", ".jsx", "javascript", "node", "express"]),
        ("go", [".go", "golang"]),
        ("rust", [".rs", "rust", "cargo"]),
        ("java", [".java", "spring", "maven"]),
        ("csharp", [".cs", "c#", ".net", "dotnet"]),
        ("ruby", [".rb", "ruby", "rails"]),
    ]
    for lang, markers in lang_markers:
        if any(m in lower for m in markers):
            return lang
    return ""


def _keyword_hits(lower: str) -> List[str]:
    """The review-intent words present in the turn, sorted.

    Reads :data:`_REVIEW_KEYWORDS` rather than an inline copy. The copy that used
    to live here was missing ``evaluate``, so the constant advertised a vocabulary
    the extractor could not produce and a rule written ``keywords: {contains:
    evaluate}`` linted clean and could never fire. No shipped rule keys on that
    word (they use ``audit`` and ``review``), so wiring it up changes no shipped
    routing — it only makes the exported vocabulary true.

    SORTED because the source is a set: iteration order varies per process, and
    this list is persisted in the decision trace. Two processes recording the same
    turn produced traces that differed only in the order of this field, which is
    enough to defeat a byte comparison between them.
    """
    return sorted(word for word in _REVIEW_KEYWORDS if word in lower)


def _estimate_input_tokens(char_len: int, num_files: int, lower: str) -> int:
    """Heuristic context need in tokens — see module constants for the knobs.

    Base is ceil(char_len / _CHARS_PER_TOKEN). On top of that, referenced
    material is charged for: _TOKENS_PER_REFERENCED_FILE per inferred file, and
    _WHOLE_REPO_TOKEN_ALLOWANCE once when a whole-repo read is implied. Never
    negative, so a nonsense count from upstream cannot produce a negative
    feature that would silently satisfy a `lt` clause.
    """
    base = math.ceil(max(0, char_len) / _CHARS_PER_TOKEN)
    total = int(base)
    if num_files > 0:
        total += num_files * _TOKENS_PER_REFERENCED_FILE
    if _detect_whole_repo(lower):
        total += _WHOLE_REPO_TOKEN_ALLOWANCE
    return max(0, total)


def _detect_whole_repo(lower: str) -> bool:
    return any(m in lower for m in _WHOLE_REPO_MARKERS)


def _compile_marker_re(markers: frozenset[str]) -> re.Pattern[str]:
    """Compile a marker set into one whole-word matcher, plural tolerated.

    Containment was the original defect: "png" fired inside "libpng-dev", so a
    makefile turn came back `needs_vision` True, lost both text-capable
    fallbacks to the capability filter and paid for a vision rail it could not
    use. A marker that already carries its own leading dot keeps it — the dot is
    the boundary, and the bare form is intentionally not accepted.
    """
    alternatives = []
    for marker in sorted(markers):
        stem = re.escape(marker)
        alternatives.append(stem + r"s?\b" if marker.startswith(".")
                            else r"\b" + stem + r"s?\b")
    return re.compile("|".join(alternatives))


_VISION_MARKER_RE: re.Pattern[str] = _compile_marker_re(_VISION_MARKERS)


def _detect_vision(lower: str) -> bool:
    """True when the turn implies visual INPUT, not merely a visual noun.

    Tier 1 is unambiguous (an image extension, a screenshot, a design-tool
    artefact) and fires on the marker alone, matched as a whole word. Tier 2 is
    the ambiguous nouns, which fire only next to an attachment or deictic cue —
    a chart the turn asks the model to *draw* is not a chart the model has to
    *see*, and conflating the two strands text-only work on the single vision
    rail.
    """
    if _VISION_MARKER_RE.search(lower):
        return True
    return _has_visual_input_cue(lower)


def _has_visual_input_cue(lower: str) -> bool:
    """Proximity test: an ambiguous visual noun next to an input cue."""
    stripped = _VISION_COMPOUND_RE.sub(" ", lower)
    for pattern in _VISION_CUE_PATTERNS:
        if pattern.search(stripped):
            return True
    return False


def _build_vision_cue_patterns() -> List[re.Pattern[str]]:
    """Compile the proximity patterns once, from the marker tables above.

    Built rather than written out so the noun lists and the cue lists stay the
    single source of truth: adding a noun cannot forget a pattern. The deictic
    pattern reads the narrower noun list — "design" and "plot" need positive
    evidence of supply, not a bare "this".
    """
    def alt(nouns: frozenset[str]) -> str:
        return r"(?:%s)s?\b" % "|".join(sorted(nouns))

    nouns = alt(_VISION_AMBIGUOUS_NOUNS)
    deictic_nouns = alt(_VISION_DEICTIC_NOUNS)
    gap_one = r"(?:\s+[\w.\-]+){0,1}\s+"   # deictic cues stay close to the noun
    gap_two = r"(?:\s+[\w.\-]+){0,2}\s+"   # attachment cues take more qualifiers
    patterns = [
        # "this chart", "this flow chart", "the following diagram"
        r"\b(?:%s)%s%s" % (
            "|".join(sorted(_VISION_DEICTIC_CUES)), gap_one, deictic_nouns,
        ),
        # "the attached diagram", "the uploaded wiring diagram"
        r"\b(?:%s)%s%s" % ("|".join(sorted(_VISION_ATTACHMENT_CUES)), gap_two, nouns),
        # "look at the chart", "here is the crash image"
        r"\b(?:%s)%s%s" % ("|".join(sorted(_VISION_LOOK_CUES)), gap_two, nouns),
        # "the diagram attached", "the plot above", "the image I pasted"
        r"\b%s%s(?:%s)\b" % (
            nouns, gap_two, "|".join(sorted(_VISION_TRAILING_CUES)),
        ),
    ]
    return [re.compile(p) for p in patterns]


_VISION_CUE_PATTERNS: List[re.Pattern[str]] = _build_vision_cue_patterns()
_VISION_COMPOUND_RE: re.Pattern[str] = re.compile(
    r"\b(?:%s)s?\b" % "|".join(
        re.escape(compound) for compound in sorted(_VISION_NON_VISUAL_COMPOUNDS)
    )
)


def _detect_structured_output(lower: str) -> bool:
    return any(m in lower for m in _STRUCTURED_OUTPUT_MARKERS)


def _detect_tools(lower: str) -> bool:
    """Bidirectional, asymmetric on purpose — see `extract`'s docstring.

    Order matters: action evidence outranks question phrasing, so "read the
    file and summarise it" is still a tool turn. Only a turn that is purely
    explanatory or interrogative, with no action verb and nothing that names a
    file or path, flips the fail-closed default to False.
    """
    if any(m in lower for m in _TOOL_MARKERS):
        return True
    if _is_pure_question(lower) and not _mentions_path(lower):
        return False
    return _TOOLS_DEFAULT


def _is_pure_question(lower: str) -> bool:
    return any(m in lower for m in _QUESTION_MARKERS)


def _mentions_path(lower: str) -> bool:
    if any(w in lower for w in _PATH_WORD_MARKERS):
        return True
    return _PATH_LIKE_RE.search(lower) is not None


_ATTACHMENT_MARKER_RES: Dict[str, re.Pattern[str]] = {
    kind: _compile_marker_re(markers)
    for kind, markers in _ATTACHMENT_MARKERS.items()
}


def _infer_attachment_kinds(lower: str) -> List[str]:
    kinds = {
        kind
        for kind, pattern in _ATTACHMENT_MARKER_RES.items()
        if pattern.search(lower)
    }
    return sorted(kinds)
