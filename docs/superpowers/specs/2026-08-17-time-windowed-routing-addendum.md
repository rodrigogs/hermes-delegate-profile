# Time-windowed routing — addendum to the conditional-routing design

Status: implemented in phase 2, layered on the conditional-routing work (which was corrected in the
same phase). This document stays the detailed authority on price windows, the injected clock and the
pricing API; where it and the code disagree, the code is the authority.
Companion document: `2026-08-17-conditional-routing-design.md` — its *Decision path* and *Lint
additions* sections now summarise the stages below, so a reader of either document sees the whole
current design.

## Why

Three of the providers this router uses now price by wall-clock window, and the swings are large
enough to dominate the routing decision:

| Provider | Expensive window (UTC) | Multiplier | Notes |
|---|---|---|---|
| `deepseek` | 01:00–04:00 and 06:00–10:00, every day | 2.0× | Official wording: "Off-peak rates are half of the peak rates. Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC (all other hours are off-peak)." 7h peak / 17h off-peak. Applies to input, cached input and output alike. |
| `zai` (Coding Plan credits) | 06:00–10:00, Monday–Friday only | 2.0× | Peak is 14:00–18:00 UTC+8 on weekdays; every other hour, including the whole weekend, bills at the 50% off-peak credit rate. |
| `xiaomi` | — (this one has a *cheap* window) | 0.8× during 16:00–00:00 UTC | Night discount, 00:00–08:00 UTC+8. |

Exactly which registry entries carry windows, so no reader has to infer it from the provider column:
`deepseek-v4-flash` and `deepseek-v4-pro`; the plan-covered zai models `glm-5.3`, `glm-5-turbo`,
`glm-4.7` and `glm-4.6v`; and `mimo-v2.5` and `mimo-v2.5-pro`. **No other provider in the registry
prices by the clock.** OpenAI Batch/Flex and Gemini Batch are 50% off but are separate *endpoints*,
not clock windows, and must not be modelled as windows — a window means the same call costs a
different amount at a different hour, which is not what a batch endpoint is.

The two primary rails share their expensive window: 06:00–10:00 UTC is peak for both `deepseek` and
`zai`. Unattended work scheduled overnight in the operator's timezone (UTC−03) lands inside it — the
06:00–10:00 UTC peak is 03:00–07:00 local — so cron traffic pays double on both rails simultaneously
while interactive daytime work pays the off-peak rate on both. Routing that ignores the clock
systematically overpays for exactly the traffic nobody is watching.

## The clock is injected, never read

`signals.py` and `rules.py` both document themselves as pure, deterministic, IO-free. Reading the wall
clock inside either would break that contract and make every routing test time-dependent and flaky.

The clock is therefore treated exactly like the `random.Random` instance introduced for the `random`
fallback strategy: it is a parameter, supplied by the caller at the edge.

- Production callers (`service.py`, `adapter.py`, `one_sidecar.py`) pass the real UTC time.
- Tests pass a fixed `datetime`.
- The console's Explain preview passes the current time and labels it, so an operator understands the
  plan they are reading is time-relative and would differ at another hour.

A `None` clock means "time-agnostic": every time-dependent feature is omitted from the feature vector
and every time-dependent ordering degrades to `sequential`. Nothing raises, nothing silently guesses.

## Two injected features

Added to the feature vector by the caller, not computed by `extract()`:

    utc_hour: int      # 0–23
    utc_weekday: int   # 0 = Monday … 6 = Sunday

These are ordinary features, so they compose with the existing closed operator set and need no new
operator — the same property that let context predicates reuse `gt`/`lt`. A rule can now say:

    - id: defer-heavy-work-off-peak
      when:
        utc_hour: { gte: 6, lt: 10 }
        est_input_tokens: { gt: 200000 }
      then:
        model: T1

Because a `when` clause whose field is absent from the feature vector never matches, a time-keyed rule
is inert when the clock is not supplied, rather than matching spuriously. That is the desired
fail-direction: no clock means no time-based routing, not arbitrary time-based routing.

That same absent-field semantics is why `signals.py` must *declare* these two names even though it
must never produce them. It exports `INJECTED_FEATURE_NAMES = {utc_hour, utc_weekday}` alongside
`EXTRACTED_FEATURE_NAMES`, and their union `KNOWN_FEATURE_NAMES` is what the field-name lint
validates a rule's `when` keys against. Without that, the lint check that catches a typo'd signal
(companion spec, *Lint additions*) would reject every legitimate time rule; with it, `utc_hour` is a
known field that is simply absent at runtime when no clock was injected.

