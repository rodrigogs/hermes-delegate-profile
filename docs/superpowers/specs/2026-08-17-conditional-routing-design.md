# Conditional Routing — Context, Capabilities, Fallback Strategy — Design (v2)

**Date:** 2026-08-17
**Status:** Shipped. Revised 2026-08-17 (phase 2) to match the code, after an adversarial review
found the document asserting contracts the implementation had since corrected. Where this
document and the code disagree, **the code is the authority** — every claim below was re-read
against `router/signals.py`, `router/rules.py`, `router/capabilities.py` and `router/adapter.py`.
**Repo:** `rodrigogs/hermes-delegate-profile` (extends the router shipped by the v1 design)
**Extends:** `docs/superpowers/specs/2026-07-21-capability-router-design.md` — additive; it
supersedes nothing. Every contract v1 declared (closed operator set, closed output set, closed
cause set, fail-closed lint, pure core) still holds at the end of this document.
**Companion:** `docs/superpowers/specs/2026-08-17-time-windowed-routing-addendum.md` — the time
layer. Its stages are summarised in *Decision path* and *Lint additions* below so a reader of one
document sees the whole current design; the addendum stays the detailed authority on price
windows, the injected clock, and the pricing API.

## Problem

The router that shipped decides on **task shape and nothing else**. `signals.extract(turn)`
returns `{char_len, has_code, size_lines, num_files, has_stacktrace, num_requirements,
verb_class, lang, keywords}`, and `rules.match()` runs first-match over `router.yaml` against
exactly those keys. That vector answers "what kind of work is this?" well — it is why
`trivial-mechanical-edit` can route a rename to T1 and `hard-verbs` can fail toward capability
on T4 without spending a classifier call. It cannot answer either of the two questions the
operator actually hits in daily use.

First, **it cannot say anything about context.** There is no signal for how much material a turn
drags in, so policy cannot express "this task needs a 400K window." The failure is not a routing
mistake that shows up in the decision log as a bad tier; it is a truncation or a provider-side
context error at the far end of a `delegate_profile` spawn, after the process boundary, on a
model the policy was perfectly happy with. `size_lines` is not a substitute: it is parsed out of
prose (`_infer_line_count` looks for "40 lines"/"200 LOC") and is `0` for the overwhelming
majority of turns, including the expensive ones. The cheapest, largest real context driver —
"refactor these 6 files", "read the entire repo and summarize" — is a *short* turn that
references a lot of material.

Second, **it cannot say anything about capability.** A turn carrying a screenshot must land on
an elo that does vision; a turn demanding schema-constrained JSON must land on one that does
structured output; agent work must land on one that can call tools at all. Today policy names a
`{model, provider}` pair and hopes. Nothing in the pipeline knows that one of the declared
fallback hops physically cannot serve the request, so the hop is tried, the provider rejects the
call, and the turn burns a spawn and a timeout to learn a fact that was static and knowable
before dispatch.

Third, **every tier walks its fallback list in one fixed order, forever.** `_resolve_tiers`
copies `tier.fallback` verbatim into the output and the executor tries hops top-down, so the
declared-first rail absorbs 100% of a tier's traffic until it fails. The shipped `router.yaml`
is the clean example: `T3` and `T4` are byte-identical — primary `gpt-5.6-terra@openai-codex`,
fallback `[glm-4.5-flash@zai, deepseek-v4-pro@deepseek]` — so every moderate *and* every hard
task in the profile's life queues behind one rail while two others sit idle, even when all three
would serve the request. Concentration is not just a throughput loss: it is how one rail's
rate limit becomes the router's rate limit, and it is how the breaker ends up carrying load it
was built to shed rather than to schedule.

## Confirmed intent (design contract)

- **Three additions, one seam each.** Context predicates are new *signals*. Capability
  filtering is a new *module*. Fallback ordering is a new *tier knob*. No shared state, no
  cross-talk; each is independently revertable.
- **No new rule operator.** Context predicates are ordinary numeric comparisons over ordinary
  signal fields. `gt/gte/lt/lte` already exist and already coerce with `float()`. The closed
  operator set `{eq, ne, in, nin, gt, gte, lt, lte, contains, starts_with, ends_with, matches}`
  survives this feature untouched, `matches` stays gated to `verb_class`, and adding a range or
  size operator is explicitly forbidden — `rules.lint()` rejects anything outside the set.
- **The pure core stays pure.** `router/rules.py` documents itself as "no IO, no state, no
  model calls. Deterministic." Randomized fallback ordering therefore takes an **injected**
  `random.Random`. Same inputs plus the same rng gives the same output, forever.
- **A capability filter may never cause an outage.** It is an optimization over an
  already-valid chain. Unknown facts fail open; a filter that empties a chain bypasses itself.
- **Availability beats correctness, loudly.** Every degrade path is observable: flags in the
  route trace, advisory strings in lint, never a silent drop.
- **Backward compatibility is a hard requirement, not a goal.** The live `router.yaml` on the
  box declares no strategy, no requirements and no capabilities. It must route
  byte-for-byte identically after this lands.

## Addition 1 — context predicates via new signals

`signals.extract()` gains four capability/context hints and one attachment list, keeping its
one-argument, depth-≤1, deterministic contract:

| signal | type | meaning |
|---|---|---|
| `est_input_tokens` | int | heuristic context need for the turn |
| `needs_vision` | bool | the turn implies visual input |
| `needs_structured_output` | bool | the turn implies JSON / schema-constrained output |
| `needs_tools` | bool | the turn implies acting, not only answering |
| `attachment_kinds` | list[str] | sorted kinds inferred from the text (`image`, `pdf`, `csv`, `log`, `diff`, `html`) |

`est_input_tokens` is a **heuristic, not a measurement**, and the module says so in a banner
comment. The base is `ceil(char_len / _CHARS_PER_TOKEN)` with `_CHARS_PER_TOKEN = 3.6` (the
working ratio for mixed prose-and-code English); on top of that the turn is charged
`_TOKENS_PER_REFERENCED_FILE = 4000` per inferred file and a one-time
`_WHOLE_REPO_TOKEN_ALLOWANCE = 40000` when a whole-repo marker fires. That structure exists
because what drives context need is the material a turn *references*, not the material it
*contains* — the "refactor these 6 files" case above. All three numbers are order-of-magnitude
knobs meant to be re-tuned from decision-log data.

**It is measured over the text the model will actually receive.** *Corrected in phase 2.* The
signal was extracted from the `goal` argument alone, while the prompt the executor sends is
`f"Context: {context}\n\nTask: {goal}"` — so a turn passing 120,000 characters of logs as `context`
derived `est_input_tokens ≈ 6`, no `min_context`, and a trivial-verb goal could still land on a
200K-window elo carrying a 33K-token prompt. A context signal blind to the largest input it exists
to measure is not a context signal. `adapter.route()` therefore takes the assembled prompt text and
extracts from that, falling back to the task string when no assembled text is supplied.

