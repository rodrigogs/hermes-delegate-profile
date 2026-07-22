"""Classifier — the gated LLM call for difficulty classification.

Stage 1: fires ONLY on uncertainty (when Stage 0 falls through to
action:classify). Fresh temp-0, token-capped, hard-timeout one-shot
on a trusted-streaming provider (glm-5.2/zai).

v1: the classifer interface. The actual model call is injected by the
adapter (the only Hermes-coupled code). Pure core tests inject a mock.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Classifier rubric — 4 discrete anchored tiers (never a numeric scale)
# ---------------------------------------------------------------------------

TIER_ANCHORS = {
    "T1": "TRIVIAL — single mechanical edit, no reasoning (rename, format, typo)",
    "T2": "SIMPLE — one well-specified file, standard pattern, boilerplate",
    "T3": "MODERATE — bounded multi-step, 2-5 files, some design choice",
    "T4": "HARD — cross-cutting, unknown-cause debug, correctness/concurrency/security/ambiguity, novel design",
}

# Tier → model/provider mapping (from router.yaml tiers)
DEFAULT_TIERS: Dict[str, Dict[str, str]] = {
    "T1": {"model": "glm-5.2-fast", "provider": "zai"},
    "T2": {"model": "glm-5.2", "provider": "zai"},
    "T3": {"model": "claude-sonnet", "provider": "anthropic"},
    "T4": {"model": "claude-opus", "provider": "anthropic"},
}

# Upward ratchet: when confidence is low or boundary straddle, bump up
_UPWARD_RATCHET = {"T1": "T2", "T2": "T3", "T3": "T4", "T4": "T4"}


class Classifier:
    """Gated difficulty classifier.

    The actual LLM call is injected via `classify_fn` — this class only
    holds the rubric, anchors, and safety logic. The adapter wires the
    real model call.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        anchors: Optional[List[Dict[str, Any]]] = None,
    ):
        cls_conf = config.get("classifier", {})
        self.model: str = cls_conf.get("model", "glm-5.2")
        self.provider: str = cls_conf.get("provider", "zai")
        self.temperature: float = float(cls_conf.get("temperature", 0))
        self.max_tokens: int = int(cls_conf.get("max_tokens", 128))
        self.timeout_seconds: int = int(cls_conf.get("timeout_seconds", 8))
        self._anchors = anchors or []
        self._tiers = config.get("tiers", DEFAULT_TIERS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_prompt(self, task: str, features: Dict[str, Any]) -> str:
        """Build the one-shot classifier prompt with anchors and context."""
        lines = [
            "You are a task difficulty classifier. Respond with a single JSON object.",
            "",
            "Tiers:",
            "  T1 (TRIVIAL): single mechanical edit, no reasoning — rename, format, typo.",
            "  T2 (SIMPLE): one well-specified file, standard pattern, boilerplate.",
            "  T3 (MODERATE): bounded multi-step, 2-5 files, some design choice.",
            "  T4 (HARD): cross-cutting, unknown-cause debug, correctness/concurrency/",
            "      security/ambiguity, novel design.",
            "",
        ]

        # Include few-shot anchors if present
        if self._anchors:
            lines.append("Examples:")
            for a in self._anchors:
                lines.append(f"  Task: \"{a['description']}\"")
                exp = a.get("expected", {})
                lines.append(f"  Tier: {exp.get('tier', '?')} "
                           f"({exp.get('needs_capability', '')})")
            lines.append("")

        lines.extend([
            "Context:",
            f"  verb_class: {features.get('verb_class', 'unknown')}",
            f"  has_code: {features.get('has_code', False)}",
            f"  size_lines: {features.get('size_lines', 0)}",
            f"  num_files: {features.get('num_files', 0)}",
            f"  has_stacktrace: {features.get('has_stacktrace', False)}",
            f"  num_requirements: {features.get('num_requirements', 0)}",
            f"  lang: {features.get('lang', '')}",
            "",
            f"Task: \"{task}\"",
            "",
            'Respond: {"signals":"1-2 sentences","tier":"T1|T2|T3|T4",'
            '"confidence":"high|med|low","needs_capability":"one clause"}',
        ])
        return "\n".join(lines)

    def safety_ratchet(
        self,
        tier: str,
        confidence: str,
    ) -> Tuple[str, Dict[str, str]]:
        """Apply upward-only safety ratchet.

        Low confidence or boundary straddle → bump up one tier.
        Returns (final_tier, {model, provider}).
        """
        if tier not in _UPWARD_RATCHET:
            tier = "T4"  # unknown → strongest

        if confidence == "low":
            tier = _UPWARD_RATCHET.get(tier, "T4")

        tier_cfg = self._tiers.get(tier, self._tiers.get("T4", {}))
        return tier, dict(tier_cfg)

    def tiers(self) -> Dict[str, Dict[str, str]]:
        """Return the configured tiers."""
        return dict(self._tiers)

    def anchors(self) -> List[Dict[str, Any]]:
        """Return the few-shot anchors."""
        return list(self._anchors)


def build_prompt_from_config(
    config: Dict[str, Any],
    task: str,
    features: Dict[str, Any],
    anchors: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Convenience: build classifier prompt from config dict."""
    c = Classifier(config, anchors)
    return c.build_prompt(task, features)
