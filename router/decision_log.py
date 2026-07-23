"""Decision log — one greppable cause= line per turn.

Closed cause set — the only valid strings:
  blocklist_veto, breaker_cooldown, keyword_match, size_rule,
  has_code_rule, hard_rule, classifier, session_pin, default_fallthrough,
  fail_safe_strong
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# Closed set — the only valid cause values
VALID_CAUSES: set[str] = {
    "blocklist_veto",
    "breaker_cooldown",
    "keyword_match",
    "size_rule",
    "has_code_rule",
    "hard_rule",
    "classifier",
    "session_pin",
    "default_fallthrough",
    "fail_safe_strong",
}


class DecisionLog:
    """Append-only decision log for greppable cause= tracing."""

    def __init__(self) -> None:
        self._entries: List[Dict[str, Any]] = []

    def record(
        self,
        cause: str,
        output: Dict[str, Any],
        matched_rule_id: Optional[str] = None,
        task_preview: str = "",
    ) -> None:
        """Record a routing decision."""
        if cause not in VALID_CAUSES:
            cause = "fail_safe_strong"

        self._entries.append({
            "ts": time.time(),
            "cause": cause,
            "output": dict(output),
            "rule_id": matched_rule_id,
            "task": task_preview[:120],
        })

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        """Return the last N entries."""
        return self._entries[-n:]

    def format_line(self, entry: Dict[str, Any]) -> str:
        """Format one entry as a greppable line."""
        ts = entry.get("ts", 0)
        cause = entry.get("cause", "?")
        rule = entry.get("rule_id") or "-"
        out = entry.get("output", {})
        profile = out.get("profile", "")
        model = out.get("model", "")
        task = entry.get("task", "")
        return (
            f"cause={cause} rule={rule} profile={profile} "
            f"model={model} task=\"{task}\""
        )

    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)
