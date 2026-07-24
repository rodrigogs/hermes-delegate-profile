"""Pure dynamic compaction-threshold calculations.

The curve follows the operational implication of Vectara's context-engineering
research: larger advertised context windows still have performance, cost, and
reliability trade-offs, so compaction should be calibrated per model window.
Source: https://www.vectara.com/blog/context-engineering-can-you-trust-long-context
"""

from __future__ import annotations

import math
from collections.abc import Iterable

PRESETS = {
    "Max-context": 0,
    "Conservative": 25,
    "Balanced": 50,
    "Aggressive": 100,
}


def p_base(window_tokens: int) -> float:
    """Return the base compaction fraction for a model context window."""
    return 0.85 - 0.0776 * math.log2(window_tokens / 128000)


def delta(aggressiveness: int | float) -> float:
    """Return the user-selected adjustment for aggressiveness in the 0..100 range."""
    return 0.10 - 0.002 * aggressiveness


def p_eff(window_tokens: int, aggressiveness: int | float) -> float:
    """Return a clamped, small-window-safe compaction fraction rounded to 3 decimals."""
    threshold = p_base(window_tokens) + delta(aggressiveness)
    threshold = max(0.55, min(0.90, threshold))
    if window_tokens < 512000:
        threshold = max(threshold, 0.75)
    return round(threshold, 3)


def compute_model_thresholds(
    models: Iterable[tuple[str, int]], aggressiveness: int | float
) -> dict[str, float]:
    """Map model substring keys to their calibrated compaction fractions."""
    return {key: p_eff(window_tokens, aggressiveness) for key, window_tokens in models}


def summarizer_cap(
    summarizer_window_tokens: int,
    target_ratio: float = 0.6,
    overhead: int = 8000,
    out_cap: int = 12000,
    head: int = 8000,
) -> int:
    """Return the largest source context that fits the summarizer budget."""
    return int(
        (summarizer_window_tokens - overhead - out_cap + head) / (1 - target_ratio)
    )


def apply_dynamic_thresholds(
    config: dict,
    aggressiveness: int,
    summarizer_window: int,
    model_windows: dict,
) -> dict:
    """Return a copied config with dynamic compression thresholds applied.

    The caller's configuration is never mutated. ``model_windows`` maps the
    substring keys understood by Hermes's core ``model_thresholds`` resolver
    to each main model's advertised context window.
    """
    import copy

    cfg = copy.deepcopy(config)
    compression = cfg.setdefault("compression", {})
    compression["aggressiveness"] = aggressiveness
    compression["model_thresholds"] = compute_model_thresholds(
        model_windows.items(), aggressiveness
    )
    compression["threshold_tokens"] = summarizer_cap(summarizer_window)
    return cfg