## Price windows in the registry

Each registry entry gains an optional `price_windows` list. Absent means flat pricing at all hours.

    "deepseek-v4-pro": {
        ...,
        "price_in": 0.66,          # the off-peak (base) rate
        "price_out": 1.98,
        "price_windows": [
            {"hours_utc": [1, 4],  "multiplier": 2.0},
            {"hours_utc": [6, 10], "multiplier": 2.0},
        ],
    },
    "glm-5.3": {
        ...,
        "price_windows": [
            {"hours_utc": [6, 10], "weekdays": [0, 1, 2, 3, 4], "multiplier": 2.0},
        ],
    },
    "mimo-v2.5": {
        ...,
        "price_windows": [
            {"hours_utc": [16, 24], "multiplier": 0.8},
        ],
    },

`hours_utc` is a half-open `[start, end)` interval. An interval that wraps midnight is expressed as two
entries rather than allowing `start > end`, so no consumer has to implement wrap-around arithmetic.
`weekdays` is optional; absent means every day. Overlapping windows are a lint error, because the
resolution order between two matching multipliers would otherwise be undefined and silently
implementation-dependent.

`price_windows` is the **one** encoding. The pre-phase-2 registry expressed the deepseek peak as a
pair of fields on the entry, `peak_multiplier: 2.0` plus `peak_windows_utc: [[1, 4], [6, 10]]`, which
carried the same facts in a shape only deepseek used, could not express the zai weekday restriction
at all, and named the xiaomi discount a "peak". Those fields are replaced by `price_windows`; two
spellings of one concept is how a consumer ends up reading the one that was not updated. The
`weekdays` key is what makes the zai case expressible, and the multiplier being an arbitrary float
is what makes the xiaomi 0.8× a window rather than a special case.

Like `billing_mode` and the prices themselves, `price_windows` is **commercial metadata, not a
capability assertion**: declaring a window on an elo does not make that elo "known" to the registry
for the purposes of the capability filter's unknown-model guard (companion spec, failure-mode (a)).
Cost facts say nothing about what a model can do.

The stored `price_in` / `price_out` are always the **base** rate — the rate outside every declared
window. For `deepseek` and `zai` that means the off-peak rate, and peak is a 2.0× window. For `xiaomi`
it means the daytime rate, and the night discount is a 0.8× window. Encoding it any other way would
make "the price" ambiguous depending on which provider you asked about.

Models billed against a plan quota rather than dollars — `glm-5.3` today has no published per-token
price — carry `price_in: None` and still carry `price_windows`, because the credit multiplier halves
off-peak even though no dollar figure exists. Cost comparison for those models is expressed in plan
credits, and a `None` dollar price must never be coerced to `0.0`: a plan model is not free, its cost is
denominated differently, and treating it as free would make it win every cost comparison.

## New API surface

Added to `router/capabilities.py`:

    def price_multiplier(model, when=None, declared=None) -> float
        # 1.0 when no window matches, when `when` is None, or when the model is unknown.

    def effective_price(model, when=None, declared=None) -> Optional[Tuple[float, float]]
        # (price_in, price_out) with the multiplier applied. None when the model
        # publishes no per-token price — never (0.0, 0.0).

    def in_expensive_window(model, when=None, declared=None) -> bool
        # True only when a matching window has multiplier > 1.0.

    def next_window_change(model, when=None, declared=None) -> Optional[int]
        # UTC hour at which this model's multiplier next changes. None when it
        # never changes (flat-priced or unknown model, or no clock). Powers the
        # console's "peak ends in 2h" affordance and any future deferral logic.

and the two stage functions the plan applies, each returning its own diagnostics rather than only a
reordered list:

    def apply_time_cap(chain, max_multiplier, when=None) -> Dict[str, Any]
        # {"chain": [...], "capped": [{"model", "multiplier"}, ...],
        #  "bypassed": bool}. No clock or no usable cap => no-op. An elo is
        # capped when its multiplier EXCEEDS the cap, so max_multiplier: 2.0
        # still permits a 2.0x window: a ceiling, not a strict bound.

    def apply_time_policy(chain, policy, when=None) -> Dict[str, Any]
        # {"chain": [...], "demoted": [model, ...], "promoted": [model, ...]}.
        # The chain is always a PERMUTATION of the input — this stage can never
        # remove an elo. No clock => no-op, nothing moves and nothing is
        # reported. Provider names match case-insensitively; model ids match
        # EXACTLY, because a model id can be case-sensitive (`MiniMax-M3`).

    def price_window_diagnostics(model, windows) -> List[str]
        # Window validation as strings, reused by both the registry self-check
        # and the YAML lint — `price_windows` is overridable per elo, so the
        # same rules have to hold on both sides of the merge.