`needs_tools` is **bidirectional but asymmetric**, and the asymmetry is the whole design. Any
action verb (`_TOOL_MARKERS`) says True. Only a *pure question* — explanatory or interrogative
phrasing (`_QUESTION_MARKERS`) with **no** action verb anywhere and **no** file or path reference
(`_PATH_WORD_MARKERS` / `_PATH_LIKE_RE`) — says False. Everything else keeps the fail-closed
default `_TOOLS_DEFAULT = True`. Action evidence outranks question phrasing, so "read the file and
summarise it" is still a tool turn.

The asymmetry is not timidity, it is the error budget. This router only ever sees agent turns, so
a false negative silently routes agent work to a model that cannot call tools at all — the turn
then fails outright — while a false positive only narrows the eligible set slightly. So the
default stays True and only *positive evidence* of a pure question is allowed to lower it. A path
reference vetoes the question path for the same reason: a turn naming a concrete file is asking
about material only a tool can reach, however interrogative its phrasing.

*Corrected in phase 2.* Phase 1 described `needs_tools` as "defaults to True when no tool verb
matches", which shipped as an unconditional constant: `_detect_tools` had no negative half, so
`_TOOL_MARKERS` was dead code, `derive_requirements` injected `tool_calling: True` into **every**
requirements dict, a rule writing `needs_tools: { eq: false }` could never match, and any registry
elo with `tool_calling: False` (`z-ai/glm-5.2:free`) was dropped from every chain regardless of the
turn. The signal is now genuinely two-valued; the *default* is what stays biased.

`needs_vision` means the turn implies visual **input**, and it is detected in two tiers because it
is the most expensive signal in this vector to get wrong: it selects the vision rule, and the
capability filter then drops every elo that cannot see, which on this registry can collapse a
3-hop tier to a single hop on one subscription rail — the rail whose 429s motivated the fallback
work in the first place. Tier 1 is unambiguous (`screenshot`, an image extension, a design-tool
artefact) and fires on the marker alone. Tier 2 is the ambiguous nouns (`chart`, `diagram`,
`image`, `design`, `plot`), which fire **only** in proximity to an attachment, deictic or "look at"
cue: "plot a chart from the csv" *produces* a chart, "look at this chart" *supplies* one. Phase 1
matched the bare nouns and therefore stranded text-only work on the single vision rail; the
proximity patterns are compiled from the marker tables so adding a noun cannot forget a pattern.

`signals.py` also exports its own vocabulary, because a typo in a `when` field name is a silently
dead rule and the linter must be able to catch it. `EXTRACTED_FEATURE_NAMES` is exactly the key set
`extract()` returns; `INJECTED_FEATURE_NAMES` is the time pair the caller adds at the edge
(`utc_hour`, `utc_weekday` — see the addendum, and note `extract()` must never produce them because
it may never read a clock); `KNOWN_FEATURE_NAMES` is the union and is what field-name lint
validates against. One list, next to the code that builds the dict, asserted equal by a test —
duplicating it inside the linter is how the two would drift.

Because these are plain signal fields, a context rule needs no new machinery at all:

```yaml
  - id: huge-context-read
    status: experimental
    when:
      est_input_tokens: { gt: 200000 }
    then: { profile: coder, model: T5 }

  - id: screenshot-triage           # `contains` already walks list-valued signals
    status: experimental
    when:
      attachment_kinds: { contains: image }
    then: { profile: reviewer, model: T4 }
```

Two properties of the existing engine make this safe rather than merely convenient.
`_all_clauses_match` returns `False` the moment a `when` field is absent from `features`, so a
rule naming a signal an older `signals.py` does not emit simply never fires — it does not raise
and it does not match everything. And `_eval_clause` wraps its comparisons in
`except (TypeError, ValueError): return False`, so a garbage bound degrades to a dead row rather
than a crash in the request path. Adding feature keys is backward compatible by construction;
`lint` (below) closes the remaining gap by catching the dead row at write time instead of
letting it sit in the file looking alive.

## Addition 2 — capability filtering via a registry

A new pure module, `router/capabilities.py`, holds static facts about the models the operator
already routes to, and a filter that removes hops from a tier chain when they cannot satisfy the
request. It is a **table lookup, not a probe**: no IO, no network, no state, no clock. Liveness
stays where it already lives — manual bans and breaker cooldowns belong to `router/blocklist.py`
and are not duplicated here. The two axes are deliberately separate: the blocklist knows what is
*broken right now*, the registry knows what is *impossible in principle*.

`router/rules.py` imports it defensively and degrades to pre-capability behavior when it is not
importable:

```python
try:
    from router import capabilities as _caps
except ImportError:  # pragma: no cover - registry not installed
    _caps = None  # type: ignore[assignment]
```

### The `router/capabilities.py` contract

Every other module codes against exactly this surface. Nothing here may be renamed.

