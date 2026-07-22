"""Decision cache + session model-floor pin.

Exact-hash cache: normalize whitespace/case → hash → cached tier.
Session pin: per-session model-floor, upward-only ratchet.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional


def normalize(task: str) -> str:
    """Normalize task text for exact-hash caching."""
    return " ".join(task.lower().split())


def hash_task(task: str) -> str:
    """Return a stable hash for a normalized task."""
    norm = normalize(task)
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


class Cache:
    """Exact-hash tier cache."""

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}

    def get(self, task: str) -> Optional[Dict[str, Any]]:
        """Return cached result for task, or None."""
        key = hash_task(task)
        return self._store.get(key)

    def set(self, task: str, result: Dict[str, Any]) -> None:
        """Cache a classification result."""
        key = hash_task(task)
        self._store[key] = dict(result)

    def size(self) -> int:
        return len(self._store)


class SessionPin:
    """Per-session model-floor pin, upward-only ratchet.

    Once set to a tier, only explicit hard-verb signals (Stage 0)
    may break it upward. Never downgrades.
    """

    _TIER_ORDER = {"T1": 0, "T2": 1, "T3": 2, "T4": 3}

    def __init__(self) -> None:
        self._floor: Optional[int] = None
        self._tier: Optional[str] = None

    @property
    def tier(self) -> Optional[str]:
        return self._tier

    def set(self, tier: str) -> None:
        """Set the model floor. Only increases."""
        level = self._TIER_ORDER.get(tier)
        if level is None:
            return
        if self._floor is None or level > self._floor:
            self._floor = level
            self._tier = tier

    def is_set(self) -> bool:
        return self._tier is not None

    def reset(self) -> None:
        self._floor = None
        self._tier = None
