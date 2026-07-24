"""Auto-Breaker — circuit breaker for model stall detection.

Pure state machine: no IO, no filesystem, no clock. Clock is injected as
``timestamp`` argument. State persistence (load/save JSON) is handled by
the caller (Blocklist).

Three states:
  CLOSED    — healthy, counting failures in a sliding window
  OPEN      — tripped, blocking all calls during cooldown
  HALF_OPEN — cooldown expired, allowing exactly one probe call

Design: (state, event, timestamp) → (new_state, blocked_set) reducer.
Config deny rows fire independently — breaker only ADDS blocks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Failure weighting — higher weight = more definitive stall signal
# ---------------------------------------------------------------------------

FAILURE_WEIGHTS: Dict[str, int] = {
    "quota_exhausted": 10,  # provider has no usable quota — trip immediately
    "ttfb_stall": 3,       # model accepts connection but never streams — definitive
    "idle_stall": 2,       # started streaming, then went silent — strong signal
    "hard_timeout": 1,     # wall-clock expired — could be slow, could be broken
    "crash": 1,            # process crash (SIGSEGV/SIGABRT)
    "crash_or_oom": 1,     # OOM-killed or external kill
    "nonzero_exit": 1,     # app-level error — least definitive
}

DEFAULT_WEIGHT = 1


class BreakerState:
    """Circuit breaker state machine for model stall detection.

    Three states: CLOSED (healthy) → OPEN (tripped) → HALF_OPEN (probing).
    Sliding-window failure counting with exponential cooldown backoff.

    This is the pure reducer: (state, event) → (new_state, blocked_set).
    State persistence (load/save JSON) is handled by the caller.

    Config keys (from router.yaml auto_breaker section):
        threshold:              total failure weight to trip (default 5)
        window_seconds:         sliding window duration (default 600 = 10 min)
        base_cooldown_seconds:  first-trip cooldown (default 60)
        max_cooldown_seconds:   cooldown cap (default 900 = 15 min)
        backoff_multiplier:     multiply cooldown on re-trip (default 2.0)
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._threshold: int = int(config.get("threshold", 5))
        self._window_s: float = float(config.get("window_seconds", 600))
        self._base_cooldown_s: float = float(config.get("base_cooldown_seconds", 60))
        self._max_cooldown_s: float = float(config.get("max_cooldown_seconds", 900))
        self._backoff_mult: float = float(config.get("backoff_multiplier", 2.0))

        # entries: model_key → _Entry
        self._entries: Dict[str, _Entry] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        model_key: str,
        failure_kind: str,
        timestamp: float,
    ) -> bool:
        """Record a failure event. Returns True if breaker just tripped."""
        weight = FAILURE_WEIGHTS.get(failure_kind, DEFAULT_WEIGHT)
        entry = self._get_or_create(model_key)

        # Prune events outside sliding window, then add new event
        entry.prune(timestamp, self._window_s)
        entry.events.append(_Event(kind=failure_kind, ts=timestamp, weight=weight))

        # Sum weights in window
        total = entry.total_weight()

        if entry.state == "CLOSED":
            if total >= self._threshold:
                return self._trip(entry, timestamp)
        elif entry.state == "HALF_OPEN":
            # HALF_OPEN probe failed — back to OPEN, extend cooldown
            return self._trip(entry, timestamp)
        # OPEN: already tripped, just record the event
        return False

    def record_success(self, model_key: str, timestamp: float) -> None:
        """Record success — resets breaker if in HALF_OPEN."""
        entry = self._entries.get(model_key)
        if entry is not None and entry.state == "HALF_OPEN":
            self._reset(entry)
        # In CLOSED, success doesn't reset counter — only window expiry does.
        # In OPEN, success can't happen (calls are blocked).

    def is_blocked(self, model_key: str, timestamp: float) -> bool:
        """Check if model is currently blocked (OPEN and within cooldown).

        Side effect: if cooldown has expired, transitions OPEN → HALF_OPEN.
        """
        entry = self._entries.get(model_key)
        if entry is None:
            return False

        if entry.state == "CLOSED":
            return False

        if entry.state == "OPEN":
            if timestamp >= entry.cooldown_until:
                # Cooldown expired — allow one probe
                entry.state = "HALF_OPEN"
                entry.probe_allowed = True
                return False  # not blocked during HALF_OPEN
            return True  # still in cooldown

        # HALF_OPEN: not blocked (probe was already allowed)
        return False

    def blocked_entries(self, timestamp: float) -> List[Dict[str, Any]]:
        """Return currently-blocked entries for CLI display."""
        result: List[Dict[str, Any]] = []
        for key, entry in self._entries.items():
            # Call is_blocked to trigger any pending state transitions
            blocked = self.is_blocked(key, timestamp)
            if entry.state == "OPEN" or blocked:
                remaining = max(0.0, entry.cooldown_until - timestamp)
                result.append({
                    "model_key": key,
                    "state": entry.state,
                    "cooldown_remaining_s": remaining,
                    "backoff_seconds": entry.backoff_seconds,
                    "last_failure_kind": entry.last_failure_kind,
                    "failure_count": entry.total_weight(),
                })
        return result

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state for persistence."""
        return {
            "version": 1,
            "entries": {
                key: entry.to_dict()
                for key, entry in self._entries.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        config: Dict[str, Any],
    ) -> "BreakerState":
        """Deserialize state from persistence."""
        breaker = cls(config)
        if not isinstance(data, dict):
            return breaker
        if data.get("version") != 1:
            return breaker
        raw_entries = data.get("entries", {})
        if not isinstance(raw_entries, dict):
            return breaker
        for key, raw in raw_entries.items():
            entry = _Entry.from_dict(raw)
            if entry is not None:
                breaker._entries[key] = entry
        return breaker

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, model_key: str) -> "_Entry":
        if model_key not in self._entries:
            self._entries[model_key] = _Entry()
        return self._entries[model_key]

    def _trip(self, entry: "_Entry", timestamp: float) -> bool:
        """Trip the breaker: CLOSED→OPEN or HALF_OPEN→OPEN."""
        was_already_open = entry.state == "OPEN"

        if was_already_open or entry.state == "HALF_OPEN":
            # Re-trip: extend backoff exponentially
            entry.backoff_seconds = min(
                entry.backoff_seconds * self._backoff_mult,
                self._max_cooldown_s,
            )
        else:
            # First trip: base cooldown
            entry.backoff_seconds = self._base_cooldown_s

        entry.state = "OPEN"
        entry.cooldown_until = timestamp + entry.backoff_seconds
        entry.probe_allowed = False
        return True

    @staticmethod
    def _reset(entry: "_Entry") -> None:
        """Reset breaker to CLOSED (successful probe)."""
        entry.state = "CLOSED"
        entry.events.clear()
        entry.cooldown_until = 0.0
        entry.backoff_seconds = 0.0
        entry.probe_allowed = False


# ---------------------------------------------------------------------------
# Internal data classes
# ---------------------------------------------------------------------------

class _Event:
    """A single failure event in the sliding window."""

    __slots__ = ("kind", "ts", "weight")

    def __init__(self, kind: str, ts: float, weight: int) -> None:
        self.kind = kind
        self.ts = ts
        self.weight = weight

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ts": self.ts, "weight": self.weight}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["_Event"]:
        try:
            kind = str(data.get("kind", ""))
            if not kind:
                return None
            return cls(
                kind=kind,
                ts=float(data.get("ts", 0)),
                weight=int(data.get("weight", DEFAULT_WEIGHT)),
            )
        except (TypeError, ValueError):
            return None


class _Entry:
    """Per-model breaker state."""

    __slots__ = (
        "state",
        "events",
        "cooldown_until",
        "backoff_seconds",
        "last_failure_kind",
        "probe_allowed",
    )

    def __init__(self) -> None:
        self.state: str = "CLOSED"
        self.events: List[_Event] = []
        self.cooldown_until: float = 0.0
        self.backoff_seconds: float = 0.0
        self.last_failure_kind: str = ""
        self.probe_allowed: bool = False

    def prune(self, now: float, window_s: float) -> None:
        """Remove events older than the sliding window."""
        cutoff = now - window_s
        self.events = [e for e in self.events if e.ts > cutoff]

    def total_weight(self) -> int:
        return sum(e.weight for e in self.events)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "failure_events": [e.to_dict() for e in self.events],
            "cooldown_until": self.cooldown_until,
            "backoff_seconds": self.backoff_seconds,
            "last_failure_kind": self.last_failure_kind,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["_Entry"]:
        if not isinstance(data, dict):
            return None
        entry = cls()
        state = data.get("state", "CLOSED")
        if state in ("CLOSED", "OPEN", "HALF_OPEN"):
            entry.state = state
        for raw in data.get("failure_events", []) or []:
            ev = _Event.from_dict(raw)
            if ev is not None:
                entry.events.append(ev)
        entry.cooldown_until = float(data.get("cooldown_until", 0.0))
        entry.backoff_seconds = float(data.get("backoff_seconds", 0.0))
        entry.last_failure_kind = str(data.get("last_failure_kind", ""))
        return entry