```python
MODEL_CAPABILITIES: Dict[str, Dict[str, Any]]
REQUIREMENT_KEYS: frozenset = frozenset({"min_context", "vision", "tool_calling", "structured_output"})
BILLING_MODES: frozenset = frozenset({"plan", "subscription", "metered", "free"})

def capabilities_for(model: str, declared: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]
    # Registry entry merged with per-elo `declared` overrides from router.yaml.
    # `declared` WINS over the registry (operators must be able to fix a stale
    # registry entry in YAML without a code change). Returns None — "unknown" —
    # when the model is absent from the registry AND `declared` carries no
    # genuine CAPABILITY assertion. Commercial/identity metadata does not make a
    # model known: `billing_mode`, `notes`, `price_in`/`price_out` and `provider`
    # are merged if present but are not, on their own, evidence that anyone has
    # vouched for what the model can do.

def satisfies(model: str, requirements: Dict[str, Any], declared: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]
    # Returns (ok, reason). reason is "" when ok. Closed reason set:
    #   "context_too_small", "no_vision", "no_tool_calling",
    #   "no_structured_output", "capability_unknown"
    # A capability that is absent/unknown NEVER produces False. Only a known
    # contradiction rejects. Unknown returns (True, "capability_unknown").

def filter_chain(chain: List[Dict[str, Any]], requirements: Dict[str, Any]) -> Dict[str, Any]
    # chain entries are {model, provider, ...optional declared capability keys...}.
    # Returns {"eligible": [...], "rejected": [...], "unknown": [...], "bypassed": bool}
    #   eligible  — order preserved, entries unchanged
    #   rejected  — each a shallow copy with "reject_reason" added
    #   unknown   — model ids that hit "capability_unknown" (they stay in eligible)
    #   bypassed  — True when filtering emptied the chain, in which case eligible
    #               is the ORIGINAL chain and `rejected` is RETAINED as diagnostics
    #               (every per-elo reason, unchanged). A capability filter must
    #               never return an empty chain and break routing — but it must
    #               still say what it could not satisfy. See failure-mode (b).
    #   unsatisfiable — requirement keys NO available model could ever meet
    #               (today only min_context, when the derived floor exceeds both
    #               MAX_REGISTERED_CONTEXT and every window in this chain).
    #               Informational, never changes eligibility: it separates "this
    #               request is pathological" from "these elos were rejected".

def order_chain(chain: List[Dict[str, Any]], strategy: str = "sequential",
                pin_primary: bool = True, rng: Optional[random.Random] = None) -> List[Dict[str, Any]]
    # "sequential" -> chain unchanged (returns a new list, never mutates input).
    # "random"     -> uniform shuffle via rng. pin_primary True keeps chain[0] at
    #                 index 0 and shuffles only the tail; False shuffles everything.
    #                 rng is REQUIRED for "random": when rng is None, fall back to
    #                 sequential rather than using global randomness, so purity holds.
    # Unknown strategy string -> sequential (fail-safe, never raise).
    # A third strategy, "cheapest_now", is added by the time layer and takes an
    # injected clock on exactly the same terms (see the addendum): no clock ->
    # sequential. Every degrade in this function lands on sequential.

def derive_requirements(features: Dict[str, Any], tier_requirements: Optional[Dict[str, Any]] = None) -> Dict[str, Any]
    # Build a requirements dict from the signal vector, unioned with an explicit
    # per-tier floor. Rules:
    #   est_input_tokens > 0  -> min_context = ceil(est_input_tokens * 1.25)
    #   needs_vision True     -> vision = True
    #   needs_tools True      -> tool_calling = True
    #   needs_structured_output True -> structured_output = True
    # tier_requirements is a true FLOOR: it can only ever TIGHTEN what the
    # signals derived, never relax it. min_context takes the MAXIMUM of the two;
    # a boolean key is OR-ed, so `vision: true` on the tier adds the requirement
    # and `vision: false` is a no-op rather than an override. Only keys in
    # REQUIREMENT_KEYS ever appear in the result.

def upstream_group(provider: str) -> str
    # Providers that share an upstream must not be counted as independent rails.
    # "nous" and "openrouter" both return "openrouter" (verified: Nous Portal is a
    # white-label reseller in front of OpenRouter at 80% of list price; 360 of 368
    # catalog entries carry pricing.original at ratio 0.80 and its stream emits the
    # literal ": OPENROUTER PROCESSING" keep-alive). Every other provider returns
    # itself.

def independent_rails(chain: List[Dict[str, Any]]) -> int
    # Count of distinct upstream_group values in the chain. Used by lint to warn
    # when a tier's first two hops share an upstream and give no real redundancy.

def registry_diagnostics() -> List[str]
    # Registry self-check: a malformed entry must surface as a lint string rather
    # than crash the router at import. Reported through lint_warnings(), so a
    # stale table is visible on an operator surface instead of only in tests.
```

The time layer adds four more pure functions to this same module — `price_multiplier`,
`effective_price`, `in_expensive_window`, `next_window_change` — each taking an **injected**
`when` and each degrading to "no window" when `when` is None. They are specified in the addendum,
not here, but they obey this module's contract verbatim: table lookup, no clock read, no state.

### Registry entry shape, and the one naming collision

A `MODEL_CAPABILITIES` entry is a flat mapping of capability facts:
`{context_window: int, vision: bool, tool_calling: bool, structured_output: bool, billing_mode: str}`.
Absent keys mean *unknown*, never *False* — that distinction is the whole of failure-mode (a)
below, and an entry that writes `vision: False` is making a claim the operator must be willing
to defend.

The requirement vocabulary and the capability vocabulary collide on exactly one key: a request
asks for `min_context` (a floor), a model offers `context_window` (a ceiling). `_resolve_tiers`
already harvests a tier's declared overrides as the subset of `REQUIREMENT_KEYS` present on the
tier, so YAML will present the context fact as `min_context`. Resolution, fixed here so the two
sides cannot drift: **`capabilities_for` treats a declared `min_context` as an alias for
`context_window`** and normalizes it during the merge. `satisfies` reads `context_window` only.
`REQUIREMENT_KEYS` stays the single vocabulary lint validates on the YAML side; the registry
keeps the honest name internally.

Registry entries are hand-maintained and will go stale — a provider silently doubles a window,
or an id gains vision. That is precisely why `declared` wins over the registry: an operator
fixes a wrong fact in `router.yaml`, through the lint-gated write path, with no code change and
no deploy. The registry is a convenience default, not an authority.

The `1.25` headroom on `min_context` is deliberate and worth defending: `est_input_tokens` is a
character-ratio estimate, the system prompt and the response also occupy the window, and a model
whose window is within 25% of the estimate is a coin flip. Rejecting a coin flip from the front
of a chain is cheap; discovering it after a spawn is not.

## Addition 3 — per-tier fallback strategy

A tier gains two knobs, `fallback_strategy: sequential | random` and `pin_primary: bool`, plus
an optional per-tier capability floor and optional declared capability facts:

```yaml
tiers:
  # Capability facts below are ILLUSTRATIVE. Declare nothing the operator has not verified.
  T4:
    model: gpt-5.6-terra
    provider: openai-codex
    fallback_strategy: random     # spread the tail; keep the known-best primary first
    pin_primary: true
    requirements:                 # per-tier FLOOR, unioned with the derived requirements
      min_context: 128000
    fallback:
      - { model: glm-4.7, provider: zai }
      - { model: deepseek-v4-pro, provider: deepseek, min_context: 128000 }
```

`_resolve_tiers` propagates them into the resolved output, and this is the part that keeps every
downstream caller branch-free: `fallback_strategy` (default `"sequential"`) and `pin_primary`
(default `True`) **always** land, while `billing_mode`, `requirements` (filtered to
`REQUIREMENT_KEYS`) and `declared_capabilities` appear only when the tier declares them.
Resolution is permissive on purpose — it applies defaults, it does not validate. `lint()` is the
gate that rejects a bad strategy string, so a stale config still routes while an operator's
*edit* is refused.

`pin_primary: true` is the honest default for a tier whose primary is genuinely the best model
and whose tail is a set of interchangeable insurance policies: it spreads failover load without
ever degrading the first attempt. `pin_primary: false` is for a tier whose hops are peers, where
the point *is* to spread the primary attempt itself. That flag is the only place in this design
where randomization can change which model serves a healthy request, which is why it is
per-tier, opt-in, and off by default.

**`requirements` is a floor in the strict sense: it may only tighten.** *Corrected in phase 2* —
the phase-1 text said "wins on conflict", and that shipped literally: a boolean key overwrote the
derived value, so `requirements: { vision: false }` **lowered** a signal-derived `vision: True` and
handed a screenshot to a blind model. A floor that can lower a requirement is not a floor. The
semantics now are:

