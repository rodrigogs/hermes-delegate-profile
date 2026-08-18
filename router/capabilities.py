"""Model capability registry, capability-aware chain shaping, price windows.

Pure: no IO, no state, no model calls, NO CLOCK READ, no global randomness.
Every function is deterministic — for the random ordering strategy the caller
injects a ``random.Random`` and for every time-dependent answer the caller
injects a ``datetime``, so ``same inputs + same rng + same when => same
output`` and ``rules.py`` keeps its "pure and deterministic" contract.

``datetime`` is imported for TYPING and for normalizing an aware ``when`` to
UTC only. There is deliberately no ``now()``/``utcnow()`` call in this module:
the clock is a parameter supplied at the edge (``service``/``adapter``/``cli``),
exactly like the injected rng. ``when=None`` means TIME-AGNOSTIC — every
multiplier is 1.0, ``cheapest_now`` degrades to sequential, and the time policy
and time cap are no-ops. Nothing raises and nothing guesses at the hour.

The registry is DATA, verified against first-party pricing pages. Prices are
USD per 1M tokens and are ``None`` when the vendor publishes no per-token
price — a ``None`` price is NEVER coerced to 0.0, because a plan model's cost is
denominated in credits and a fabricated 0.0 would make it win every cost
comparison for the wrong reason. (``cheapest_now`` does rank a plan rail ahead of
a metered one, but on its ``billing_mode`` — a deliberate marginal-cost
judgement about an hour already bought — never on a price that looked like zero.)
Operators may override any registry field per elo in router.yaml via `declared`
keys — `declared` WINS, so a stale registry entry is fixable in YAML without a
code change. Only genuine CAPABILITY keys (:data:`CAPABILITY_ASSERTION_KEYS`)
make a model "known", though: ``billing_mode``, ``notes`` and the price fields
are commercial metadata, and a hop that declares nothing but its billing mode
is still unknown to this registry and must be reported as such.

Time-varying pricing is encoded per entry as ``price_windows``; the stored
``price_in``/``price_out`` are always the BASE rate, i.e. the rate OUTSIDE
every declared window (see :func:`price_multiplier`).

Vendor context that does not fit a registry field, kept here so it is not lost:

  zai (GLM) — the Coding Plan covers ONLY glm-5.3, glm-5-turbo and glm-4.7
    (+ glm-4.6v through the Vision MCP); requests naming glm-5.2 or glm-5.1
    are SILENTLY auto-routed to glm-5.3, and glm-5 is not plan-eligible at all.
    Plan credit multipliers (input/cached/output): glm-5.3 6.9/1.7/24,
    glm-5-turbo 5.7/1.5/21, glm-4.7 4.6/1.2/16, glm-4.6v 1.2/0.3/2.7.
    Off-peak spends 50% of the credits; peak is Mon-Fri 14:00-18:00 UTC+8 ==
    06:00-10:00 UTC on WEEKDAYS ONLY, encoded as a weekday-gated 2.0x window on
    the four plan-covered models. The whole weekend bills off-peak.
  deepseek — prices changed 2026-08-16 16:00 UTC and gained a peak/off-peak
    split: "Off-peak rates are half of the peak rates. Peak hours are
    01:00 - 04:00 and 06:00 - 10:00 UTC (all other hours are off-peak)", input,
    cached input and output alike. ``price_in``/``price_out`` hold the OFF-PEAK
    (base) price and peak is a 2.0x ``price_windows`` entry, EVERY day.
  xiaomi — the one provider whose window is CHEAP rather than expensive: 0.8x
    during 16:00-00:00 UTC (the 00:00-08:00 UTC+8 night discount).
  openai-codex — reachable flat-rate through a ChatGPT subscription OR metered
    by API key. Listed prices are the metered short-context rates; input above
    272K tokens is billed 2x input and 1.5x output. Because those prices ARE the
    rate that rail bills at, a ``subscription`` elo stays quoted in dollars and
    ``cheapest_now`` compares it like any metered rail instead of ranking it as
    already-paid — see :data:`_BILLING_RANK`.
  minimax — ``MiniMax-M3`` is case-sensitive, ``tool_choice`` accepts only
    auto|none, and input above 512K doubles the price.
  moonshot — kimi-k3 needs a >=$1 top-up before it unlocks and must not be
    hot-swapped mid-session (it was trained with preserved thinking history).
  nous — a white-label reseller in front of OpenRouter, so it shares an
    upstream with it and the two are NOT independent rails.

  No other provider here prices by the clock. OpenAI Batch/Flex and Gemini
  Batch are 50% off but are SEPARATE ENDPOINTS, not clock windows, so they are
  deliberately NOT modelled as ``price_windows`` — a window would claim a
  discount the router gets without changing anything about how it calls.

The 06:00-10:00 UTC window is peak on BOTH primary rails at once (deepseek
every day, zai on weekdays), which is why the time policy has to be able to
demote two providers simultaneously without emptying a chain.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Closed key sets — extend ONLY by adding a member, never a new family
# ---------------------------------------------------------------------------

#: Requirement keys a rule/tier may ask for. Anything else is ignored.
REQUIREMENT_KEYS: frozenset = frozenset(
    {"min_context", "vision", "tool_calling", "structured_output"}
)

#: How an elo is paid for.
BILLING_MODES: frozenset = frozenset({"plan", "subscription", "metered", "free"})

# Deterministic evaluation order for requirements, so a chain that both
# contradicts one requirement and is unknown on another always reports the
# contradiction (rejection beats unknown).
_REQUIREMENT_ORDER: Tuple[str, ...] = (
    "min_context",
    "vision",
    "tool_calling",
    "structured_output",
)

# Boolean requirement -> rejection reason.
_BOOL_REJECT_REASON: Dict[str, str] = {
    "vision": "no_vision",
    "tool_calling": "no_tool_calling",
    "structured_output": "no_structured_output",
}

#: Closed reason set returned by :func:`satisfies`.
_REASONS: frozenset = frozenset(
    {
        "context_too_small",
        "no_vision",
        "no_tool_calling",
        "no_structured_output",
        "capability_unknown",
    }
)

#: Keys that are a genuine CAPABILITY ASSERTION — a claim about what a model
#: can do. These, and only these, can make a model absent from the registry
#: count as "known" (see :func:`capabilities_for`). Callers that build a
#: `declared` override out of a router.yaml hop should mirror THIS set, not the
#: full field list: an elo that declares nothing but ``billing_mode`` (which
#: router.yaml requires of every elo) asserts no capability at all, so treating
#: it as known would silence the unknown-model warning for every elo in policy.
CAPABILITY_ASSERTION_KEYS: frozenset = frozenset(
    {
        "context_window",
        "max_input_tokens",
        "max_output",
        "vision",
        "tool_calling",
        "structured_output",
    }
)

# COMMERCIAL / annotation metadata. Overridable per elo (a stale price is
# fixable in YAML) but never evidence that a model's capabilities are known.
_COMMERCIAL_FIELDS: frozenset = frozenset(
    {
        "billing_mode",
        "price_in",
        "price_out",
        "price_windows",
        "notes",
    }
)

# Identity, not capability: naming a provider says nothing about what the model
# can do. ``service.py`` already refuses to pass it as a declaration; the split
# here means the registry is safe even when a caller forgets.
_IDENTITY_FIELDS: frozenset = frozenset({"provider"})

#: Every field a registry entry (or a `declared` override) may carry.
_REGISTRY_FIELDS: frozenset = (
    CAPABILITY_ASSERTION_KEYS | _COMMERCIAL_FIELDS | _IDENTITY_FIELDS
)

# Ordering strategies. Anything unrecognized degrades to sequential.
_SEQUENTIAL = "sequential"
_RANDOM = "random"
_CHEAPEST_NOW = "cheapest_now"

#: Closed strategy set, exported so lint can reject a typo at the write gate.
FALLBACK_STRATEGIES: frozenset = frozenset({_SEQUENTIAL, _RANDOM, _CHEAPEST_NOW})

# `cheapest_now` buckets, in sort order. The bucket is decided by BILLING MODE
# and by nothing else — in particular NOT by whether a dollar price happens to be
# published. An elo billed against the z.ai Coding PLAN is FREE AT THE MARGIN and
# is not quoted in dollars at all: the plan spends CREDITS off an allowance
# already bought (glm-5.3 24 output credits, glm-5-turbo 21, glm-4.7 16, glm-4.6v
# 2.7), so the next token adds nothing to any invoice — however many dollars the
# registry ALSO records as that model's separately-purchasable metered list price.
# glm-4.7, glm-5-turbo and glm-4.6v are exactly that case — plan-covered AND
# carrying a list price — and bucketing them by the ABSENCE of a price would
# compare them in dollars the operator does not pay, then spend metered tokens to
# avoid a cost that is already sunk: plan-covered glm-4.7 (2.20 out, 4.40 inside
# zai's weekday peak) would sort behind metered mimo-v2.5 (0.28, 0.224 inside
# xiaomi's night discount).
#
# `subscription` is deliberately NOT in that bucket, and it is the one line here
# worth arguing about. A ChatGPT/Codex seat is flat-rate too, but every
# openai-codex elo in this registry publishes the per-token rate that rail bills
# at — "Listed prices are the metered short-context rates", see the module
# docstring — so its cost stays denominated in DOLLARS and stays commensurable
# with every other dollar-priced rail. Ranking a seat ahead of metered on billing
# mode alone would take the price comparison off the table for a whole chain: it
# is precisely what stops the shipped T2 tail [gpt-5.6-luna 1.20 flat,
# deepseek-v4-flash 0.66 -> 1.32 in its peak] from reordering by the hour, which
# reduces the injected clock to decoration. A rail is bucketed on the UNIT its
# price is quoted in, not on who is holding the credential.
#
# A free rail spends nothing either, but it carries the reliability caveats
# recorded in the design, so it follows the plan bucket; the dollar-priced rails
# come next; an elo whose billing mode nothing can describe sorts last, because
# claiming it is the cheapest would be inventing the very number this layer
# refuses to invent.
_BUCKET_PLAN_CREDITS = 0   # billing_mode plan: credits, off an allowance bought
_BUCKET_FREE = 1           # billing_mode free
_BUCKET_DOLLARS = 2        # billing_mode subscription / metered
_BUCKET_UNKNOWN = 3        # no describable billing mode

#: Billing mode -> `cheapest_now` bucket; lower is cheaper at the margin. EVERY
#: member of :data:`BILLING_MODES` must appear: a mode that fell through here
#: would land in the unknown bucket and sort last on cost, which is a silent
#: routing change rather than a diagnostic.
_BILLING_RANK: Dict[str, int] = {
    "plan": _BUCKET_PLAN_CREDITS,
    "free": _BUCKET_FREE,
    "subscription": _BUCKET_DOLLARS,
    "metered": _BUCKET_DOLLARS,
}
_BILLING_RANK_UNKNOWN = _BUCKET_UNKNOWN

# Rank WITHIN a bucket. Elos that publish a dollar price are compared in dollars,
# ascending effective output price; an elo with no published price cannot be
# converted into dollars without inventing an exchange rate, so it sorts behind
# its priced bucket-mates in declared order rather than being treated as 0.0.
# In the zai plan bucket that ordering also happens to be the credit ordering:
# glm-4.6v/glm-4.7/glm-5-turbo cost 2.7/16/21 output credits in list-price order,
# and unpriced glm-5.3 — last here — is the most expensive of the four at 24.
_PRICED_IN_BUCKET = 0
_UNPRICED_IN_BUCKET = 1

# Safety headroom applied to est_input_tokens when deriving min_context:
# the prompt grows with the system preamble, tool schemas and the reply.
_CONTEXT_SAFETY_NUMERATOR = 5
_CONTEXT_SAFETY_DENOMINATOR = 4  # 5/4 == 1.25

# Clock arithmetic. `hours_utc` is HALF-OPEN [start, end); end may be 24, which
# is midnight-exclusive. A window that would cross midnight is TWO entries, so
# nothing here implements wrap-around arithmetic.
_HOURS_IN_DAY = 24
_DAYS_IN_WEEK = 7
_HOURS_IN_WEEK = _HOURS_IN_DAY * _DAYS_IN_WEEK

# `weekdays` absent means every day. 0 = Monday .. 6 = Sunday, matching
# datetime.weekday(), so nothing has to translate between two conventions.
_EVERY_DAY: frozenset = frozenset(range(_DAYS_IN_WEEK))

# Windows and caps are declared data, so exact float equality is the wrong test
# for "changed" / "exceeds". One part in a billion is far below any published
# multiplier's precision.
_MULTIPLIER_EPSILON = 1e-9

#: Neutral multiplier — no window matches, no clock, or an unknown model.
_FLAT_MULTIPLIER = 1.0

# Providers that resolve to a shared upstream. Two hops in the same group give
# no real redundancy — one upstream outage takes both down.
_UPSTREAM_GROUPS: Dict[str, str] = {
    "nous": "openrouter",
    "openrouter": "openrouter",
}


# ---------------------------------------------------------------------------
# Registry — verified data. Do not "improve" these numbers without a source.
# ---------------------------------------------------------------------------

MODEL_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    # -- zai (GLM) ---------------------------------------------------------
    "glm-5.3": {
        "provider": "zai",
        "context_window": 1_000_000,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "plan",
        "price_in": None,
        "price_out": None,
        # Plan credits, not dollars: 2.0x on weekday 06:00-10:00 UTC, half-rate
        # every other hour including the whole weekend. price_in stays None —
        # there is no dollar price to scale, and None must never become 0.0.
        "price_windows": [
            {"hours_utc": [6, 10], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
        ],
        "notes": "metered API not launched; credit multipliers only",
    },
    "glm-5-turbo": {
        "provider": "zai",
        "context_window": 200_000,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "plan",
        "price_in": 1.20,
        "price_out": 4.00,
        "price_windows": [
            {"hours_utc": [6, 10], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
        ],
    },
    "glm-4.7": {
        "provider": "zai",
        "context_window": 200_000,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "plan",
        "price_in": 0.60,
        "price_out": 2.20,
        "price_windows": [
            {"hours_utc": [6, 10], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
        ],
        "notes": "also purchasable metered at the same price",
    },
    "glm-4.7-flashx": {
        "provider": "zai",
        "context_window": 200_000,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 0.07,
        "price_out": 0.40,
    },
    "glm-4.7-flash": {
        "provider": "zai",
        "context_window": 200_000,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
    },
    "glm-5.2": {
        "provider": "zai",
        "context_window": 1_048_576,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 1.40,
        "price_out": 4.40,
        "notes": "plan auto-routes this id to glm-5.3",
    },
    "glm-5.1": {
        "provider": "zai",
        "context_window": 204_800,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 1.40,
        "price_out": 4.40,
        "notes": "plan auto-routes this id to glm-5.3",
    },
    "glm-5": {
        "provider": "zai",
        "context_window": 204_800,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 1.00,
        "price_out": 3.20,
        "notes": "NOT plan-eligible; a Coding Plan key cannot call it",
    },
    "glm-4.6": {
        "provider": "zai",
        "context_window": 204_800,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 0.60,
        "price_out": 2.20,
    },
    "glm-5v-turbo": {
        "provider": "zai",
        "context_window": 202_752,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": False,
        "billing_mode": "metered",
        "price_in": 1.20,
        "price_out": 4.00,
    },
    "glm-4.6v": {
        "provider": "zai",
        "context_window": 204_800,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "plan",
        "price_in": 0.30,
        "price_out": 0.90,
        "price_windows": [
            {"hours_utc": [6, 10], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
        ],
    },
    "glm-4.5v": {
        "provider": "zai",
        "context_window": 65_536,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": False,
        "billing_mode": "metered",
        "price_in": 0.60,
        "price_out": 1.80,
    },
    "glm-4.5-flash": {
        "provider": "zai",
        "context_window": 131_072,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": None,
        "price_out": None,
        "notes": (
            "current classifier; scored 3/10 in the operator's own "
            "reliability bench"
        ),
    },
    # -- deepseek ----------------------------------------------------------
    "deepseek-v4-flash": {
        "provider": "deepseek",
        "context_window": 1_048_576,
        "max_output": 384_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 0.22,
        "price_out": 0.66,
        # Peak is EVERY day, so no `weekdays` gate. Two entries because
        # 01:00-04:00 and 06:00-10:00 are disjoint, not one wrapping window.
        "price_windows": [
            {"hours_utc": [1, 4], "multiplier": 2.0},
            {"hours_utc": [6, 10], "multiplier": 2.0},
        ],
        "notes": "cache-hit input 0.007 off-peak",
    },
    "deepseek-v4-pro": {
        "provider": "deepseek",
        "context_window": 1_048_576,
        "max_output": 384_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 0.66,
        "price_out": 1.98,
        "price_windows": [
            {"hours_utc": [1, 4], "multiplier": 2.0},
            {"hours_utc": [6, 10], "multiplier": 2.0},
        ],
        "notes": "cache-hit input 0.022 off-peak",
    },
    # -- openai-codex ------------------------------------------------------
    "gpt-5.6-sol": {
        "provider": "openai-codex",
        "context_window": 1_050_000,
        "max_input_tokens": 922_000,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "subscription",
        "price_in": 5.00,
        "price_out": 30.00,
        "notes": "Plus 10-100 msgs/5h",
    },
    "gpt-5.6-terra": {
        "provider": "openai-codex",
        "context_window": 1_050_000,
        "max_input_tokens": 922_000,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "subscription",
        "price_in": 2.00,
        "price_out": 12.00,
        "notes": "Plus 25-200 msgs/5h; Terminal-Bench 2.1 78.4% verified",
    },
    "gpt-5.6-luna": {
        "provider": "openai-codex",
        "context_window": 1_050_000,
        "max_input_tokens": 922_000,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "subscription",
        "price_in": 0.20,
        "price_out": 1.20,
        "notes": "Plus 250-2000 msgs/5h; Terminal-Bench 2.1 75.7% verified",
    },
    "gpt-5.5": {
        "provider": "openai-codex",
        "context_window": 1_050_000,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "subscription",
        "price_in": 5.00,
        "price_out": 30.00,
        "notes": (
            "Plus 15-80 msgs/5h; Terminal-Bench 2.1 83.1%; served with "
            "400K ctx inside Codex, 1.05M on the API"
        ),
    },
    "gpt-5.3-codex": {
        "provider": "openai-codex",
        "context_window": 400_000,
        "max_input_tokens": 272_000,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 1.75,
        "price_out": 14.00,
        "notes": "Responses API only, no Chat Completions, no Batch",
    },
    "gpt-5.4-mini": {
        "provider": "openai-codex",
        "context_window": 400_000,
        "max_input_tokens": 272_000,
        "max_output": 128_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 0.75,
        "price_out": 4.50,
    },
    # -- xiaomi ------------------------------------------------------------
    "mimo-v2.5": {
        "provider": "xiaomi",
        "context_window": 1_050_000,
        "max_output": 131_072,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 0.14,
        "price_out": 0.28,
        # A CHEAP window, not a peak: the 00:00-08:00 UTC+8 night discount.
        "price_windows": [
            {"hours_utc": [16, 24], "multiplier": 0.8},
        ],
        "notes": (
            "omnimodal text+image+audio+video; cache-hit 0.0028; "
            "no free tier exists"
        ),
    },
    "mimo-v2.5-pro": {
        "provider": "xiaomi",
        "context_window": 1_050_000,
        "max_output": 131_072,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 0.435,
        "price_out": 0.87,
        "price_windows": [
            {"hours_utc": [16, 24], "multiplier": 0.8},
        ],
        "notes": "cache-hit 0.0036",
    },
    # -- minimax -----------------------------------------------------------
    "MiniMax-M3": {
        "provider": "minimax",
        "context_window": 1_000_000,
        "max_output": 131_072,
        "vision": True,
        "tool_calling": True,
        "structured_output": False,
        "billing_mode": "metered",
        "price_in": 0.30,
        "price_out": 1.20,
        "notes": (
            "model id is case-sensitive; tool_choice supports only auto|none; "
            ">512K input doubles the price"
        ),
    },
    # -- moonshot ----------------------------------------------------------
    "kimi-k3": {
        "provider": "moonshot",
        "context_window": 1_048_576,
        "max_output": 131_072,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "metered",
        "price_in": 3.00,
        "price_out": 15.00,
        "notes": (
            "cache-hit 0.30; requires a >=$1 top-up before k3 unlocks; "
            "do NOT hot-swap mid-session, it was trained with preserved "
            "thinking history"
        ),
    },
    "kimi-k2.7-code": {
        "provider": "moonshot",
        "context_window": 262_144,
        "max_output": 256_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": False,
        "billing_mode": "metered",
        "price_in": 0.95,
        "price_out": 4.00,
        "notes": "strict json_schema not documented, only JSON Mode",
    },
    # -- nous (white-label reseller in front of openrouter) ----------------
    "stepfun/step-3.7-flash:free": {
        "provider": "nous",
        "context_window": 262_144,
        "max_output": 256_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
    },
    "tencent/hy3:free": {
        "provider": "nous",
        "context_window": 262_144,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
        "notes": "only free model here that can disable reasoning entirely",
    },
    "upstage/solar-pro4:free": {
        "provider": "nous",
        "context_window": 524_288,
        "max_output": 131_072,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
    },
    "meituan/longcat-2.0:free": {
        "provider": "nous",
        "context_window": 1_048_576,
        "max_output": 131_072,
        "vision": False,
        "tool_calling": True,
        "structured_output": False,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
    },
    "poolside/laguna-s-2.1:free": {
        "provider": "nous",
        "context_window": 262_144,
        "max_output": 131_072,
        "vision": False,
        "tool_calling": True,
        "structured_output": False,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
        "notes": (
            "paid twin has 1_048_576 ctx; the free variant loses 75% "
            "of context"
        ),
    },
    "inclusionai/ling-3.0-flash": {
        "provider": "nous",
        "context_window": 262_144,
        "max_output": 32_768,
        "vision": False,
        "tool_calling": True,
        "structured_output": False,
        "billing_mode": "metered",
        "price_in": 0.0168,
        "price_out": 0.0504,
        "notes": 'NO ":free" variant of this id exists',
    },
    # -- openrouter --------------------------------------------------------
    "nvidia/nemotron-3-ultra-550b-a55b:free": {
        "provider": "openrouter",
        "context_window": 1_000_000,
        "max_output": 65_536,
        "vision": False,
        "tool_calling": True,
        "structured_output": False,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
    },
    "nvidia/nemotron-3-super-120b-a12b:free": {
        "provider": "openrouter",
        "context_window": 262_144,
        "max_output": 262_144,
        "vision": False,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
    },
    "dots-studio/dots-3-note-preview:free": {
        "provider": "openrouter",
        "context_window": 512_000,
        "max_output": 512_000,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
    },
    "z-ai/glm-5.2:free": {
        "provider": "openrouter",
        "context_window": 128_000,
        "max_output": 128_000,
        "vision": False,
        "tool_calling": False,
        "structured_output": False,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
        "notes": (
            "NO tool calling — hard-fails any agent loop that sends "
            "tool definitions"
        ),
    },
    "openrouter/free": {
        "provider": "openrouter",
        "context_window": 200_000,
        "max_output": None,
        "vision": True,
        "tool_calling": True,
        "structured_output": True,
        "billing_mode": "free",
        "price_in": 0.00,
        "price_out": 0.00,
        "notes": (
            "meta-router that selects among currently-available free models"
        ),
    },
}

#: The largest context window ANY registered model has. A ``min_context``
#: requirement above this is unsatisfiable by construction — no registered rail
#: can ever serve it — which :func:`filter_chain` reports as a named condition
#: instead of letting it look like an ordinary per-elo rejection.
MAX_REGISTERED_CONTEXT: int = max(
    (
        window
        for window in (
            _entry.get("context_window") for _entry in MODEL_CAPABILITIES.values()
        )
        if isinstance(window, int) and not isinstance(window, bool) and window > 0
    ),
    default=0,
)


# ---------------------------------------------------------------------------
# Public API — capability lookup
# ---------------------------------------------------------------------------

def capabilities_for(
    model: str,
    declared: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the registry entry for ``model`` merged with ``declared``.

    ``declared`` holds per-elo overrides read straight out of router.yaml and
    WINS over the registry, so an operator can correct a stale entry in YAML
    without shipping code. Only recognized registry fields are merged, so a
    chain entry (which also carries ``model``, ``fallback``, tier policy …) can
    be passed in whole.

    Returns a NEW dict — never the registry entry itself, and ``price_windows``
    is copied too so no consumer can mutate registry data through a merged
    view — or None when the model is in neither the registry nor ``declared``.

    "In ``declared``" means declaring at least one CAPABILITY ASSERTION
    (:data:`CAPABILITY_ASSERTION_KEYS`). Commercial metadata does NOT make a
    model known: ``billing_mode`` is mandatory on every elo in router.yaml, so
    counting it would mean no elo is ever unknown and the unknown-model warning
    (plus liveness's ``capabilities_known`` flag) could never fire. Naming a
    ``provider`` says nothing about capability either. The commercial fields are
    still merged once the model IS known — they are overridable, just not
    evidence.
    """
    if not isinstance(model, str) or not model:
        return None

    entry = MODEL_CAPABILITIES.get(model)
    overrides = _declared_overrides(declared)

    if entry is None and not (set(overrides) & CAPABILITY_ASSERTION_KEYS):
        return None

    merged: Dict[str, Any] = dict(entry or {})
    merged.update(overrides)
    windows = merged.get("price_windows")
    if isinstance(windows, list):
        merged["price_windows"] = [
            dict(window) if isinstance(window, dict) else window
            for window in windows
        ]
    return merged


