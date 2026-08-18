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

    def next_window_change(model, when=None, declared=None) -> Optional[Dict[str, Any]]
        # WHEN this model's multiplier next changes, and to what:
        #   {"hour": 6, "weekday": 0, "hours_ahead": 47, "multiplier": 2.0}
        # None when it never changes (flat-priced or unknown model, or no clock).
        # Powers the console's "peak ends in 2h" affordance and any future
        # deferral logic.

*Corrected in phase 2:* this returned a bare UTC hour, which cannot express the answer a
weekday-gated window produces. `next_window_change("glm-4.7", Saturday 07:00Z)` is hour 6 — read as
a bare hour that says "23 hours away" when the real answer is Monday 06:00, **47 hours** away, and
the whole point of the function is the countdown. So it returns a day-aware mapping: `hour` and
`weekday` (0 = Monday … 6 = Sunday, matching `datetime.weekday()`) label the instant the change
lands on, `hours_ahead` is the whole-hour countdown any consumer must read instead of subtracting
hours itself, and `multiplier` is the rate that takes effect then. Minutes are deliberately not
modelled — windows begin on the hour, so `hours_ahead` counts whole hour boundaries crossed and 23:59
is one hour from midnight. The search scans forward one full week, which is the whole period of the
schedule (`weekdays` is a window's only date dependence), so "never changes" is a proof rather than a
give-up; `None` also covers a flat-priced model, an unknown model, a registry whose windows cover
every hour at one multiplier, and no clock.

`service.liveness()` carries the whole mapping through per elo — a copy, so a caller mutating the
liveness payload cannot edit the registry's answer, but otherwise verbatim, because that surface does
not get to decide which of the registry's fields a console may see. `service._next_window_change` also
accepts the pre-`weekday` spelling (a bare `int`), because `service.py` is deployed by copy and can
land beside an older `capabilities.py`: the hour is preserved and `weekday`, `hours_ahead` and
`multiplier` are all reported as `None`. Defaulting the weekday to "today" there would manufacture
exactly the off-by-two-days error the richer shape was introduced to remove. Anything else — including
a `bool`, which is an `int` in Python and would otherwise render as hour 0 or 1 — is `None`.

and the two stage functions the plan applies, each returning its own diagnostics rather than only a
reordered list:

    def apply_time_cap(chain, max_multiplier, when=None) -> Dict[str, Any]
        # {"chain": [...], "capped": [{"model", "multiplier"}, ...],
        #  "cap_exempt": [{"model", "multiplier", "billing_mode"}, ...],
        #  "bypassed": bool}. No clock or no usable cap => no-op. An elo is
        # capped when its multiplier EXCEEDS the cap, so max_multiplier: 2.0
        # still permits a 2.0x window: a ceiling, not a strict bound. The
        # ceiling is in DOLLARS, so it removes only metered/subscription rails;
        # plan, free and undescribable rails are kept and named in cap_exempt.

    def apply_time_policy(chain, policy, when=None) -> Dict[str, Any]
        # {"chain": [...], "demoted": [model, ...], "promoted": [model, ...],
        #  "peak_priced": [model, ...]}. demoted/promoted are POSITION facts —
        # what actually moved; peak_priced is the PRICE fact — what avoid_peak
        # matched. The chain is always a PERMUTATION of the input — this stage
        # can never remove an elo. No clock => no-op, nothing moves and nothing
        # is reported. Provider names match case-insensitively; model ids match
        # EXACTLY, because a model id can be case-sensitive (`MiniMax-M3`).

    def price_window_diagnostics(model, windows) -> List[str]
        # Window validation as strings, reused by both the registry self-check
        # and the YAML lint — `price_windows` is overridable per elo, so the
        # same rules have to hold on both sides of the merge.

Two exported constants join `REQUIREMENT_KEYS` / `BILLING_MODES`: `FALLBACK_STRATEGIES` (the closed
strategy set, so lint rejects a typo at the write gate rather than degrading to sequential at
runtime) and `MAX_REGISTERED_CONTEXT` (the largest window in the registry, which is what lets the
capability filter report a `min_context` floor as *unsatisfiable* instead of as three coincidental
rejections). `unsatisfiable` travels from `filter_chain` into the plan, the trace and the JSON of
`explain()`; **no human-facing surface renders it yet** — see the companion spec's failure-mode (b) for
exactly how far it reaches and where it stops.

## A third fallback strategy

`fallback_strategy` gains `cheapest_now`, joining `sequential` and `random`:

    T2:
      model: glm-5.3
      provider: zai
      billing_mode: plan
      fallback_strategy: cheapest_now
      pin_primary: false

Semantics: rank on **marginal** price — what the next token adds to a bill — in two steps, an outer
bucket and an inner dollar comparison.

**Step 1, the bucket, is decided by `billing_mode` and by nothing else.** In particular it is *not*
decided by whether a dollar price happens to be published. Four buckets, in sort order
(`capabilities._BILLING_RANK`):

| bucket | `billing_mode` | why |
|---|---|---|
| 0 | `plan` | Spends **credits** off an allowance already bought, so no dollar figure describes what the next token costs. |
| 1 | `free` | Spends nothing either, but carries the reliability caveats the companion spec records, so it follows the plan bucket. |
| 2 | `subscription` **and** `metered`, together | Both are quoted **in dollars**: a seat publishes the per-token rate that rail bills at, so it stays commensurable with metered dollars. |
| 3 | no billing mode `capabilities_for` can describe | Claiming an undescribed rail is the cheapest would invent the very number this layer refuses to invent. |

**A rail is bucketed on the unit its price is quoted in, not on who holds the credential.** That is
the whole rule, and the two consequences an operator sizing a chain has to be able to predict are:

- `glm-4.7` is `billing_mode: plan` *and* carries `price_out: 2.20` (`4.40` inside zai's weekday
  peak). It lands in **bucket 0**, not in the dollar bucket. The plan covers it, so those dollars are
  its separately-purchasable metered list price and not what the operator pays for the next token.
  Bucketing it by the *presence* of a price would compare it in dollars nobody is paying and then
  spend metered tokens to avoid a cost that is already sunk — plan-covered `glm-4.7` would sort
  behind metered `mimo-v2.5` (0.28 out, 0.224 inside xiaomi's night discount).
- `glm-4.7-flash` is `billing_mode: free` with `price_in`/`price_out` of `0.0`. A declared `0.0` **is**
  a published price, so `effective_price` returns `(0.0, 0.0)` rather than None — but the bucket still
  comes from the billing mode, so it lands in **bucket 1**, ahead of every dollar-priced rail and
  behind the plan bucket. It is not sorted into the dollar bucket at position zero.

`subscription` sharing the metered bucket is the one line here worth arguing about, and it is
deliberate. Every `openai-codex` elo in this registry publishes the rate that rail bills at (the
registry's own note: "Listed prices are the metered short-context rates"), so a seat's cost is
denominated in dollars and the comparison against a metered rail is well-formed. Ranking a seat as
already-paid on billing mode alone would take the price comparison off the table for a whole chain:
it is exactly what would stop the shipped `T2` tail — `gpt-5.6-luna` at a flat 1.20 and
`deepseek-v4-flash` at 0.66 rising to 1.32 in its peak — from ever reordering by the hour, which
reduces the injected clock to decoration.

**Step 2, inside a bucket:** ascending effective **output** price at `when`, output-weighted because
output dominates agent cost. An elo with no published price cannot be converted into dollars without
inventing an exchange rate, so it sorts **behind** its priced bucket-mates, in declared order, never
as `0.0`. Ties keep declared order — the declared index is part of the sort key, so stability is a
property of the key rather than of the sort implementation, and an operator's declared preference
still expresses itself.

**The bucket-first shape is also what makes the hour's multiplier safe to apply at all.** A multiplier
scales the rail's *own* unit — dollars for `metered`/`subscription`, plan credits for `plan`, nothing
for `free` — so bucketing first means a multiplier is only ever applied to a price *inside* a
single-unit bucket and two units are never compared. That is one rule read three ways across this
layer: `cheapest_now` buckets before it compares, `time_cap` applies a dollar ceiling only to dollar
rails, and `time_policy` needs no unit rule at all because reordering spends nothing.

Inside the zai plan bucket that inner ordering happens to be the credit ordering too:
`glm-4.6v`/`glm-4.7`/`glm-5-turbo` cost 2.7/16/21 output credits in list-price order, and unpriced
`glm-5.3` — last, because it publishes no dollars — is in fact the most expensive of the four at 24
credits. That is a coincidence of this registry, not a rule the code relies on.

An unpriced model is never treated as `0.0`. `glm-5.3` sorts where a plan model belongs, on the
strength of its billing mode, and never because its price looked like zero.

**The limit, stated because it is the one an operator will hit.** A `plan` or `subscription` rail is
free at the margin only until its **quota** runs out, and quota state is nowhere in the registry —
nothing here can see how many plan credits or seat messages are left. `cheapest_now` therefore
optimises marginal *price*, not remaining *entitlement*, and relies on the existing breaker to route
around an exhausted rail: the key fails, the breaker opens it, and the next hop in this order is
tried. It is not a substitute for quota awareness, which would need a live per-key usage feed this
router does not have.

`pin_primary: true` keeps the declared primary at position 0 and applies `cheapest_now` to the tail
only, for the case where an operator wants a fixed first choice and merely wants the fallbacks ordered
sensibly.

With `when=None`, `cheapest_now` degrades to `sequential`. The clock is never read implicitly.

That degrade is **reported, not silent**: `plan_chain` returns the triple `strategy` (the one that
actually ran), `strategy_declared` (what the tier asked for) and `strategy_degraded`, plus a
`strategy_degraded_reason` string — so a `cheapest_now` tier planned without a clock is labelled
`sequential` in the plan and the trace rather than claiming an ordering it did not perform, and the
declared intent is still legible beside it. `random` without an rng behaves identically. A caller who
forgets to inject gets safe behaviour *and* is told.

## Per-tier time policy

For the common cases that do not warrant a hand-written rule:

    T2:
      time_policy:
        avoid_peak: [deepseek, zai]
        prefer: [gpt-5.6-luna]

`avoid_peak` demotes every elo belonging to a named provider to the end of the chain while **that elo**
is inside a `multiplier > 1.0` window, preserving relative order among the demoted. The window test is
deliberately per elo and not per provider: a same-provider elo with flat pricing costs no more at that
hour, so demoting it would degrade the route and save nothing. It never removes an elo: a demoted rail
is still better than no rail, and this must not be able to empty a chain — the same invariant the
capability filter holds. The returned chain is always a **permutation** of the input.

`prefer` promotes named models to the front when they are not themselves in an expensive window —
promoting an elo into its own peak would invert the intent. Promotion runs after demotion, and the two
can never fight over the same elo, since "expensive" is exactly the condition that demotes.

This stage is **unit-agnostic**, unlike `time_cap`: it only reorders, so it spends nothing in any unit
and cannot push a request onto another rail. A doubled plan-**credit** draw is worth stepping around
exactly as much as a doubled dollar one, and the stepped-around rail is still attemptable if everything
ahead of it fails.

### `demoted` means MOVED; `peak_priced` means CHARGING MORE

*Disambiguated in phase 2.* One field was carrying two different facts. The shipped return shape is
three lists, and they answer three different questions:

    {"chain": [...], "demoted": [model, ...], "promoted": [model, ...],
     "peak_priced": [model, ...]}

- **`peak_priced` — price.** Every elo `avoid_peak` named that is inside a `multiplier > 1.0` window at
  `when`, i.e. every elo the policy *matched*, whether or not moving it changed anything. A statement
  about the bill, not about the order. It names credit peaks and dollar peaks alike.
- **`demoted` — position.** Only the elos this call actually moved **later** in the chain. When the
  matched elos already occupy the trailing hops, the permutation is the identity and `demoted` is
  **empty** while `peak_priced` still names them.
- **`promoted` — position, mirrored.** Only the elos `prefer` actually moved **earlier**. A preferred
  model already sitting at the head is not reported, because nothing happened to it.

`demoted` and `promoted` are derived from the **returned chain**, not from the match, so the report and
the permutation cannot drift apart.

That split matters on the shipped policy, not just in principle: `avoid_peak: [deepseek, zai]` over
T3/T4's `[gpt-5.6-terra, deepseek-v4-pro, glm-5.3]` leaves the chain byte-identical at 07:00 UTC,
because demotion preserves relative order and the two named rails are already the trailing hops. As
shipped that reports `peak_priced: [deepseek-v4-pro, glm-5.3]` with `demoted: []`. Under the old single
field the console rendered "moved to the end — its rail is in an expensive window" for an order that
had not changed by one position: a decision described through the surface that displays it, disagreeing
with the path that ran it. A field named `demoted` cannot also mean "charging more" without lying to
whoever reads the word.

**Where these two new lists have to arrive, and how to check.** `cap_exempt` and `peak_priced` are
returned by the *stages*; the surface an operator actually reads is the plan, the trace and the console.
The requirement is agreement: whatever `apply_time_cap` and `apply_time_policy` report, `plan_chain`
must carry through unchanged (never re-derive — the stage owns the fact), `decision_log.CHAIN_PLAN_KEYS`
and `empty_chain_plan()` must list it so it survives into `routes.jsonl`, and the console must render
`capped` as removed and `cap_exempt` as *kept*. **`rules.plan_chain`'s returned key list is the
authority for what a reader of a trace will actually find** — check it rather than this paragraph;
propagating a diagnostic one frame short of the trace is how `unsatisfiable` came to exist in the plan
and nowhere a human can see it.

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

Any **dollar-billed** elo whose effective multiplier exceeds `max_multiplier` at the injected time is
treated as ineligible for that request.

**`max_multiplier` is a dollar ceiling, and as shipped it only applies to the rails whose price is
quoted in dollars.** *Corrected in phase 2:* the cap compared every multiplier against the ceiling
regardless of unit, so a plan rail's doubled **credit** draw was measured against a dollar limit and
the zero-marginal-dollar rail was evicted from T1 during zai's weekday peak — spending metered dollars
to dodge a cost that was already sunk, on the busiest trivial-work tier. The cap is the only stage that
*removes* a rail, so the unit is decisive here in a way it is not for `time_policy`, which merely
reorders. It therefore applies to exactly the `_BILLING_RANK` dollars bucket — `metered` and
`subscription` — and exempts the rest:

| `billing_mode` | over the ceiling ⇒ | why |
|---|---|---|
| `metered`, `subscription` | **capped** (removed) | the registry price is what the next token adds to an invoice |
| `plan` | **exempt**, kept | the multiplier doubles a credit draw and adds no dollars; a dollar ceiling has nothing to say about it |
| `free` | **exempt**, kept | a multiple of zero dollars is zero dollars |
| nothing describable | **exempt**, kept | unknown fails OPEN and is flagged; guessing a unit in order to DROP a rail is the one direction this module never fails in |

**What `max_multiplier: 1.5` on T1 does and does not do to a plan rail.** T1's primary is plan-covered
`glm-4.7`, whose multiplier is 2.0 at 06:00–10:00 UTC Monday–Friday.

- It does **not** evict it. No value of `max_multiplier` can: a dollar ceiling cannot remove a rail
  billed in credits. `glm-4.7` stays at the head of the chain during its peak.
- It **reports** the exemption rather than passing it off as a cap that simply did not fire:
  `cap_exempt: [{"model": "glm-4.7", "multiplier": 2.0, "billing_mode": "plan"}]`.
- It **does** still remove any `metered` or `subscription` hop over 1.5× — on T1 that is
  `gpt-5.6-luna` (flat subscription seat, 1.0×) and `mimo-v2.5` (metered, 1.0× or 0.8× in xiaomi's
  night discount), so nothing is capped there today; the ceiling governs every rail that can actually
  bill money and would bite the moment a windowed metered hop is added.
- An operator who wants a **credit** peak stepped around wants `time_policy`'s `avoid_peak`, which is
  unit-agnostic precisely because it demotes instead of removing, or a different tier. The cap is not
  the knob for that, and reading it as one is what produced the phase-2 defect.

Return shape: `{"chain": [...], "capped": [{model, multiplier}, ...], "cap_exempt": [{model,
multiplier, billing_mode}, ...], "bypassed": bool}`. The two lists never name the same elo, and every
`cap_exempt` elo is still in `chain`. An elo whose billing mode nothing can describe is reported with
`billing_mode: "unknown"` — the registry's own word, deliberately **outside** `BILLING_MODES`, and a
string rather than `null` because a console rendering "billing_mode: null" beside a multiplier reads it
as a mode it failed to fetch rather than as a mode nobody declared.

This reuses the capability filter's bypass invariant exactly: if the cap would empty the chain, the cap
is bypassed, the original chain is restored, and the trace carries `time_cap_bypassed: true`. A cost
control must never be able to cause an outage — the failure mode of paying double for one request is
strictly better than the failure mode of having no route. Unit-awareness makes that bypass **rarer**,
never more common: exempting a rail can only add survivors.

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
- **The cap's unit.** A plan rail at 2.0× under `max_multiplier: 1.5` stays in the chain and appears in
  `cap_exempt` with its billing mode; a metered rail at the same multiplier is capped. Asserted on both
  sides, because a cap that quietly stopped exempting would look exactly like a cap that worked.
- **`demoted` vs `peak_priced`.** The T3/T4 shape — matched rails already trailing — must report
  `peak_priced` non-empty **and** `demoted` empty, asserted against the returned chain rather than
  against the match, so a report that drifts from the permutation fails.

## Out of scope

Deferring a request until an off-peak window opens. It is the obvious next idea and it is a scheduling
concern, not a routing one: the router answers "which model, now", and a component that holds work has
to own queue durability, starvation and cancellation. Recorded here so it is chosen deliberately rather
than half-built. `next_window_change()` is the seam a future scheduler would read.

Cost-aware ordering across *plan credits* and *dollars* in one comparison. The two are not
commensurable without a policy decision about what a plan credit is worth, which is the operator's to
make, not the router's. `cheapest_now` therefore buckets every elo by `billing_mode` first — the unit
its price is quoted in — and compares dollars only *inside* the dollar bucket, rather than inventing
an exchange rate between a credit and a dollar.
