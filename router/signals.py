"""Signal extraction — compute a flat feature vector from a task turn.

No IO, no state, no model calls. Deterministic, depth ≤ 1.
"""

from __future__ import annotations

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
    """
    lower = turn.lower()
    lines = turn.split("\n")

    return {
        "char_len": len(turn),
        "has_code": _detect_code(lower),
        "size_lines": _infer_line_count(lower),
        "num_files": _infer_file_count(turn),
        "has_stacktrace": _detect_stacktrace(turn),
        "num_requirements": _count_requirements(lines),
        "verb_class": _classify_verb(lower),
        "lang": _detect_language(lower),
        "keywords": _keyword_hits(lower),
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