- `min_context` — the **maximum** of the derived value and the declared one.
- boolean keys (`vision`, `tool_calling`, `structured_output`) — **OR-ed**. `true` adds the
  requirement; `false` is a no-op, not an override.
- keys outside `REQUIREMENT_KEYS` — dropped, exactly as before.

There is deliberately no YAML syntax for *relaxing* a derived requirement, and that is the point:
the signals derive a requirement because the turn implies it, so the honest way to disagree is to
fix the detector or to correct the elo's `declared` capability facts — both of which are visible,
reviewable and linted — not to punch a hole in the request's own requirement vector from a tier
knob three layers away.

Requirement **values** are validated by `lint()` as well as their keys. `min_context: "lots"` or
`min_context: true` reads as working policy in the file but coerces to None and silently disables
the floor, so it is a hard error, as is a non-boolean value on a boolean requirement key.

## Decision path

Every new stage slots **between rule matching and the terminal call**, and nothing before them
moves:

1. **`signals.extract(prompt_text or task)`** — one model-free pass over the text the model will
   actually receive; now also emits `est_input_tokens`, `needs_vision`,
   `needs_structured_output`, `needs_tools`, `attachment_kinds`. The adapter then merges the two
   injected clock features (`utc_hour`, `utc_weekday`) into the vector, or nothing at all when there
   is no clock.
2. **Blocklist pre-filter** — `Blocklist.is_blocked(requested_model, requested_provider)`.
   Unchanged, still fail-closed, still the only mutable-state consult. (In `adapter.route` this
   physically runs first, because a veto short-circuits with `cause=blocklist_veto` before there
   is any point extracting signals. The capability path does not touch that ordering.)
3. **`rules.match(features, blocked, rules, default, tiers)`** — first-match; `_resolve_tiers`
   turns a `Tn` alias into `{model, provider, fallback[], fallback_strategy, pin_primary,
   requirements?, billing_mode?, declared_capabilities?, time_policy?, time_cap?}` — the route
   (`model`/`provider`/`fallback`) plus the planning policy, always from the same tier.
4. **`capabilities.derive_requirements(features, tier_floor)`** — signal-derived requirements
   tightened by the tier's declared floor, `max` on `min_context`, OR on the booleans.
5. **`capabilities.filter_chain(chain, requirements)`** — over the assembled chain
   `[{model, provider}] + output["fallback"]`, i.e. the primary is `chain[0]`. The tier's
   `declared_capabilities` are the primary hop's overrides; each fallback row carries its own.
6. **`time_cap`** — drop hops whose price multiplier at the injected clock exceeds
   `time_cap.max_multiplier` (time layer; no cap declared or no clock ⇒ no-op).
7. **`time_policy`** — `avoid_peak` demotes, `prefer` promotes, neither ever removes (time layer;
   no policy or no clock ⇒ no-op).
8. **`capabilities.order_chain(survivors, strategy, pin_primary, rng, when)`** — apply the tier's
   strategy (`sequential` | `random` | `cheapest_now`) to whatever is left.
9. **Emit decision plus trace** — the ordered head becomes `output["model"]`/`["provider"]`, the
   ordered tail becomes `output["fallback"]`, and the whole plan is recorded.

**The exact final order of operations, as shipped:** capability filter → `time_cap` →
`time_policy` → strategy ordering. Two constraints fix it, and neither is stylistic:

- **Reordering can never precede filtering.** An order applied first, then filtered, produces a
  trace that shows a promotion or a shuffle over hops that no longer exist — the operator reads a
  decision the router did not make. Filter first and every subsequent stage operates on the set
  that will actually be attempted, so the recorded order *is* the attempted order. This is also
  why `time_policy` runs after the cap rather than before it: promoting an elo the cap is about to
  remove is the same lie one stage later.
- **Elimination before preference.** `filter_chain` and `time_cap` decide *membership*;
  `time_policy` and `fallback_strategy` decide *position*. Both eliminating stages carry the same
  bypass invariant (restore the original chain rather than return nothing), so ordering position
  last means position work is always done over a non-empty set.

Steps 4–8 are packaged as one pure function so no caller can perform them in the wrong order or
skip one:

```python
rules.plan_chain(output, features, *, rng=None, when=None) ->
    {chain, requirements, rejected, unknown, bypassed, strategy, strategy_degraded,
     independent_rails, ...time-layer keys (see addendum)}
```

`strategy` is the **effective** strategy — the one that was actually applied — not the declared
one, and `strategy_degraded` is True whenever the two differ. *Corrected in phase 2:* phase 1
reported the declared string unconditionally, so a caller that forgot to inject an rng got a
sequential chain labelled `"random"` (reachable from the CLI with no `--seed`), and the operator
surface confidently described an ordering that never happened. A degrade is only a safe default if
it is visible; the same applies to `cheapest_now` with no clock.

`plan_chain` degrades to the declared chain in declared order — `requirements {}`, `bypassed
False`, `independent_rails` as a distinct-provider count — in two cases: `_caps is None`, and
the registry raising `AttributeError/TypeError/ValueError/KeyError`. A stale or partially
written registry must never raise into the request path.

**One ordering constraint is load-bearing.** `plan_chain` must run *after* the session-pin floor
has settled the tier. `adapter._apply_session_floor` identifies a candidate's tier by looking
`output["model"]` up in the tiers table; if a shuffle had already moved a fallback hop into the
head, that lookup would miss, the function would return `(output, False)`, and the session's
upward-only ratchet would be silently unenforced. The pin decides *which tier*; the chain plan
decides *how that tier is attempted*. In that order.

