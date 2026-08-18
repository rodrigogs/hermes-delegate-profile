"""Decision log — one greppable cause= line per turn.

Pure in-memory bookkeeping: no IO, no model calls. (The durable subclass adds
the only IO, and guards it.)

Closed cause set — the only valid strings:
  blocklist_veto, breaker_cooldown, keyword_match, size_rule,
  has_code_rule, hard_rule, classifier, session_pin, default_fallthrough,
  fail_safe_strong

Chain-plan persistence (additive, backward compatible):
  ``record(..., chain_plan=...)`` attaches the capability/fallback chain plan
  produced by ``rules.plan_chain`` so a trace can be replayed and audited —
  which elos survived the capability filter, which were rejected and why, and
  in what order they will be tried. Two properties are load-ballasting here:
    * OLD entries have no ``chain_plan`` key at all — the box has live traces in
      routes.jsonl written by the current code, and a reader that raises on them
      takes the console down. :func:`chain_plan_of` therefore returns an empty
      default for missing AND for corrupt values, and never raises.
    * routes.jsonl is size-bounded and rotated, so the entry is bounded:
      ``rejected`` is truncated to :data:`MAX_REJECTED_ENTRIES` and the number
      of dropped rows is reported as ``rejected_truncated``.
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

# ---------------------------------------------------------------------------
# Chain plan — bounded shape + defensive parse
# ---------------------------------------------------------------------------

# Hard cap on persisted ``rejected`` rows. routes.jsonl is size-bounded and
# rotated, so one pathological chain must not evict everybody else's traces.
MAX_REJECTED_ENTRIES = 8

# The persisted chain-plan keys: rules.plan_chain()'s return shape plus the
# truncation counter added here. Built by a factory, never a shared constant —
# the values are mutable and callers are free to mutate what they get back.
CHAIN_PLAN_KEYS: frozenset = frozenset({
    "chain", "requirements", "rejected", "unknown",
    "bypassed", "strategy", "independent_rails", "rejected_truncated",
})


def empty_chain_plan() -> Dict[str, Any]:
    """A fresh, mutable empty chain plan — the default for OLD/corrupt entries."""
    return {
        "chain": [],
        "requirements": {},
        "rejected": [],
        "unknown": [],
        "bypassed": False,
        "strategy": "sequential",
        "independent_rails": 0,
        "rejected_truncated": 0,
    }


def bound_chain_plan(plan: Any) -> Optional[Dict[str, Any]]:
    """Return a bounded, JSON-safe copy of ``plan``, or None to skip it.

    None means "do not record a chain_plan key" — a non-mapping plan is a
    programming error upstream, and a trace entry is never worth raising over.
    ``rejected`` is truncated to :data:`MAX_REJECTED_ENTRIES`; the count of
    dropped rows lands in ``rejected_truncated`` (0 when nothing was dropped, so
    consumers never have to branch on the key's presence).
    """
    if not isinstance(plan, dict):
        return None

    bounded: Dict[str, Any] = dict(plan)

    rejected = plan.get("rejected")
    if not isinstance(rejected, list):
        # Corrupt/absent rejected list degrades to empty rather than raising.
        bounded["rejected"] = []
        bounded["rejected_truncated"] = 0
        return bounded

    dropped = max(0, len(rejected) - MAX_REJECTED_ENTRIES)
    bounded["rejected"] = list(rejected[:MAX_REJECTED_ENTRIES])
    bounded["rejected_truncated"] = dropped
    return bounded


def chain_plan_of(entry: Any) -> Dict[str, Any]:
    """Read the chain plan out of a recorded entry, defensively.

    Contract (all three branches are exercised by live data on the box):
      * entry written before this feature -> no ``chain_plan`` key -> empty default
      * ``chain_plan`` present but corrupt (string, list, null) -> empty default
      * ``chain_plan`` present and a mapping -> merged over the empty default,
        with each field type-checked individually so ONE bad field cannot
        discard the rest of the plan.
    Never raises.
    """
    plan = empty_chain_plan()
    if not isinstance(entry, dict):
        return plan
    raw = entry.get("chain_plan")
    if not isinstance(raw, dict):
        return plan  # missing (old entry) or corrupt value -> skipped

    for key in ("chain", "rejected", "unknown"):
        value = raw.get(key)
        if isinstance(value, list):
            plan[key] = list(value)
    requirements = raw.get("requirements")
    if isinstance(requirements, dict):
        plan["requirements"] = dict(requirements)
    if isinstance(raw.get("bypassed"), bool):
        plan["bypassed"] = raw["bypassed"]
    strategy = raw.get("strategy")
    if isinstance(strategy, str) and strategy:
        plan["strategy"] = strategy
    for key in ("independent_rails", "rejected_truncated"):
        value = raw.get(key)
        # bool is an int subclass — a boolean here is corrupt, not a count.
        if isinstance(value, int) and not isinstance(value, bool):
            plan[key] = value
    return plan


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
        *,
        steps: Optional[List[Dict[str, Any]]] = None,
        chain_plan: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a routing decision.

        ``steps`` is an optional per-stage in/out trace (``[{stage, in, out,
        cause}, ...]``) for visual replay. It is purely additive: when omitted
        the recorded entry keeps its historical shape (no ``steps`` key), so
        existing consumers and persisted logs are unchanged.

        ``chain_plan`` is the ``rules.plan_chain`` result (eligible order,
        rejected+reasons, derived requirements, strategy, independent_rails).
        Also purely additive and bounded: omitted -> no key; a non-mapping value
        is skipped rather than raised; ``rejected`` is truncated to
        :data:`MAX_REJECTED_ENTRIES` with a ``rejected_truncated`` count.
        """
        if cause not in VALID_CAUSES:
            cause = "fail_safe_strong"

        entry: Dict[str, Any] = {
            "ts": time.time(),
            "cause": cause,
            "output": dict(output),
            "rule_id": matched_rule_id,
            "task": task_preview[:120],
        }
        if steps is not None:
            entry["steps"] = steps
        if chain_plan is not None:
            bounded = bound_chain_plan(chain_plan)
            if bounded is not None:
                entry["chain_plan"] = bounded
        self._entries.append(entry)

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        """Return the last N entries."""
        return self._entries[-n:]

    def format_line(self, entry: Dict[str, Any]) -> str:
        """Format one entry as a greppable line.

        Entries WITHOUT a chain plan (every historical entry) format exactly as
        before — the chain segment is only appended when a plan is present.
        """
        ts = entry.get("ts", 0)
        cause = entry.get("cause", "?")
        rule = entry.get("rule_id") or "-"
        out = entry.get("output", {})
        profile = out.get("profile", "")
        model = out.get("model", "")
        task = entry.get("task", "")
        chain_seg = ""
        if isinstance(entry.get("chain_plan"), dict):
            plan = chain_plan_of(entry)
            rejected_total = len(plan["rejected"]) + plan["rejected_truncated"]
            chain_seg = (
                f"chain={len(plan['chain'])} rejected={rejected_total} "
                f"strategy={plan['strategy']} rails={plan['independent_rails']} "
            )
        return (
            f"cause={cause} rule={rule} profile={profile} "
            f"model={model} {chain_seg}task=\"{task}\""
        )

    def chain_plan(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Chain plan of ``entry``, or the empty default for old/corrupt ones."""
        return chain_plan_of(entry)

    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)