Two exported constants join `REQUIREMENT_KEYS` / `BILLING_MODES`: `FALLBACK_STRATEGIES` (the closed
strategy set, so lint rejects a typo at the write gate rather than degrading to sequential at
runtime) and `MAX_REGISTERED_CONTEXT` (the largest window in the registry, which is what lets the
capability filter report a `min_context` floor as *unsatisfiable* instead of as three coincidental
rejections).

## A third fallback strategy

`fallback_strategy` gains `cheapest_now`, joining `sequential` and `random`:

    T2:
      model: glm-5.3
      provider: zai
      billing_mode: plan
      fallback_strategy: cheapest_now
      pin_primary: false

Semantics: order the capability-eligible chain by ascending effective output price at the injected
time, output-weighted because output dominates agent cost. Ties keep declared order, so the ordering is
stable and an operator's declared preference still expresses itself.

Models with no dollar price cannot be converted into dollars without inventing an exchange rate, so
they are placed by `billing_mode` rank **around** the priced group rather than inside it — three
buckets, in this order:

1. **Already paid** — no price, `plan` or `subscription`. An hour already bought is the cheapest
   marginal token there is, so these sort ahead of every metered dollar figure.
2. **Priced** — compared in dollars at `when`, ascending effective output price.
3. **Unpriced** — no price and `free`, `metered`, or an unknown billing mode. `free` before
   `metered`, because an unpublished *metered* price is the one genuinely unbounded cost in the
   registry; a free rail costs nothing but carries the reliability caveats the companion spec
   records.

An unpriced model is never treated as `0.0`. `glm-5.3` sorts where a plan model belongs, on the
strength of its billing mode, and never because its price looked like zero.

`pin_primary: true` keeps the declared primary at position 0 and applies `cheapest_now` to the tail
only, for the case where an operator wants a fixed first choice and merely wants the fallbacks ordered
sensibly.

With `when=None`, `cheapest_now` degrades to `sequential`. The clock is never read implicitly.

That degrade is **reported, not silent**: `plan_chain` returns the *effective* strategy plus
`strategy_degraded: true`, so a `cheapest_now` tier planned without a clock is labelled `sequential`
in the plan and the trace rather than claiming an ordering it did not perform. `random` without an
rng behaves identically. A caller who forgets to inject gets safe behaviour *and* is told.

## Per-tier time policy

For the common cases that do not warrant a hand-written rule:

    T2:
      time_policy:
        avoid_peak: [deepseek, zai]
        prefer: [gpt-5.6-luna]

`avoid_peak` demotes every elo belonging to a named provider to the end of the chain while that
provider is inside a `multiplier > 1.0` window, preserving relative order among the demoted. It never
removes an elo: a demoted rail is still better than no rail, and this must not be able to empty a chain
— the same invariant the capability filter holds.

`prefer` promotes named models to the front when they are not themselves in an expensive window.

`time_policy` sits at one fixed position in the shipped order of operations:

    capability filter  ->  time_cap  ->  time_policy  ->  fallback_strategy ordering

and the resulting order is what the trace records. Stating that order matters. Reordering can never
precede filtering: a policy that promoted before the capability filter — or before the cap below —
would promote an elo that the next stage then removes, and the trace would show a promotion that
never took effect. Membership is decided first (capability filter, then `time_cap`), position second
(`time_policy`, then the strategy), so every position decision is made over the set that will
actually be attempted.

`time_cap` runs before `time_policy` rather than after it for the same reason one stage in: demoting
a rail the cap is about to remove is wasted work that still shows up in the trace as a decision.

## Time cap

Distinct from ordering, and the reason the operator asked for a "time cap": a tier may decline to use an
expensive rail at all during its peak, rather than merely preferring others.

    T3:
      time_cap:
        max_multiplier: 1.5

