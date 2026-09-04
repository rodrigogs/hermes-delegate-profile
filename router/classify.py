"""Classifier — the gated LLM call for difficulty classification.

Stage 1: fires ONLY on uncertainty (when Stage 0 falls through to
action:classify). Fresh temp-0, token-capped, hard-timeout one-shot
on a trusted-streaming provider (glm-5.3-flash/zai).

v1: the classifer interface. The actual model call is injected by the
adapter (the only Hermes-coupled code). Pure core tests inject a mock.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from . import capabilities as _caps
from .signals import chars_per_token

# ---------------------------------------------------------------------------
# How much of the task text may reach the classifier
#
# WHAT WENT IN BEFORE: the whole thing. `build_prompt` ended with
# f'Task: "{task}"' and nothing anywhere bounded `task`. On the kanban path that
# is a card body, which is usually short — but "usually short" is not a bound,
# and a pasted stacktrace or a long chat turn is neither.
#
# WHY A SMALL CAP LOSES NOTHING, which is the whole argument: the prompt already
# carries the SCALE of the job as features — size_lines, num_files,
# has_stacktrace, num_requirements, and the rule layer's own est_input_tokens.
# The task TEXT is there for the job's NATURE, not its size. So truncating the
# text cannot hide a big job from the classifier; the features say how big it is
# and the anchors say what each tier looks like. A difficulty verdict of
# T1..T4 does not improve by reading the 40th kilobyte.
#
# THE ARITHMETIC, so the number is not a vibe: 4000 chars is ~1111 tokens at the
# ratio signals.py records (3.6). The SMALLEST context window in the whole
# capability registry is glm-4.5v at 65,536 tokens — measured, all 43 entries
# declare one — so this budget is under 2% of the tightest catalogued model and
# the window can never be the binding constraint for a model the catalogue
# knows. The fixed rubric-and-features preamble measures 675 chars.
#
# It is still clamped by the window below, because an operator may declare a
# classifier whose window is genuinely small, and that clamp is reachable
# configuration rather than decoration.
_TASK_CHAR_BUDGET: int = 4000


# ---------------------------------------------------------------------------
# Classifier rubric — 4 discrete anchored tiers (never a numeric scale)
# ---------------------------------------------------------------------------

TIER_ANCHORS = {
    "T1": "TRIVIAL — single mechanical edit, no reasoning (rename, format, typo)",
    "T2": "SIMPLE — one well-specified file, standard pattern, boilerplate",
    "T3": "MODERATE — bounded multi-step, 2-5 files, some design choice",
    "T4": "HARD — cross-cutting, unknown-cause debug, correctness/concurrency/security/ambiguity, novel design",
}

# Tier → model/provider mapping comes from router.yaml's ``tiers`` table and
# is passed in via config. There is deliberately NO hardcoded fallback here
# anymore: the one that shipped pointed at glm-5.2-fast / glm-5.2 /
# claude-sonnet / claude-opus — none of them among the 8 live links on the
# reference install (measured 2026-08-19) — so a config without ``tiers``
# would route the classifier's answer onto rails that do not exist. An empty
# table degrades honestly: safety_ratchet returns an empty tier config, the
# adapter materialises no model, and the delegation runs under the caller's
# default instead of a stale lie.

#: Defaults for the ``classifier:`` block — the ONE place they are written.
#:
#: They used to be written twice, and the two disagreed on the thing that matters
#: most: this module defaulted to ``glm-5.3-flash`` while the code that ACTUALLY
#: DISPATCHES (``_make_classify_fn`` in the plugin) defaulted to ``glm-5.2``.
#: Commit bdb92f6 ("z.ai sempre glm-5.3-flash — nunca glm-5.2 nem glm-5.3
#: normal") corrected this module and missed that one, so the golden rule it
#: established was violated by the only copy that runs.
#:
#: That is not a cosmetic mismatch. ``glm-5.2`` is registry-marked "plan
#: auto-routes this id to glm-5.3": on a Coding Plan key the request SUCCEEDS,
#: the plan bills the substitute, and every trace, log and console row names the
#: id nobody ran — the exact defect ``MODEL_CAPABILITIES``' docstring exists to
#: prevent and that ``tests/test_shipped_policy_names_real_rails.py`` refuses in
#: the policy. It was reachable whenever ``classifier.model`` was absent from
#: router.yaml.
#:
#: Read through :func:`classifier_defaults` so a caller cannot mutate the table.
CLASSIFIER_DEFAULTS = {
    "model": "glm-5.3-flash",
    "provider": "zai",
    "temperature": 0,
    "max_tokens": 128,
    "timeout_seconds": 8,
}


def classifier_defaults() -> Dict[str, Any]:
    """A copy of :data:`CLASSIFIER_DEFAULTS`, so no caller can edit the table."""
    return dict(CLASSIFIER_DEFAULTS)


def _classifier_window(cls_conf: Dict[str, Any]) -> Optional[int]:
    """The classifier model's context window in tokens, or None if nobody knows.

    ``capabilities_for`` is the one merge: policy's ``declared`` keys beat the registry,
    the same precedence the capability filter routes on. Passing the whole classifier
    block is safe — that function merges only recognised registry fields, so ``chain``,
    ``on_total_failure`` and the rest ride along harmlessly.

    None is a real answer and is returned rather than guessed. The docker install's
    classifier is ``us.anthropic.claude-haiku-4-5-…``, and the registry deliberately does
    not carry ``us.anthropic.*`` (same weights, different commercial rail), so None is
    what an honest lookup gives there. The caller falls back to the absolute budget,
    which is what makes the feature work on that install at all.
    """
    model = cls_conf.get("model") or CLASSIFIER_DEFAULTS["model"]
    try:
        caps = _caps.capabilities_for(str(model), cls_conf)
    except (AttributeError, TypeError, ValueError):
        return None
    window = (caps or {}).get("context_window")
    return window if isinstance(window, int) and window > 0 else None


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
        d = CLASSIFIER_DEFAULTS
        self.model: str = cls_conf.get("model", d["model"])
        self.provider: str = cls_conf.get("provider", d["provider"])
        self.temperature: float = float(
            cls_conf.get("temperature", d["temperature"]))
        self.max_tokens: int = int(cls_conf.get("max_tokens", d["max_tokens"]))
        self.timeout_seconds: int = int(
            cls_conf.get("timeout_seconds", d["timeout_seconds"]))
        self._anchors = anchors or []
        # An absent ``tiers`` table degrades to EMPTY (see the DEFAULT_TIERS
        # removal note above) rather than to a hardcoded stale rail set.
        self._tiers = config.get("tiers") or {}
        # The classifier's OWN window, for sizing the prompt sent to it. Read
        # through the same merge every capability decision uses, so `declared`
        # keys on the classifier block win over the registry exactly as they do
        # on a tier's elo — and an id the registry has never heard of (every
        # `us.anthropic.*` is uncatalogued by design) yields None rather than an
        # invented number.
        self._context_window: Optional[int] = _classifier_window(cls_conf)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def task_char_budget(self, preamble_chars: int = 0) -> int:
        """How many characters of task text this prompt may carry.

        ``preamble_chars`` is the length of everything already in the prompt — rubric,
        anchors, features. Passed in rather than assumed as a constant because the
        anchors are operator-supplied and can be any length, so a fixed allowance would
        be wrong exactly when someone configures many of them. Defaults to 0 so the
        method also answers the plain question "what is the cap for this classifier".

        The smaller of two bounds, so both hold:

        * :data:`_TASK_CHAR_BUDGET` — the purpose bound. A difficulty verdict does not
          get better with more text (see the module note); this is what keeps the call
          cheap and fast on every model, catalogued or not.
        * the classifier's own window, when it is known — the physical bound. Room for
          the reply (``max_tokens``) comes off first, the recorded 5/4 headroom is
          applied through :func:`capabilities.without_safety_margin` so the preamble and
          tool scaffolding are paid for, and the remainder is converted to characters at
          the ratio :mod:`router.signals` records. Never below zero: a classifier
          configured with a window smaller than its own reply allowance yields 0, and
          0 means "send the features and no text", not "send everything".

        Both are floors of the same intent — err toward less room — so a prompt that fits
        the budget fits the window.
        """
        budget = _TASK_CHAR_BUDGET
        if self._context_window is not None:
            room_tokens = _caps.without_safety_margin(
                max(0, self._context_window - self.max_tokens)
            )
            room_chars = int(room_tokens * chars_per_token()) - max(0, preamble_chars)
            budget = min(budget, max(0, room_chars))
        return budget

    def build_prompt(self, task: str, features: Dict[str, Any]) -> str:
        """Build the one-shot classifier prompt with anchors and context.

        The task is bounded by :meth:`task_char_budget`, and a bounded task SAYS so —
        see the disclosure at the bottom. Silently truncating would be the worse bug of
        the two: the classifier would read a prefix as if it were the whole job and
        answer confidently about work it never saw.
        """
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
        ])
        # The task, bounded — and when it was bounded, the prompt says the number it was
        # bounded to and the number it came from. That turns a truncation from a lie into
        # a fact the classifier can weigh: "I am seeing 4000 of 51230 characters" is
        # itself evidence of a large job, and it stops a hedged answer being read as a
        # confident one about the whole thing.
        text = str(task if task is not None else "")
        budget = self.task_char_budget(len("\n".join(lines)))
        if len(text) > budget:
            lines.append(
                f"Task (first {budget} of {len(text)} characters; the rest was not sent "
                f"— judge from this excerpt and the size features above):"
            )
            lines.append(f"\"{text[:budget]}\"")
        else:
            lines.append(f"Task: \"{text}\"")
        lines.extend([
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
        # The classifier is an LLM answering a prompt, not a typed API. It is asked
        # for "high|med|low" and returns "Low", "LOW", "very low", " low " - and
        # occasionally a float or nothing. An exact == "low" comparison missed every
        # variant, and the ratchet exists precisely to catch uncertain answers: a
        # missed ratchet sends work the classifier was NOT sure about to the CHEAPEST
        # tier. Measured before this fix: only exact lowercase "low" ratcheted;
        # "Low", "LOW", "low ", "very low" all did not.
        conf = str(confidence if confidence is not None else "").strip().lower()

        tier = str(tier or "").strip().upper()
        if tier not in _UPWARD_RATCHET:
            tier = "T4"  # unknown -> strongest

        # endswith, not ==, so a hedged "very low" still counts as low.
        if conf.endswith("low"):
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
