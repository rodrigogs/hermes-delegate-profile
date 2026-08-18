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
#   extension, a screenshot, a design-tool artefact). The noun alone fires.
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
_VISION_AMBIGUOUS_NOUNS: frozenset[str] = frozenset({
    "chart", "diagram", "image", "design", "plot",
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

# Markers are deliberately narrow substrings: bare "log" would fire on
# "logic"/"login"/"logging" and bare "diff" would fire on "different", so the
# ambiguous kinds are keyed on file extensions and multi-word phrases only.
_ATTACHMENT_MARKERS: Dict[str, frozenset[str]] = {
    "image": frozenset({
        "screenshot", "image", ".png", "png", ".jpg", "jpg", "jpeg",
        ".webp", "webp", ".gif", ".svg",
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
    at this chart" supplies one. See the marker tables for the two tiers.
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
    markers = [
        "traceback", "stack trace", "exception:", "error:",
        "panic:", "segfault", "segmentation fault", "null pointer",
        "index out of", "key error", "type error", "attribute error",
        "syntax error", " runtime error",
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
    review_words = {"review", "audit", "inspect", "check", "assess"}
    hits = [w for w in review_words if w in lower]
    return hits


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


def _detect_vision(lower: str) -> bool:
    """True when the turn implies visual INPUT, not merely a visual noun.

    Tier 1 is unambiguous (an image extension, a screenshot, a design-tool
    artefact) and fires on the marker alone. Tier 2 is the ambiguous nouns,
    which fire only next to an attachment or deictic cue — a chart the turn
    asks the model to *draw* is not a chart the model has to *see*, and
    conflating the two strands text-only work on the single vision rail.
    """
    if any(m in lower for m in _VISION_MARKERS):
        return True
    return _has_visual_input_cue(lower)


def _has_visual_input_cue(lower: str) -> bool:
    """Proximity test: an ambiguous visual noun next to an input cue."""
    for pattern in _VISION_CUE_PATTERNS:
        if pattern.search(lower):
            return True
    return False


def _build_vision_cue_patterns() -> List[re.Pattern[str]]:
    """Compile the proximity patterns once, from the marker tables above.

    Built rather than written out so the noun list and the cue lists stay the
    single source of truth: adding a noun cannot forget a pattern.
    """
    nouns = r"(?:%s)s?\b" % "|".join(sorted(_VISION_AMBIGUOUS_NOUNS))
    gap_one = r"(?:\s+[\w.\-]+){0,1}\s+"   # deictic cues stay close to the noun
    gap_two = r"(?:\s+[\w.\-]+){0,2}\s+"   # attachment cues take more qualifiers
    patterns = [
        # "this chart", "this flow chart", "the following diagram"
        r"\b(?:%s)%s%s" % ("|".join(sorted(_VISION_DEICTIC_CUES)), gap_one, nouns),
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


def _infer_attachment_kinds(lower: str) -> List[str]:
    kinds = {
        kind
        for kind, markers in _ATTACHMENT_MARKERS.items()
        if any(m in lower for m in markers)
    }
    return sorted(kinds)