Any elo whose effective multiplier exceeds `max_multiplier` at the injected time is treated as
ineligible for that request. This reuses the capability filter's bypass invariant exactly: if the cap
would empty the chain, the cap is bypassed, the original chain is restored, and the trace carries
`time_cap_bypassed: true`. A cost control must never be able to cause an outage — the failure mode of
paying double for one request is strictly better than the failure mode of having no route.

It reuses the *diagnostics* half of that invariant too, which the companion spec had to correct for
the capability filter: a bypassing cap **retains** the per-elo findings — which model, which
multiplier, against which `max_multiplier` — beside `time_cap_bypassed: true`. The flag says the
requirements and the chain contradicted each other; only the per-elo numbers say *why*, and a bypass
is exactly when an operator needs to know whether their cap is wrong or their tier is.

`max_multiplier` defaults to absent, meaning no cap. It is deliberately expressed as a multiplier rather
than a dollar ceiling: a dollar ceiling would need a live price feed and would silently rot as list
prices change, while a multiplier is a property of the declared window and stays correct.

## Lint additions

Hard errors, blocking the write:

    tier '{tn}': 'fallback_strategy' must be one of cheapest_now, random, sequential
    tier '{tn}': 'time_cap.max_multiplier' must be a number >= 1.0
    tier '{tn}': 'time_policy.avoid_peak' must be a list of provider names
    tier '{tn}': 'time_policy.prefer' must be a list of model names
    rule '{rid}': 'when.utc_hour' must be bounded to 0..23
    rule '{rid}': 'when.utc_weekday' must be bounded to 0..6
    model '{model}': price_windows entries overlap

Warnings, informing the operator without blocking:

    tier '{tn}': every elo is in an expensive window at some hour — time_cap will bypass
    tier '{tn}': 'time_policy.avoid_peak' names provider '{p}', absent from this tier
    tier '{tn}': 'cheapest_now' with no priced elo degrades to billing_mode rank only

## Operator console

The console gains a single persistent affordance: the current UTC hour and, per rail, whether it is
inside an expensive window right now and when that changes. A tier using `cheapest_now` must show that
its displayed order is time-relative, because an order that silently differs from the declared YAML is
otherwise indistinguishable from a bug. The Explain panel reports the multiplier applied per elo, the
effective prices used for the comparison, and `time_cap_bypassed` when it fires.

## Testing notes

Every case passes a fixed `datetime`; no test reads a clock, and a test that did would be the bug
this design exists to prevent. The cases that carry the load:

- **Boundaries, per window.** `hours_utc` is half-open, so 01:00 is inside `[1, 4)` and 04:00 is
  outside; 00:00 is outside the xiaomi `[16, 24)` window and 16:00 is inside. Off-by-one here is a
  silent 2× on an hour of traffic.
- **The zai weekday restriction**, asserted on both sides: 07:00 Monday is peak, 07:00 Saturday is
  not. The whole weekend billing off-peak is the point of the `weekdays` key.
- **The overlap at 06:00–10:00 UTC**, where both primary rails are simultaneously at 2.0×. It is the
  case `cheapest_now` and `time_cap` most need to get right, because it is the window unattended
  overnight work lands in (03:00–07:00 in the operator's UTC−03).
- **`None` prices are never `0.0`.** `effective_price("glm-5.3", ...)` returns `None` at every hour,
  and `cheapest_now` must not sort it first. A plan model winning every cost comparison because its
  price is unpublished would be the worst possible failure of this feature.
- **`when=None` per stage:** no injected features, multiplier 1.0, no cap, no `time_policy`,
  `cheapest_now` → `sequential` with `strategy_degraded: true`.
- **Bypass:** a `time_cap` that would empty the chain restores the original chain, sets
  `time_cap_bypassed: true`, and still reports the per-elo multipliers.

## Out of scope

Deferring a request until an off-peak window opens. It is the obvious next idea and it is a scheduling
concern, not a routing one: the router answers "which model, now", and a component that holds work has
to own queue durability, starvation and cancellation. Recorded here so it is chosen deliberately rather
than half-built. `next_window_change()` is the seam a future scheduler would read.

Cost-aware ordering across *plan credits* and *dollars* in one comparison. The two are not
commensurable without a policy decision about what a plan credit is worth, which is the operator's to
make, not the router's. `cheapest_now` therefore compares dollars among priced models and falls back to
`billing_mode` rank rather than inventing an exchange rate.