The plan is persisted through the existing trace, additively:
`DecisionLog.record(..., chain_plan=...)`, bounded by `bound_chain_plan` (`rejected` truncated to
`MAX_REJECTED_ENTRIES = 8`, dropped rows counted in `rejected_truncated`, because
`routes.jsonl` is size-bounded and rotated and one pathological chain must not evict everybody
else's traces) and read back by `chain_plan_of`, which returns an empty default for a missing
key (old entries) and for a corrupt value, and never raises. `RouterService.explain()` surfaces
the same plan, so the console's Explain preview and production routing are the same code path —
the v1 rule that tooling can never drift from production behavior.

### The production decision path is `adapter.route()`

`adapter.route()` is the only decision path production takes (`__init__.py` → `_route_task`), and
`RouterService.explain()` is a display surface beside it. **Phase 1 wired `plan_chain` into
`explain()` and not into `adapter.route()`,** with the result that the entire feature was inert
where it mattered: `route()` called `rules.match()` and stopped, no `chain_plan` reached
`DecisionLog.record(...)` at any of its terminal sites, and the executor built its attempt list
from the **declared**, unfiltered order. A vision turn was routed to a blind model in production
while the console showed the blind model correctly rejected. Capability filtering, `pin_primary`,
`fallback_strategy` and the whole `chain_plan` replay pipeline were, in production, decoration.

As shipped, `route()` performs steps 4–9 on every path that produces a model — direct rule,
session pin, cache hit, classifier, blocklist veto and fail-safe alike — passes `chain_plan=` to
every `dlog.record(...)` call, and writes the ordered head and tail back into the routing result it
returns, so the executor's `targets` list is the planned chain rather than the declared one.

Structurally, that is enforced by a single terminal funnel rather than by discipline: every return
path goes through one local `finish(cause, output)` which plans, records and returns. Eight
`record(...)` sites each remembering to pass `chain_plan=` is exactly the arrangement that produced
the phase-1 defect, so there is now one site, and it runs last — after the session-pin floor, for
the reason stated below.

The adapter is also where the two impure inputs enter, and nowhere else: the per-turn
`random.Random` (seeded from the task text, with the seed written into the trace so the order that
ran can be replayed) and the wall clock (read once per turn and passed down as a value). One more
invariant lives here: a resolved output carries both a **route** (`model`, `provider`, `fallback`)
and a **planning policy** (`requirements`, `fallback_strategy`, `pin_primary`, `time_policy`,
`time_cap`, `declared_capabilities`) from its tier, and the two must always come from the *same*
tier. A session-pin floor or classifier answer that replaced the route while keeping the previous
tier's policy would plan the new chain under the old chain's rules, so replacing a route evicts the
stale policy keys with it.

Both surfaces read
the same `plan_chain`, with the same inputs, and the only intended difference is the rng seed:
`explain()` previews under the fixed `_PREVIEW_SEED = 0`, production injects a per-turn seed and
records it in the trace's `steps` entry for the chain stage.

That "only intended difference" is an assertion, so it is a test:
**`tests/router/test_adapter.py::test_route_and_explain_agree_on_the_chain_plan`** — the
two-surface-agreement test. It routes a task through `adapter.route()` and explains the same task
through the same config with the same injected rng, and asserts the two `chain_plan`s are equal,
including the head model, the tail order, `rejected` and `bypassed`. It is the guard that would
have failed on day one of phase 1, and it is the check that keeps the display surface honest as
the time layer adds stages to the same function.

## Failure-mode decisions

### (a) An unknown model capability FAILS OPEN, with a loud `capability_unknown` flag

`satisfies` returns `(True, "capability_unknown")` when a model is in neither the registry nor
`declared`, or when the specific fact a requirement asks about is absent. The unknown hop stays
in `eligible` and its id is added to the plan's `unknown` list.

**Only a genuine capability key makes a model "known".** *Corrected in phase 2.* The declared-key
harvest originally worked by exclusion — everything on a tier or hop mapping that was not routing
or identity (`model`, `provider`, `fallback`, `fallback_strategy`, `pin_primary`, `requirements`)
was handed to `capabilities_for` as a capability override, and `capabilities_for` returned non-None
whenever *any* override existed. `billing_mode` therefore made a model known. Since `router.yaml`'s
own convention mandates `billing_mode` on every elo, that silently disabled the unknown-model guard
**entirely**: a hop naming `gpt-9-does-not-exist` linted clean, produced no warning, and reported
`capabilities_known: True` to liveness. Removing the `billing_mode` line was what made the warning
appear — the exact inversion of what an operator would expect.

The fix is to stop inferring capability-ness by exclusion. A model is known only when a real
capability assertion exists for it: `context_window`/`min_context`, `vision`, `tool_calling`,
`structured_output`. Commercial and descriptive metadata — `billing_mode`, `price_in`, `price_out`,
`max_output`, `notes`, and the time layer's `price_windows` — is merged and used where it is
relevant, but it asserts nothing about what the model can *do* and therefore cannot stand in for
someone having verified that. Cost metadata is not a capability claim.

The alternative — dropping what we cannot vouch for — is worse in every dimension that matters
here. The registry is a hand-maintained table; the expected steady state is that *some* elo the
operator just started using is missing from it. Dropping unknowns makes the router's competence
a function of documentation freshness, and the failure compounds: a chain of three hops where
two are new drops to one, and a chain where all three are new drops to zero, which is
failure-mode (b) firing for no reason other than a stale table. Keeping the unknown hop costs at
most one provider-side rejection, and the system already has a purpose-built organ for that —
the breaker weights (`ttfb_stall`, `quota_exhausted`, `nonzero_exit`) and trips on real evidence
rather than on our ignorance. **Loud beats silent; available beats correct-and-dead.**

The flag is what makes fail-open honest rather than negligent: `unknown` in every trace entry,
and an advisory lint warning naming any policy model the registry does not know. The operator
sees the gap accumulating and fixes the table, instead of discovering it as a routing anomaly
weeks later.

This decision has one sharp edge, and it is worth naming: because `needs_tools` defaults to
True, `tool_calling: True` is requested on every turn that is not detected as a pure question —
which is most of them. A *wrong* `tool_calling: False`
in the registry therefore rejects a perfectly good elo from every chain it appears in. That is
the price of a closed vocabulary with an unknown state: absent is free, wrong is expensive. The
YAML override exists to make wrong cheap to fix, and (b) keeps wrong from being fatal.

### (b) A filter that would empty the chain BYPASSES itself

When filtering leaves zero eligible hops, `filter_chain` returns the **original** chain as
`eligible`, **retains `rejected`** — every per-elo reason, unchanged — and sets `bypassed: True`;
the flag propagates into the plan as `bypassed` and into the trace as
`capability_filter_bypassed`.

The reasoning is a strict ordering of harms. Before the filter existed, this chain routed. Every
hop in it was chosen by the operator and passed lint. The filter's entire value proposition is
"skip hops that will fail anyway" — it is an optimization, and an optimization that can convert
a working chain into no chain has negative expected value no matter how good its hit rate is,
because its failure mode is a *total* routing outage triggered by a data-quality problem in a
static table. Serving a request that might fail strictly dominates refusing to route at all, and
the components that decide "might fail" already exist downstream.

**The bypass keeps its diagnostics.** *Corrected in phase 2.* Phase 1 returned `rejected: []` on
the bypass path and defended it as honesty — nothing was ultimately rejected, so claiming otherwise
would send an operator hunting a decision that never happened. That argument is wrong on the facts
that matter. `bypassed: True` says *the requirements and the chain contradict each other*; it does
not say **which requirement nothing could meet**, and the per-elo `reject_reason` values are the
only place that information exists anywhere in the system. Discarding them at exactly the moment
the filter gives up leaves the operator with the console rendering "Capability filter bypassed — no
elo can meet these requirements", no elos, and no reasons: a dead end precisely when a diagnosis is
needed. The observed case is not exotic — a turn implying ~211 files derives a `min_context` above
the largest registered window (1.05M), so every hop is rejected as `context_too_small` and the
filter becomes a silent no-op for that request. That case is now also named in its own right:
`unsatisfiable` lists the requirement keys **no** available model could meet, so "this request is
pathological" is distinguishable from "these particular elos were rejected" without an operator
having to reconstruct it from three `context_too_small` reasons.

So the two facts are reported separately, because they are separate facts: `eligible` is the
**restored original chain** (routing is unaffected, which is the whole point of the bypass), and
`rejected` is the **evidence** (each hop with the reason it failed). The trace is not claiming those
hops were dropped — `bypassed: True` sitting beside them says they were not. `unknown` is retained
on the same terms, though on the bypass path it can only ever be empty, since it is populated from
entries that made it into `eligible`.

`time_cap` reuses this invariant verbatim, including the diagnostics: if the cap would empty the
chain, the original chain is restored and the trace carries `time_cap_bypassed: true` alongside the
per-elo multipliers that tripped it. A cost control must never be able to cause an outage, and it
must never be able to cause a silent one.

### (c) Random ordering takes an INJECTED `random.Random`

`order_chain(..., rng=None)` never touches module-level `random`. With `strategy="random"` and
`rng=None` it falls back to sequential rather than reaching for global randomness.

`router/rules.py`'s module docstring is a contract, not a comment: "Pure: no IO, no state, no
model calls. Deterministic." A global `random.shuffle` would break all three words at once —
hidden process-global state, an outcome that depends on whoever seeded it last, and a test suite
that either becomes flaky or has to monkeypatch a module to be stable. An injected rng preserves
the contract exactly: *same inputs plus the same rng gives the same output*, which is still a
total function, just one with an extra argument.

It also buys two concrete properties. The Explain preview is reproducible: `rules.explain()`
previews under a fixed `_PREVIEW_SEED = 0`, so an operator inspecting the same task twice sees
the same chain order and can reason about policy instead of chasing noise. And a route stays
replayable: production seeds a fresh `random.Random` per turn at the adapter edge and records
that seed in the trace's `steps` entry for the chain stage — not in `chain_plan`, whose shape is
fixed by `decision_log.CHAIN_PLAN_KEYS` / `empty_chain_plan()` — so a past decision can be
reconstructed exactly.
The `rng=None → sequential` fallback closes the last hole: a caller who forgets to inject cannot
accidentally make the router nondeterministic. Forgetting degrades behavior, never the contract —
and, since phase 2, forgetting is also *reported*, through `strategy_degraded`.

**The clock is injected on exactly these terms**, and for exactly these reasons. `signals.py` and
`rules.py` are pure, deterministic and IO-free; reading the wall clock inside either would break all
three words at once and make every routing test time-dependent and flaky. So `when` is a parameter
supplied at the adapter edge, `when=None` means time-agnostic (time features omitted, time-dependent
ordering degraded to sequential, nothing raised, nothing guessed), and tests pass a fixed
`datetime`. Two impure inputs, one pattern, one edge. See the addendum.

## Lint additions

`rules.lint()` keeps its exact meaning — **hard errors only**. `RouterService` runs it before
every write, so anything it returns blocks the write. Findings that describe a legitimate config
an operator may knowingly want to ship therefore cannot live there; they go to a sibling
`rules.lint_warnings(config) -> List[str]`, surfaced by `plan()`/`lint()` under a `warnings` key
and never gating a write. Two error sets that behave identically would have collapsed the
fail-closed gate into an advice channel.

New hard errors. The shipped wording lives in `rules._lint_tier_shapes` and the tests assert on it
verbatim; **the code is authoritative for the exact text**, and it does differ from phase-1's
rendering in this document in small ways (`must be one of sequential, random` rather than
`sequential|random`, and the strategy list now includes `cheapest_now`). Read this table as the set
of *checks*, not as the string table:

```
tier '{tn}' must be a mapping
tier '{tn}': 'fallback_strategy' must be one of cheapest_now, random, sequential
tier '{tn}': 'pin_primary' must be a boolean
tier '{tn}': 'fallback' must be a list
tier '{tn}': fallback[{i}] must be a mapping
tier '{tn}': fallback[{i}] missing 'model'
tier '{tn}': fallback[{i}] missing 'provider'
tier '{tn}': fallback[{i}] declares unknown capability key '{key}'
tier '{tn}': 'requirements' must be a mapping
tier '{tn}': 'requirements.{key}' not in closed requirement set
tier '{tn}': 'requirements.min_context' must be a positive integer
tier '{tn}': 'requirements.{key}' must be a boolean
tier '{tn}': 'min_context' must be a positive integer
tier '{tn}': declared capability '{key}' must be a boolean
tier '{tn}': 'billing_mode' must be one of plan|subscription|metered|free, found '{value}'
tier '{tn}': missing 'model'
tier '{tn}': missing 'provider'
tier '{tn}': 'time_cap.max_multiplier' must be a number >= 1.0
tier '{tn}': 'time_policy.avoid_peak' must be a list of provider names
tier '{tn}': 'time_policy.prefer' must be a list of model names
rule '{rid}': 'when.est_input_tokens' bound must be numeric, found '{value}'
rule '{rid}': 'when.{field}' is a boolean signal; only eq/ne/in/nin apply
rule '{rid}': 'when.{field}' is not a known signal
rule '{rid}': 'when.utc_hour' must be bounded to 0..23
rule '{rid}': 'when.utc_weekday' must be bounded to 0..6
default: 'model' references unknown tier '{tn}'
model '{model}': price_windows entries overlap
```

Three of these close holes phase 1 left open, and each one had the same shape — a config that
lints clean and routes nowhere real:

- **A tier's own `model`/`provider` were never required.** Only *fallback* hops were checked, while
  `_resolve_tiers` quietly filled a missing `model` from the alias itself (`tier.get("model", model)`),
  so deleting one line from `T2` produced a clean lint and a chain of `[{model: "T2", provider: ...}]`
  — 100% of standard traffic routed to a model named "T2". A console apply that drops a line must not
  be able to do that past a fail-closed gate.
- **`when.<field>` names were unvalidated,** so a typo was a silently dead rule: renaming
  `needs_vision` to `need_vision` linted clean and the vision rule simply stopped firing, because
  `_all_clauses_match` returns False for an absent feature. That absent-field semantics is what makes
  adding signals backward compatible, and it is exactly what makes a typo invisible; lint is the only
  place the two can be told apart. The check validates against `signals.KNOWN_FEATURE_NAMES` (plus
  the injected `blocked_model`), imported rather than duplicated so the linter cannot drift from the
  extractor.
- **`default.model` was never checked against the tier table,** although the identical check existed
  for rules and lint's own docstring advertised it. `default: { model: T9 }` linted clean and resolved
  every fall-through — i.e. every classifier route — to a literal model `"T9"`.

The rule-side value checks exist because `_eval_clause` swallows
`TypeError`/`ValueError` to stay crash-proof. `est_input_tokens: { gt: "200k" }` is not an
error at runtime — it is a row that never matches, which reads in the file as working policy.
Lint is where that becomes visible, and it is cheap to check because both new closed
vocabularies (`REQUIREMENT_KEYS`, `BILLING_MODES`) are read from the registry when it imports
and mirrored locally (`_FALLBACK_REQUIREMENT_KEYS`, `_FALLBACK_BILLING_MODES`) when it does not,
so the gate stays fail-closed either way.

New advisory warnings, exact strings:

```
tier '{tn}': fallback hops 1 and 2 share upstream '{group}' — not an independent rail
tier '{tn}': 'fallback_strategy: random' has no fallback tail to shuffle
tier '{tn}': 'pin_primary: false' has no effect with strategy 'sequential'
tier '{tn}': model '{model}' is unknown to the capability registry — it will fail open
tier '{tn}': every elo is in an expensive window at some hour — time_cap will bypass
tier '{tn}': 'time_policy.avoid_peak' names provider '{p}', absent from this tier
tier '{tn}': 'cheapest_now' with no priced elo degrades to billing_mode rank only
registry: {diagnostic}
```

The last row is `capabilities.registry_diagnostics()`, which phase 1 wrote and never called. Its
stated purpose — "a malformed registry entry must surface as a lint string rather than crash the
router" — is unreachable if nothing reads it, so `lint_warnings()` now folds it in. A registry
defect is exactly an advisory finding: the shipped table is clean, a defective row fails open
anyway, and blocking an operator's write on a code-side data bug they did not introduce would be
the wrong gate.

The first is exactly why `upstream_group`/`independent_rails` exist: two hops on `nous` and
`openrouter` look like redundancy in YAML and are one upstream in reality, so the second hop buys
nothing when the first fails for an upstream-side reason. It is a warning and not an error
because an operator may legitimately want a second entry on the same upstream (a different
model, a different price tier). Checked against the shipped `router.yaml`: `T3`/`T4` resolve to
`openai-codex`, `zai`, `deepseek` — three distinct groups, `independent_rails == 3`, no warning
today.

### Shadow detection reasons about intervals, not key sets

*Corrected in phase 2.* Phase 1's shadow check compared `when` **key sets**: identical key sets
meant shadowed, unconditionally, whatever the operators and values were. That made the headline
feature of this design impossible to express. Two disjoint context thresholds —
`est_input_tokens: { gt: 200000 }` followed by `est_input_tokens: { gt: 800000 }`, or a
`{ lt: 2000 }` fast path — share one key and shadow nothing, yet `lint()` rejected them, and
`lint()` is the write gate, so multi-threshold context routing could not be shipped through
`plan`/`apply` at all.

The check now reasons about the clause itself: for numeric fields it compares the **intervals** the
operators describe and reports a shadow only when the earlier rule's interval genuinely **contains**
the later one; for set/equality operators it reasons about **membership** the same way. A superset
of fields with identical shared clauses still shadows, as before — that part was always sound.

**The conservative direction is: do not report a shadow when containment cannot be decided.** This
is the opposite of the usual "when in doubt, warn", and the asymmetry of harms is why. A shadow is a
`lint()` hard error, so a *false* shadow **blocks a legitimate config through the write gate** — the
operator's apply is refused, and the only remedies are to abandon the rule or to edit the file
outside the guarded path, which is precisely the behaviour the gate exists to prevent. A *missed*
shadow leaves a dead row in the file — visible in `router.yaml`, visible in `explain()` as a rule
that never matches, visible in the decision log as a rule id with zero hits. One failure mode
strands the operator; the other leaves them a row they can see and delete. Undecidable containment
is therefore silence, not an error.

If a genuinely unreachable row should still be *surfaced*, that belongs in `lint_warnings()` where
it informs without blocking — the same split that keeps `lint()` a gate rather than an advice
channel.

## Backward compatibility

A `router.yaml` with no `fallback_strategy`, no `requirements` and no declared capabilities —
i.e. the file live on the box — must behave **exactly** as it does today. The defaults that make
that true, each one load-bearing:

- **`fallback_strategy` defaults to `"sequential"`**, and sequential returns the chain unchanged
  (as a new list, never mutating the input). Same primary, same tail, same order.
- **`pin_primary` defaults to `True`**, which is moot under sequential and stays correct if the
  strategy is later flipped.
- **`rng` defaults to `None`**, and `random` with no rng degrades to sequential. Nondeterminism
  cannot arrive by omission — only by an explicit tier knob plus an explicitly injected rng.
- **An unknown strategy string degrades to sequential** and never raises, so even a config that
  slipped past an older lint routes.
- **No `requirements` on a tier means `tier_floor = None`**; no `declared_capabilities` means
  registry-only. `derive_requirements` then yields only what the signals imply.
- **Signal-derived requirements are inert on ordinary turns.** A 2,000-character turn with no
  file references derives `min_context ≈ 700`; no real model window is under that, so nothing
  is rejected. Filtering only bites when the registry *knows* a window and that window is
  genuinely too small.
- **Unknown is fail-open and an emptied chain bypasses.** Composed, these two mean a wrong,
  incomplete, or entirely empty registry cannot change which models get tried. The worst case,
  `router/capabilities.py` missing altogether, is handled at the import: `_caps is None` and
  `plan_chain` returns the declared chain with `requirements {}` and `bypassed False` — the
  pre-capability behavior, bit for bit.
- **Existing rules are untouched** because `_all_clauses_match` iterates the *rule's* `when`
  keys, not the feature dict. New signal keys are invisible to a rule that does not name them.
- **`VALID_CAUSES` is unchanged.** `capability_unknown` and `capability_filter_bypassed` are
  trace *flags*, not causes. This is not cosmetic: `DecisionLog.record` coerces an unrecognized
  cause to `"fail_safe_strong"`, so smuggling a new cause string in would relabel healthy routes
  as fail-safe and corrupt every `cause=` count on the dashboard. Capability filtering changes
  *which chain* a decision produces, never *why* the tier was chosen.
- **`chain_plan` is an additive keyword.** Entries already in `routes.jsonl` have no
  `chain_plan` key; `chain_plan_of` returns the empty default for missing and corrupt values
  alike, so the console keeps rendering pre-feature traces.
- **New lint checks only fire on keys today's file does not contain**, so the live config still
  lints clean and the guarded write path stays open. Advisory findings are deliberately not
  errors precisely so no currently-valid file becomes unwritable. The two exceptions are
  deliberate and are bug fixes, not compatibility breaks: a tier missing its own `model`/`provider`
  and a `default.model` naming a nonexistent tier were always broken configs; the shipped
  `router.yaml` trips neither.
- **`when` defaults to `None`**, and every time-dependent stage is a no-op without it: no
  `utc_hour`/`utc_weekday` in the feature vector (so a time-keyed rule is inert rather than
  arbitrary), no multiplier, no cap, no `time_policy`, and `cheapest_now` degrading to sequential.
  The clock cannot arrive by omission any more than randomness can.

## Deliberately out of scope

- **Cost-aware ordering — superseded by the time layer, with the objection preserved.** This
  document originally ruled it out on the grounds that ranking by price needs a live price feed the
  box does not have, and that a stale price table fails *invisibly*: it does not error, it just
  spends the wrong money on every turn until someone reads a bill. That objection still stands and
  is the reason the time layer is scoped the way it is. What it adds is not a price feed but
  **declared windows**: a multiplier is a property of the provider's published schedule, so
  `cheapest_now` and `time_cap` compare *relative* prices under a clock the caller injects, and
  `time_cap` is expressed as a multiplier rather than a dollar ceiling precisely because a dollar
  ceiling would rot silently while a multiplier stays correct. `billing_mode` is no longer a pure
  label — `cheapest_now` uses its rank to order models with **no** dollar price — but it is still
  never converted into money. See the addendum; a plan credit and a dollar are not commensurable
  without an operator policy decision, and a `None` price is never coerced to `0.0`.
- **Onboarding new providers.** `capabilities.py` describes models the operator already routes
  to. Adding a rail is credentials, install, and liveness work; it is not routing policy, and
  conflating the two would make the registry a deploy artifact instead of a lookup table.
- **Benchmarking unmeasured models.** The registry holds *capability facts* — window size,
  vision, tool calling, structured output — not quality judgments. Which tier a model belongs in
  stays the operator's call plus the classifier's rubric, exactly as in v1. A filter that
  removes an elo because we think it is *worse* is a different feature with a different failure
  mode, and it would need the benchmark harness this design does not have.

## Testing strategy

- **`capabilities.py`, pure unit tests:** the closed reason set exercised one branch per reason;
  absent-vs-`False` asymmetry (absent → `(True, "capability_unknown")`, `False` → reject);
  `filter_chain` bypass returning the *original* chain **while retaining `rejected`**; `order_chain`
  non-mutation of its input, `pin_primary` both ways, `rng=None` and an unknown strategy both
  landing on sequential, and identical output for two `random.Random(k)` with the same `k`;
  `derive_requirements` taking the max of signal-derived and tier-floor `min_context` and
  emitting nothing outside `REQUIREMENT_KEYS`; `upstream_group` collapsing `nous`/`openrouter`
  and `independent_rails` counting groups rather than providers.
- **`rules.plan_chain` degrade paths:** `_caps` patched to `None`, and to a stub that raises,
  both asserted to return the declared chain in declared order.
- **Lint:** adversarial configs asserting the exact strings above, plus the invariant that the
  shipped `router.yaml` yields zero errors and zero new warnings.
- **Trace:** `chain_plan_of` against a pre-feature entry, a corrupt value, and a `rejected` list
  longer than `MAX_REJECTED_ENTRIES` (truncated to 8, `rejected_truncated` correct).
- **Service:** `explain()` on the same task twice returning an identical chain order under the
  fixed preview seed; the write path still refusing a config that trips a new hard error.
- **Adapter — the two surfaces must agree.** `adapter.route()` is tested directly, not only through
  `explain()`: that the returned head/tail is the *planned* chain and not the declared one, that
  `chain_plan` reaches every `dlog.record(...)` site, and — the load-bearing one —
  `test_route_and_explain_agree_on_the_chain_plan`, which asserts route and explain produce the same
  plan for the same task under the same injected rng. A test that exercises only the display surface
  cannot detect a feature that is wired only into the display surface.
- **Requirements are a floor:** a tier declaring `vision: false` against a signal-derived
  `vision: true` must still require vision; `min_context` still takes the max in both directions.
- **Shadow detection:** disjoint numeric thresholds on one field lint clean in both orders, a
  genuinely containing interval is reported, and an undecidable pair is silent.
- **Time layer:** see the addendum's own testing notes; every case passes a fixed `datetime`, and
  the `when=None` degrade is asserted per stage.

## Provenance

Grounded in the shipped v1 design (`2026-07-21-capability-router-design.md`) and a read of the
live seams it produced: `router/signals.py` (feature vector), `router/rules.py`
(`_all_clauses_match` absent-field semantics, `_eval_clause` exception swallowing, `_resolve_tiers`
tier propagation, `lint` as the fail-closed gate), `router/adapter.py` (stage order, session-pin
tier lookup), `router/decision_log.py` (closed cause set and its coercion), and
`router/service.py` (lint-gated, optimistic-concurrency write path). The Nous/OpenRouter
upstream identity is a verified finding, not an assumption: 360 of 368 catalog entries carry
`pricing.original` at a 0.80 ratio and the stream emits OpenRouter's literal
`": OPENROUTER PROCESSING"` keep-alive.

## Retrospective — what phase 1 got wrong

Phase 1 shipped green: 686 tests passing, `router.yaml` linting clean, the console rendering
filtered chains with reasons. An adversarial review then found fifteen defects, and this document
asserted several of them as settled contracts. The corrections are marked *Corrected in phase 2*
above. Three lessons are worth keeping, in order of sharpness.

**1. A feature verified only through the surface that displays it is not verified.** This is the
whole of the phase-1 failure. `plan_chain` was wired into `explain()` and not into
`adapter.route()`, so every test, every console screenshot and every CLI check agreed with the
design — and production routed the declared, unfiltered chain, sending vision turns to a blind
model while the console showed that model correctly rejected. The suite was green because it asked
the diagnostic surface what it thought, and the diagnostic surface was right. Nothing asked the
decision path. The generalisation: when a feature has an execution surface and a display surface,
the test that matters is the one asserting they **agree** —
`tests/router/test_adapter.py::test_route_and_explain_agree_on_the_chain_plan` — and it must
exercise the execution surface directly, or the display surface will keep vouching for itself.
Coverage of `explain()` measured our belief, not the router's behaviour.

**2. A diagnostic is worth least when it is easiest to drop.** The bypass returned `rejected: []`,
the shadow detector treated undecidable as shadowed, `registry_diagnostics()` had no caller, and
`--seed` was silently ignored. Each was locally defensible and each removed information at the exact
moment an operator needed it. Degrading is fine; degrading quietly is not — which is also why
`plan_chain` now reports the **effective** strategy plus `strategy_degraded` instead of the declared
one.

**3. A convention in the config file can silently disable a guard in the code.** `billing_mode`
counted as a declared capability, and `router.yaml` mandates `billing_mode` on every elo, so the
unknown-model guard could never fire — and removing a line from the config was what made the warning
appear. Deriving "is this a capability assertion?" by *exclusion* let a documentation convention
reach into a safety check. Closed sets have to be enumerated positively.

A spec that disagrees with the shipped code is worse than no spec, because the next reader trusts
it. That is why this document was revised against the code rather than the code against the
document.
