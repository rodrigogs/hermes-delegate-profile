"""Blocklist pre-filter — the first stage of the routing pipeline.

Owns the only mutable ban state. Unions operator manual bans with
(deferred) auto-breaker cooldowns into a single boolean `blocked_model`.
The pure rule engine only reads this boolean — never writes state.

v1: static manual deny row + fallback chain. Auto-breaker deferred.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class Blocklist:
    """Fail-closed blocklist with manual bans and fallback chain.

    The config deny rows fire independently of any mutable state file.
    If breaker state is missing/corrupt, cooldowns are treated as empty —
    but config deny rows still fire. The blocklist never fails open.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self._manual_bans: List[Dict[str, str]] = []
        self._fallback_chain: List[str] = []
        self._auto_breaker_enabled = False

        bl_conf = config.get("blocklist", {})
        self._manual_bans = bl_conf.get("manual_ban", [])
        self._fallback_chain = bl_conf.get("fallback_chain", [])

        ab = bl_conf.get("auto_breaker", {})
        self._auto_breaker_enabled = ab.get("enabled", False) if isinstance(ab, dict) else False

        # Deferred mutable state — empty in v1
        self._cooldowns: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_blocked(self, model: Optional[str], provider: Optional[str]) -> bool:
        """Return True if (model, provider) is blocked.

        Checks manual bans, then cooldowns (v1: empty). Fail-closed.
        """
        if not model:
            return False

        # Check manual bans — these always fire
        for ban in self._manual_bans:
            ban_model = ban.get("model", "")
            ban_provider = ban.get("provider", "")
            if self._match(ban_model, ban_provider, model, provider or ""):
                return True

        # Check cooldowns (v1: always empty)
        if self._auto_breaker_enabled:
            key = f"{model}:{provider}"
            if key in self._cooldowns:
                return True

        return False

    def fallback_for(self, model: str) -> Optional[str]:
        """Return the next model in the fallback chain, or None."""
        try:
            idx = self._fallback_chain.index(model)
            if idx + 1 < len(self._fallback_chain):
                return self._fallback_chain[idx + 1]
        except ValueError:
            pass
        return None

    def manual_bans(self) -> List[Dict[str, str]]:
        """Return the current manual ban list (for CLI display)."""
        return list(self._manual_bans)

    def fallback_chain(self) -> List[str]:
        """Return the current fallback chain (for CLI display)."""
        return list(self._fallback_chain)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _match(ban_model: str, ban_provider: str, model: str, provider: str) -> bool:
        """Check if model/provider matches a ban entry.

        If the model matches the ban, block regardless of provider
        (fail-closed — a banned model is banned).
        An empty ban provider matches any provider.
        """
        model_match = not ban_model or ban_model.lower() == model.lower()
        if not model_match:
            return False
        # Model matches — block unless provider is specifically non-matching
        if not ban_provider:
            return True  # ban all providers for this model
        if not provider:
            return True  # fail-closed: if model banned anywhere, block it
        return ban_provider.lower() == provider.lower()