def satisfies(
    model: str,
    requirements: Dict[str, Any],
    declared: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Return ``(ok, reason)`` for ``model`` against ``requirements``.

    ``reason`` is "" when ok and comes from the closed set otherwise:
    context_too_small, no_vision, no_tool_calling, no_structured_output,
    capability_unknown.

    Fail-OPEN on ignorance, fail-CLOSED on knowledge: a capability that is
    absent or unpublished NEVER rejects — it yields (True,
    "capability_unknown") so the caller can flag it without breaking routing.
    Only a KNOWN contradiction rejects, and a contradiction always wins over
    an unknown.
    """
    caps = capabilities_for(model, declared)
    if caps is None:
        return True, "capability_unknown"
    if not isinstance(requirements, dict) or not requirements:
        return True, ""

    unknown = False
    for key in _REQUIREMENT_ORDER:
        if key not in requirements:
            continue
        wanted = requirements[key]

        if key == "min_context":
            needed = _as_int(wanted)
            if needed is None or needed <= 0:
                continue
            window = _as_int(caps.get("context_window"))
            if window is None:
                unknown = True
            elif needed > window:
                return False, "context_too_small"
            continue

        # Boolean capabilities: only a True requirement constrains anything.
        if not wanted:
            continue
        have = caps.get(key)
        if have is None:
            unknown = True
        elif not have:
            return False, _BOOL_REJECT_REASON[key]

    if unknown:
        return True, "capability_unknown"
    return True, ""


# ---------------------------------------------------------------------------
# Public API — time-windowed pricing
#
# Window shape stored per registry entry (optional; absent = flat pricing at all
# hours):
#
#   "price_windows": [
#       {"hours_utc": [start, end), "weekdays": [0..6] | absent,
#        "multiplier": float},
#   ]
#
# ``hours_utc`` is HALF-OPEN [start, end): the start hour is INSIDE the window,
# the end hour is OUTSIDE it, and ``end`` may be 24 (midnight-exclusive). A
# window that crosses midnight is expressed as TWO entries — ``start > end`` is a
# lint error (see :func:`price_window_diagnostics`), so no consumer here or
# downstream implements wrap-around arithmetic. ``weekdays`` absent means every
# day; 0 = Monday … 6 = Sunday, matching ``datetime.weekday()``.
#
# The stored ``price_in``/``price_out`` are the BASE rate — what the model costs
# OUTSIDE every declared window. deepseek/zai therefore store the off-peak rate
# with a 2.0x peak window, and xiaomi stores the daytime rate with a 0.8x night
# window. Encoding it any other way would make "the price" mean something
# different depending on which provider you asked about.
# ---------------------------------------------------------------------------

def price_multiplier(
    model: str,
    when: Optional[datetime] = None,
    declared: Optional[Dict[str, Any]] = None,
) -> float:
    """Return the price multiplier in force for ``model`` at ``when``.

    1.0 — the neutral, base-rate answer — when ``when`` is None (time-agnostic:
    the clock is injected, never read here), when ``model`` is unknown to both
    the registry and ``declared``, or when no declared window matches. So a
    caller that never passes a clock sees today's flat behaviour exactly.

    ``when`` is interpreted in UTC: an aware datetime is converted, a naive one
    is assumed to already be UTC. Anything that is not a usable
    hour+weekday source is treated as no clock at all rather than raising.

    Windows are lint-checked non-overlapping; if a malformed registry ever does
    overlap, the FIRST matching entry wins so the answer stays deterministic.
    """
    parts = _utc_parts(when)
    if parts is None:
        return _FLAT_MULTIPLIER
    caps = capabilities_for(model, declared)
    if caps is None:
        return _FLAT_MULTIPLIER
    hour, weekday = parts
    return _multiplier_at(_windows_of(caps), hour, weekday)


def effective_price(
    model: str,
    when: Optional[datetime] = None,
    declared: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[float, float]]:
    """Return ``(price_in, price_out)`` for ``model`` at ``when``, or None.

    The stored base rates scaled by :func:`price_multiplier`. USD per 1M tokens.

    None means "this model publishes no per-token price" — a plan-credit model
    such as glm-5.3, or an elo the registry has never heard of. NEVER
    ``(0.0, 0.0)``: a plan model is not free, its cost is denominated in credits,
    and coercing None to zero would make it win every cost comparison. A model
    that publishes only half a price pair is treated the same way, because the
    missing half would have to be invented. A genuinely free rail declares
    ``price_in: 0.0``, which IS a published price and is returned as such.

    An elo that declares a price but no capability at all is still UNKNOWN by
    :func:`capabilities_for`'s rule, so it prices as None; declaring one real
    thing about the model — its context window, say — makes both its
    capabilities and its declared price usable. One gate, not two.
    """
    caps = capabilities_for(model, declared)
    if caps is None:
        return None
    price_in = _as_float(caps.get("price_in"))
    price_out = _as_float(caps.get("price_out"))
    if price_in is None or price_out is None:
        return None
    multiplier = price_multiplier(model, when, declared)
    return price_in * multiplier, price_out * multiplier


def in_expensive_window(
    model: str,
    when: Optional[datetime] = None,
    declared: Optional[Dict[str, Any]] = None,
) -> bool:
    """Whether ``model`` is inside a window that COSTS MORE at ``when``.

    True only for a matching window with ``multiplier > 1.0``. xiaomi's 0.8x
    night discount is a window too, and it must never be read as "avoid this
    now" — hence "expensive" rather than "in a window".
    """
    multiplier = price_multiplier(model, when, declared)
    return multiplier - _FLAT_MULTIPLIER > _MULTIPLIER_EPSILON


def next_window_change(
    model: str,
    when: Optional[datetime] = None,
    declared: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return WHEN ``model``'s multiplier next changes, and to what::

        {"hour": 6, "weekday": 0, "hours_ahead": 47, "multiplier": 2.0}

    ``hour`` is the UTC hour the change lands on, ``weekday`` the UTC weekday it
    lands on (0 = Monday … 6 = Sunday, matching ``datetime.weekday()``),
    ``hours_ahead`` how many whole hours from ``when`` that is, and
    ``multiplier`` the rate that takes effect then.

    The DAY and ``hours_ahead`` are load-bearing, not decoration: a
    weekday-gated window means an hour alone is ambiguous by up to two days.
    ``next_window_change("glm-4.7", Saturday 07:00Z)`` lands on hour 6, which as
    a bare hour reads as "23 hours away" when the real answer is Monday 06:00,
    47 hours away. Any consumer computing a countdown must read
    ``hours_ahead``; ``hour``/``weekday`` are for labelling the instant. Minutes
    are deliberately not modelled — windows begin on the hour, so ``hours_ahead``
    counts whole hour boundaries crossed, and 23:59 is one hour from midnight.

    None when the multiplier never changes — a flat-priced model, an unknown
    model, a registry whose windows cover every hour at one multiplier, or no
    injected clock. Hours are scanned forward over one full week, which is the
    whole period of the schedule (``weekdays`` is the only date dependence a
    window has), so "never changes" is a proof, not a give-up.

    Pure in the injected clock like everything else here: ``when`` is the only
    source of "now", so the answer is a function of its arguments alone.

    Powers the console's "peak ends in 2h" affordance — which is why the shape
    matches the one the console's own JS already computes — and is the seam a
    future deferral scheduler would read; deferring work is deliberately out of
    scope here, this module answers "what does it cost now", never "wait".
    """
    parts = _utc_parts(when)
    if parts is None:
        return None
    caps = capabilities_for(model, declared)
    if caps is None:
        return None

    windows = _windows_of(caps)
    if not windows:
        return None

    hour, weekday = parts
    current = _multiplier_at(windows, hour, weekday)
    for step in range(1, _HOURS_IN_WEEK):
        elapsed = hour + step
        ahead_hour = elapsed % _HOURS_IN_DAY
        ahead_weekday = (weekday + elapsed // _HOURS_IN_DAY) % _DAYS_IN_WEEK
        ahead = _multiplier_at(windows, ahead_hour, ahead_weekday)
        if abs(ahead - current) > _MULTIPLIER_EPSILON:
            return {
                "hour": ahead_hour,
                "weekday": ahead_weekday,
                "hours_ahead": step,
                "multiplier": ahead,
            }
    return None


# ---------------------------------------------------------------------------
# Public API — chain shaping
# ---------------------------------------------------------------------------

def filter_chain(
    chain: List[Dict[str, Any]],
    requirements: Dict[str, Any],
) -> Dict[str, Any]:
    """Drop chain hops that cannot satisfy ``requirements``.

    ``chain`` entries are {model, provider, ...optional declared capability
    keys...}. Returns::

        {"eligible": [...], "rejected": [...], "unknown": [...],
         "bypassed": bool, "unsatisfiable": [requirement_key, ...]}

    ``eligible`` preserves order and holds the ORIGINAL entry objects.
    ``rejected`` holds shallow copies carrying an added ``reject_reason``.
    ``unknown`` lists model ids whose capabilities are unpublished; they stay
    eligible.

    When filtering would empty the chain the filter BYPASSES itself:
    ``eligible`` becomes the original chain and ``bypassed`` is True, because a
    capability filter must never break routing outright. On that path
    ``rejected`` RETAINS every per-elo reason as DIAGNOSTICS — the entries are
    informational and are deliberately NOT excluded from ``eligible``. Throwing
    the reasons away is throwing them away exactly when the operator needs
    them: "nothing can meet this requirement" is only actionable next to WHICH
    requirement each elo failed. (This corrects the phase-1 contract, which
    specified ``rejected: []`` on bypass.) A consumer that renders ``rejected``
    as "dropped" must therefore check ``bypassed`` first.

    ``unsatisfiable`` names requirement keys that NO available model could ever
    meet — today only ``min_context``, when the derived floor exceeds both
    :data:`MAX_REGISTERED_CONTEXT` and every ``context_window`` declared in this
    chain. That is a pathological request (a turn implying a few hundred files
    already exceeds the largest registered window), not an ordinary per-elo
    rejection, so it is reported as its own named condition. It is
    informational: it never changes eligibility, and it can be set with
    ``bypassed`` False when some hop passes on a fail-open unknown.
    """
    entries = [entry for entry in (chain or []) if isinstance(entry, dict)]
    unsatisfiable = _unsatisfiable_requirements(requirements, entries)
    if not entries:
        return {
            "eligible": list(entries),
            "rejected": [],
            "unknown": [],
            "bypassed": False,
            "unsatisfiable": unsatisfiable,
        }

    eligible: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    unknown: List[str] = []

    for entry in entries:
        model = entry.get("model")
        ok, reason = satisfies(model if isinstance(model, str) else "",
                               requirements, entry)
        if ok:
            eligible.append(entry)
            if reason == "capability_unknown" and isinstance(model, str):
                if model not in unknown:
                    unknown.append(model)
        else:
            copy = dict(entry)
            copy["reject_reason"] = reason
            rejected.append(copy)

    if not eligible:
        # No elo can meet the requirement — routing beats correctness here, but
        # the reasons are kept: they are the only explanation the operator gets.
        return {
            "eligible": list(entries),
            "rejected": rejected,
            "unknown": unknown,
            "bypassed": True,
            "unsatisfiable": unsatisfiable,
        }

    return {
        "eligible": eligible,
        "rejected": rejected,
        "unknown": unknown,
        "bypassed": False,
        "unsatisfiable": unsatisfiable,
    }


def order_chain(
    chain: List[Dict[str, Any]],
    strategy: str = "sequential",
    pin_primary: bool = True,
    rng: Optional[random.Random] = None,
    when: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return a NEW list of chain entries ordered by ``strategy``.

    "sequential"    — order unchanged (still a fresh list).
    "random"        — uniform shuffle through ``rng``.
    "cheapest_now"  — ascending effective OUTPUT price at ``when``.

    ``pin_primary`` True keeps ``chain[0]`` at index 0 and reorders only the
    tail, for the operator who wants a fixed first choice with sensibly ordered
    fallbacks; False reorders everything.

    ``rng`` is REQUIRED for "random" and ``when`` is REQUIRED for
    "cheapest_now": without them each degrades to sequential rather than
    reaching for global randomness or the wall clock, so the caller's purity
    contract holds. An unrecognized strategy also degrades to sequential — this
    never raises.

    "cheapest_now" ranks on MARGINAL price — what the next token actually adds to
    the bill — in two steps:

    1. ``billing_mode`` decides the bucket: ``plan`` first (the z.ai Coding Plan
       spends CREDITS off an allowance already bought, so the next token adds
       nothing to any dollar invoice), then ``free``, then the dollar-priced
       rails — ``subscription`` and ``metered`` TOGETHER — then an elo whose
       billing mode nothing can describe. The bucket is NOT decided by whether a
       price is published: glm-4.7, glm-5-turbo and glm-4.6v are plan-covered AND
       carry dollar list prices, and comparing those in dollars would sort
       plan-covered glm-4.7 (2.20 out, 4.40 inside its peak) behind metered
       mimo-v2.5 (0.28 out) — spending metered tokens to dodge a cost that is
       already sunk. ``subscription`` shares the metered bucket on purpose: an
       openai-codex seat publishes the per-token rate that rail bills at, so it
       stays quoted in dollars and stays comparable. Ranking a seat as
       already-paid instead leaves a chain's order IDENTICAL at every hour — the
       injected clock reduced to decoration (see :data:`_BILLING_RANK`).
    2. Inside a bucket, ascending effective OUTPUT price at ``when``, because
       output dominates agent cost. Ties keep declared order, so the ordering is
       stable and an operator's declared preference still expresses itself. An
       elo with no published price cannot be converted into dollars without
       inventing an exchange rate, so it sorts behind its priced bucket-mates in
       declared order — never as 0.0.

    LIMIT, and it is a real one: a ``plan`` or ``subscription`` rail is free at
    the margin only until its QUOTA is exhausted, and quota state is nowhere in
    the registry — nothing here can see how many plan credits or seat messages
    are left. "cheapest_now" therefore optimises marginal PRICE, not remaining
    entitlement, and relies on the EXISTING BREAKER to route around a rail that
    has run out: an exhausted plan key fails, the breaker opens it, and the next
    hop in this order is tried. It is NOT a substitute for quota awareness, which
    would need a live per-key usage feed this router does not have.
    """
    ordered = list(chain or [])
    if len(ordered) < 2:
        return ordered

    if strategy == _RANDOM and rng is not None:
        if pin_primary:
            tail = ordered[1:]
            rng.shuffle(tail)
            return ordered[:1] + tail
        rng.shuffle(ordered)
        return ordered

    if strategy == _CHEAPEST_NOW and when is not None:
        head = ordered[:1] if pin_primary else []
        tail = ordered[1:] if pin_primary else ordered
        return head + _by_cheapest_now(tail, when)

    return ordered


def apply_time_policy(
    chain: List[Dict[str, Any]],
    policy: Optional[Dict[str, Any]],
    when: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Reorder ``chain`` for a tier's ``time_policy`` at ``when``.

    ``policy`` is ``{"avoid_peak": [provider, ...], "prefer": [model, ...]}``.
    Returns ``{"chain": [...], "demoted": [model, ...], "promoted": [model, ...]}``.

    ``avoid_peak`` DEMOTES every elo of a named provider to the end of the chain
    while that elo is inside a ``multiplier > 1.0`` window, preserving relative
    order among the demoted. It NEVER removes an elo: a demoted rail is still
    better than no rail, and this must not be able to empty a chain — the same
    invariant the capability filter holds. The returned chain is always a
    PERMUTATION of the input.

    "While that elo is in a window" is deliberately PER ELO, not per provider: a
    same-provider elo with flat pricing costs no more at that hour, so demoting
    it would degrade the route and save nothing. ``avoid_peak: [zai]`` during
    zai's plan-credit peak therefore demotes glm-5.3 and leaves metered glm-4.6
    exactly where the operator put it, and ``demoted`` names precisely the elos
    that are actually charging more right now.

    ``prefer`` PROMOTES named models to the front, but only when they are not
    themselves in an expensive window — promoting an elo into its own peak would
    invert the intent. Promotion happens after demotion; the two can never fight
    over the same elo, since "expensive" is exactly the condition that demotes.

    ``when`` None is a NO-OP: the chain comes back as a fresh list in declared
    order with both diagnostic lists empty, because with no clock there is no
    peak to avoid and guessing at one is worse than doing nothing. A ``policy``
    that is not a mapping is the same no-op rather than an exception.

    Provider names match case-insensitively; model ids match EXACTLY, because a
    model id can be case-sensitive (``MiniMax-M3``).
    """
    entries = list(chain or [])
    if when is None or not isinstance(policy, dict) or not entries:
        return {"chain": entries, "demoted": [], "promoted": []}

    avoid = _provider_names(policy.get("avoid_peak"))
    prefer = _model_names(policy.get("prefer"))

    kept: List[Dict[str, Any]] = []
    demoted_entries: List[Dict[str, Any]] = []
    demoted: List[str] = []
    for entry in entries:
        model = _model_of(entry)
        provider = _provider_of(entry)
        if (
            model
            and provider in avoid
            and in_expensive_window(model, when, entry)
        ):
            demoted_entries.append(entry)
            if model not in demoted:
                demoted.append(model)
        else:
            kept.append(entry)

    promoted_entries: List[Dict[str, Any]] = []
    rest: List[Dict[str, Any]] = []
    promoted: List[str] = []
    for entry in kept + demoted_entries:
        model = _model_of(entry)
        if (
            model
            and model in prefer
            and not in_expensive_window(model, when, entry)
        ):
            promoted_entries.append(entry)
            if model not in promoted:
                promoted.append(model)
        else:
            rest.append(entry)

    return {
        "chain": promoted_entries + rest,
        "demoted": demoted,
        "promoted": promoted,
    }


def apply_time_cap(
    chain: List[Dict[str, Any]],
    max_multiplier: Any,
    when: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Drop elos priced above ``max_multiplier`` at ``when``.

    Returns ``{"chain": [...], "capped": [{"model", "multiplier"}, ...],
    "bypassed": bool}``. An elo is capped when its multiplier EXCEEDS the cap,
    so ``max_multiplier: 2.0`` still permits a 2.0x window; the cap is a
    ceiling, not a strict bound.

    This reuses the capability filter's bypass invariant exactly: if the cap
    would empty the chain, the cap is BYPASSED — ``chain`` is the ORIGINAL,
    ``bypassed`` is True and ``capped`` is retained as diagnostics so the trace
    can say which elos the cap objected to and why it gave way. A cost control
    must never be able to cause an outage: paying double for one request is a
    strictly better failure than having no route. This never returns an empty
    chain for a non-empty input.

    ``when`` None, or a ``max_multiplier`` that is not a usable number, means NO
    CAP. A cap below 1.0 is honoured literally (it would exclude every
    flat-priced elo, and the bypass then restores them); lint rejects such a
    value at the write gate rather than this module second-guessing the operator.
    """
    entries = list(chain or [])
    cap = _as_float(max_multiplier)
    if when is None or cap is None or not entries:
        return {"chain": entries, "capped": [], "bypassed": False}

    eligible: List[Dict[str, Any]] = []
    capped: List[Dict[str, Any]] = []
    survivors = 0
    for entry in entries:
        model = _model_of(entry)
        if not model:
            # Nothing to price and nothing to blame: an unattributable hop is
            # passed through untouched, exactly as the capability filter does.
            eligible.append(entry)
            continue
        multiplier = price_multiplier(model, when, entry)
        if multiplier - cap > _MULTIPLIER_EPSILON:
            capped.append({"model": model, "multiplier": multiplier})
        else:
            eligible.append(entry)
            survivors += 1

    # "Emptied" means no NAMED elo survived: a chain of unattributable hops is
    # not a route, so the cap gives way there too rather than claiming success.
    if capped and not survivors:
        return {"chain": entries, "capped": capped, "bypassed": True}
    return {"chain": eligible, "capped": capped, "bypassed": False}


# ---------------------------------------------------------------------------
# Public API — requirement derivation
# ---------------------------------------------------------------------------

def derive_requirements(
    features: Dict[str, Any],
    tier_requirements: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a requirements dict from the signal vector plus a tier floor.

    From ``features``:
      est_input_tokens > 0         -> min_context = ceil(est_input_tokens * 1.25)
      needs_vision True            -> vision = True
      needs_tools True             -> tool_calling = True
      needs_structured_output True -> structured_output = True

    ``tier_requirements`` is an explicit per-tier floor and wins on conflict;
    for ``min_context`` the MAXIMUM of the two is used. Only keys in
    REQUIREMENT_KEYS ever appear in the result.
    """
    result: Dict[str, Any] = {}
    feats = features if isinstance(features, dict) else {}

    est = _as_int(feats.get("est_input_tokens"))
    if est is not None and est > 0:
        result["min_context"] = _with_safety_margin(est)

    if feats.get("needs_vision"):
        result["vision"] = True
    if feats.get("needs_tools"):
        result["tool_calling"] = True
    if feats.get("needs_structured_output"):
        result["structured_output"] = True

    floor = tier_requirements if isinstance(tier_requirements, dict) else {}
    for key, value in floor.items():
        if key not in REQUIREMENT_KEYS:
            continue
        if key == "min_context":
            wanted = _as_int(value)
            if wanted is None:
                continue
            current = _as_int(result.get("min_context")) or 0
            result["min_context"] = max(current, wanted)
            continue
        result[key] = value

    return result


# ---------------------------------------------------------------------------
# Public API — upstream independence
# ---------------------------------------------------------------------------

def upstream_group(provider: str) -> str:
    """Return the shared-upstream group for ``provider``.

    "nous" and "openrouter" both return "openrouter": Nous Portal is a
    white-label reseller in front of OpenRouter at 80% of list price (360 of
    368 catalog entries carry pricing.original at ratio 0.80 and its stream
    emits the literal ": OPENROUTER PROCESSING" keep-alive). Every other
    provider returns itself, so it counts as its own rail.
    """
    if not isinstance(provider, str) or not provider:
        return ""
    return _UPSTREAM_GROUPS.get(provider.strip().lower(), provider)


def independent_rails(chain: List[Dict[str, Any]]) -> int:
    """Count distinct upstream groups in ``chain``.

    Lint uses this to warn when a tier's first hops share an upstream and so
    give no real redundancy. Entries without a usable provider contribute no
    rail — an unattributable hop is not evidence of independence.
    """
    groups: List[str] = []
    for entry in chain or []:
        if not isinstance(entry, dict):
            continue
        group = upstream_group(entry.get("provider"))
        if group and group not in groups:
            groups.append(group)
    return len(groups)


def registry_diagnostics() -> List[str]:
    """Return a list of registry defects — empty means clean.

    Diagnostics, never exceptions: a malformed registry entry must surface as a
    lint string rather than crash the router at import time.

    Every string is already shaped ``model '<id>': <defect>``, matching lint's
    own message style, so ``rules.lint_warnings()`` can append the list
    VERBATIM. That is the whole point of the function and the only way it earns
    its keep: a diagnostic nobody calls is a diagnostic that does not exist.
    Called with no arguments and no imports required — ``capabilities`` must
    never import ``rules`` (that would be an import cycle), so the direction of
    the dependency is ``rules -> capabilities``, and this is the seam.
    """
    problems: List[str] = []
    for model, entry in MODEL_CAPABILITIES.items():
        if not isinstance(entry, dict):
            problems.append(f"model '{model}': entry is not a mapping")
            continue
        for field in ("provider", "context_window", "billing_mode"):
            if field not in entry:
                problems.append(
                    f"model '{model}': missing required field '{field}'"
                )
        mode = entry.get("billing_mode")
        if mode is not None and mode not in BILLING_MODES:
            problems.append(f"model '{model}': unknown billing_mode '{mode}'")
        window = entry.get("context_window")
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            problems.append(
                f"model '{model}': context_window must be a positive int"
            )
        for field in ("vision", "tool_calling", "structured_output"):
            if not isinstance(entry.get(field), bool):
                problems.append(f"model '{model}': '{field}' must be a bool")
        problems.extend(price_window_diagnostics(model, entry.get("price_windows")))
        unknown_fields = set(entry) - _REGISTRY_FIELDS
        for field in sorted(unknown_fields):
            problems.append(f"model '{model}': unrecognized field '{field}'")
    return problems


def price_window_diagnostics(model: str, windows: Any) -> List[str]:
    """Return lint strings for one model's ``price_windows`` — [] means clean.

    Absent windows (None) are legal and clean: flat pricing at all hours. Shaped
    like :func:`registry_diagnostics` so lint can append either verbatim, and
    public because ``price_windows`` is also overridable per elo in router.yaml —
    the write gate needs to be able to check an operator's windows with the same
    rules the registry is held to, not a second implementation of them.

    Overlapping windows are an ERROR rather than a resolution rule: with two
    matching multipliers the winner would be an accident of registry order. A
    window with ``start >= end`` is an error too, which is what keeps
    wrap-around arithmetic out of every consumer — cross midnight with two
    entries instead.
    """
    if windows is None:
        return []
    if not isinstance(windows, list):
        return [f"model '{model}': price_windows must be a list"]

    problems: List[str] = []
    spans: List[Tuple[int, int, frozenset]] = []
    for index, window in enumerate(windows):
        label = f"model '{model}': price_windows entry {index}"
        if not isinstance(window, dict):
            problems.append(f"{label} is not a mapping")
            continue

        bounds = _hour_bounds(window.get("hours_utc"))
        if bounds is None:
            problems.append(
                f"{label} 'hours_utc' must be a [start, end) pair of ints "
                f"with 0 <= start < end <= {_HOURS_IN_DAY} "
                f"(cross midnight with two entries, never start > end)"
            )

        weekdays = window.get("weekdays")
        days = _weekday_set(weekdays)
        gate_ok = weekdays is None or days is not None
        if not gate_ok:
            problems.append(
                f"{label} 'weekdays' must be a non-empty list of ints "
                f"0..{_DAYS_IN_WEEK - 1} (0 = Monday)"
            )

        multiplier = _as_float(window.get("multiplier"))
        if multiplier is None or multiplier <= 0:
            problems.append(f"{label} 'multiplier' must be a positive number")

        # Only a well-formed window takes part in the overlap check: a window
        # already reported as malformed must not also produce a second,
        # confusing "entries overlap" error on top of its own.
        if bounds is not None and gate_ok:
            spans.append((bounds[0], bounds[1], days or _EVERY_DAY))

    for index, (start, end, days) in enumerate(spans):
        for other_start, other_end, other_days in spans[index + 1:]:
            if start < other_end and other_start < end and (days & other_days):
                problems.append(f"model '{model}': price_windows entries overlap")
                return problems
    return problems


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _declared_overrides(declared: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the recognized registry-field overrides inside ``declared``.

    Defensive: a non-dict `declared` (bad YAML) yields no overrides instead of
    raising, and unrecognized keys — ``model``, ``fallback``, ``time_policy``,
    ``time_cap``, a stray tuning knob — are dropped so the merged result keeps
    the shape of a registry entry. Recognized-ness is not the same as being a
    capability ASSERTION; see :func:`capabilities_for`.
    """
    if not isinstance(declared, dict):
        return {}
    return {
        key: value
        for key, value in declared.items()
        if key in _REGISTRY_FIELDS
    }


def _unsatisfiable_requirements(
    requirements: Any,
    entries: Sequence[Any],
) -> List[str]:
    """Return requirement keys no available model could ever satisfy.

    Only ``min_context`` can be unsatisfiable by construction today: a floor
    above every context window in the registry AND above every window declared
    in this chain cannot be met by anything the router can reach, however the
    chain is ordered. Declared windows are consulted so an operator who
    describes a bigger house model in YAML is not told their request is
    impossible when it is not.
    """
    if not isinstance(requirements, dict):
        return []
    needed = _as_int(requirements.get("min_context"))
    if needed is None or needed <= 0:
        return []

    ceiling = MAX_REGISTERED_CONTEXT
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        declared = _as_int(entry.get("context_window"))
        if declared is not None and declared > ceiling:
            ceiling = declared
    return ["min_context"] if needed > ceiling else []


def _model_of(entry: Any) -> str:
    """Return the model id on a chain entry, "" when there is not a usable one."""
    if not isinstance(entry, dict):
        return ""
    model = entry.get("model")
    return model if isinstance(model, str) and model else ""


def _provider_of(entry: Any) -> str:
    """Return the entry's provider name, normalized for comparison."""
    if not isinstance(entry, dict):
        return ""
    provider = entry.get("provider")
    if not isinstance(provider, str):
        return ""
    return provider.strip().lower()


def _provider_names(value: Any) -> frozenset:
    """Normalize a declared provider list for matching — case-insensitively.

    Provider names are operator-typed identifiers, so "DeepSeek" and "deepseek"
    must mean the same rail; :func:`upstream_group` normalizes the same way.
    """
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        item.strip().lower()
        for item in value
        if isinstance(item, str) and item.strip()
    )


def _model_names(value: Any) -> frozenset:
    """Normalize a declared model list for EXACT matching.

    Model ids are vendor-owned and can be case-sensitive (``MiniMax-M3``), so
    only surrounding whitespace is forgiven.
    """
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


# --- time -------------------------------------------------------------------

def _utc_parts(when: Any) -> Optional[Tuple[int, int]]:
    """Return ``(utc_hour, utc_weekday)`` for ``when``, or None for "no clock".

    None in, None out: ``when=None`` is the documented time-agnostic mode. An
    AWARE datetime is converted to UTC (a caller holding local time still gets
    the right window); a NAIVE one is assumed to be UTC already, which is what
    every edge in this repo passes. Anything that cannot supply both an hour and
    a weekday — a bare ``time``, a string, junk from a decoded trace — is
    treated as no clock at all rather than raising: a diagnostic surface must not
    be able to break the request path.
    """
    if when is None:
        return None
    try:
        if getattr(when, "tzinfo", None) is not None:
            when = when.astimezone(timezone.utc)
        hour = _as_int(getattr(when, "hour", None))
        weekday_of = getattr(when, "weekday", None)
        if hour is None or not callable(weekday_of):
            return None
        weekday = _as_int(weekday_of())
    except (AttributeError, TypeError, ValueError, OSError, OverflowError):
        return None
    if weekday is None:
        return None
    if not 0 <= hour < _HOURS_IN_DAY or not 0 <= weekday < _DAYS_IN_WEEK:
        return None
    return hour, weekday


def _windows_of(caps: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the usable ``price_windows`` entries on a merged capability dict."""
    windows = caps.get("price_windows")
    if not isinstance(windows, list):
        return []
    return [window for window in windows if isinstance(window, dict)]


def _multiplier_at(
    windows: List[Dict[str, Any]],
    hour: int,
    weekday: int,
) -> float:
    """Return the multiplier the first matching window declares, else 1.0.

    Overlaps are a lint error, so "first match" is a determinism guarantee for a
    malformed registry rather than a resolution policy. A window whose shape or
    multiplier is unusable is SKIPPED, not guessed at: the base rate is the safe
    reading, and :func:`price_window_diagnostics` reports the defect.
    """
    for window in windows:
        bounds = _hour_bounds(window.get("hours_utc"))
        if bounds is None or not bounds[0] <= hour < bounds[1]:
            continue
        weekdays = window.get("weekdays")
        days = _weekday_set(weekdays)
        if weekdays is not None and days is None:
            continue
        if days is not None and weekday not in days:
            continue
        multiplier = _as_float(window.get("multiplier"))
        if multiplier is None or multiplier <= 0:
            continue
        return multiplier
    return _FLAT_MULTIPLIER


def _hour_bounds(hours: Any) -> Optional[Tuple[int, int]]:
    """Validate ``hours_utc`` and return ``(start, end)`` for [start, end).

    None when the pair is missing, not a 2-sequence of ints, out of 0..24, or
    not strictly increasing. ``start > end`` is refused rather than interpreted
    as wrapping midnight — that is the invariant that keeps wrap-around
    arithmetic out of every consumer.
    """
    if isinstance(hours, (str, bytes)) or not isinstance(hours, (list, tuple)):
        return None
    if len(hours) != 2:
        return None
    start = _as_int(hours[0])
    end = _as_int(hours[1])
    if start is None or end is None:
        return None
    if not 0 <= start < end <= _HOURS_IN_DAY:
        return None
    return start, end


def _weekday_set(weekdays: Any) -> Optional[frozenset]:
    """Return the weekday gate as a set, or None for absent/unusable.

    Absent means every day, which the callers express by keeping the raw value
    around: None here with ``weekdays`` present means MALFORMED, and a malformed
    gate must not silently become "every day".
    """
    if weekdays is None:
        return None
    if isinstance(weekdays, (str, bytes)) or not isinstance(
        weekdays, (list, tuple, set, frozenset)
    ):
        return None
    days = set()
    for day in weekdays:
        value = _as_int(day)
        if value is None or not 0 <= value < _DAYS_IN_WEEK:
            return None
        days.add(value)
    return frozenset(days) if days else None


# --- cheapest_now -----------------------------------------------------------

def _by_cheapest_now(
    entries: List[Dict[str, Any]],
    when: datetime,
) -> List[Dict[str, Any]]:
    """Sort ``entries`` by effective output price at ``when``, stably."""
    return [
        entry
        for _, entry in sorted(
            ((_cheapest_now_key(entry, when, index), entry)
             for index, entry in enumerate(entries)),
            key=lambda pair: pair[0],
        )
    ]


def _cheapest_now_key(
    entry: Any,
    when: datetime,
    index: int,
) -> Tuple[int, int, float, int]:
    """Return ``(bucket, priced, output_price, declared_index)`` for ordering.

    ``bucket`` is the BILLING-MODE bucket (marginal cost first: plan credits off
    an allowance already bought, then free, then the dollar-priced rails, then
    undescribable). ``priced`` and ``output_price`` compare dollars inside that
    bucket only, so a list price never moves an elo out of the bucket its billing
    mode puts it in. The declared index is part of the key so "ties keep declared
    order" is a property of the key itself, not of the sort implementation.

    ``output_price`` is only ever read when ``priced`` says a real pair came back
    from :func:`effective_price`; the 0.0 in the unpriced branch is INERT padding
    that keeps the tuple one shape, never a claim that an unpublished price is
    zero, and no ``None`` is ever compared against a float.
    """
    model = _model_of(entry)
    declared = entry if isinstance(entry, dict) else None
    bucket = _billing_rank(model, declared)
    priced = effective_price(model, when, declared) if model else None
    if priced is None:
        return bucket, _UNPRICED_IN_BUCKET, 0.0, index
    return bucket, _PRICED_IN_BUCKET, priced[1], index


def _billing_rank(model: str, declared: Optional[Dict[str, Any]]) -> int:
    """Rank an elo's billing mode by MARGINAL cost; lower is cheaper.

    plan 0, free 1, subscription/metered 2, anything undescribable 3. This is the
    outer key of :func:`_cheapest_now_key` for EVERY elo, priced or not: a
    plan-covered hour is bought already and is spent in CREDITS, so its list
    price — if the registry even records one — is not what the next token costs,
    and credits and dollars are not commensurable without an exchange rate, which
    is the operator's policy call and not the router's.

    A ``subscription`` seat shares the METERED rank rather than leading it,
    because the rate the registry records for it IS the per-token rate that rail
    bills at (see the openai-codex note in the module docstring): same unit, so
    the dollar comparison is well-formed, and making it is what keeps a
    `cheapest_now` order hour-relative at all. :data:`_BILLING_RANK` carries the
    full argument.

    An elo :func:`capabilities_for` cannot describe at all ranks unknown, so it
    sorts LAST on a cost strategy: claiming an undescribed elo is the cheapest
    would be inventing the very number this whole layer refuses to invent. It is
    still never dropped — ordering is not eligibility.
    """
    caps = capabilities_for(model, declared) if model else None
    mode = caps.get("billing_mode") if isinstance(caps, dict) else None
    if not isinstance(mode, str):
        return _BILLING_RANK_UNKNOWN
    return _BILLING_RANK.get(mode.strip().lower(), _BILLING_RANK_UNKNOWN)


def _as_int(value: Any) -> Optional[int]:
    """Coerce ``value`` to int, or None when it is not a usable number.

    Bools are rejected on purpose — ``True`` is not a context size.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> Optional[float]:
    """Coerce ``value`` to float, or None when it is not a usable number.

    Bools are rejected on purpose — ``True`` is not a price and not a multiplier.
    None stays None all the way through :func:`effective_price`: an unpublished
    price must never become 0.0.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _with_safety_margin(tokens: int) -> int:
    """Return ceil(tokens * 1.25) using exact integer arithmetic.

    Integer math rather than float multiplication so million-token estimates
    cannot drift by a token on the boundary.
    """
    numerator = tokens * _CONTEXT_SAFETY_NUMERATOR
    return -(-numerator // _CONTEXT_SAFETY_DENOMINATOR)
