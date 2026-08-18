# Capability Router Console — Information Design (Operate)

The durable contract for this console. It governs **what earns a place on screen**
and how state is signalled. This is an Operate surface: an operator's control
room, not a dashboard demo. Fewer elements, each carrying a fact.

The previous version of this file mandated a kicker + title + subtitle on every
panel and a card around every block. That rule produced 53 cards, 21 subtitles
and three competing health rollups, and the operator's verdict was "feio e muito
poluído — pouco objetivo". The rules below exist to make that outcome
impossible.

## 1. The three questions

The console answers exactly three questions, in this order. Every screen belongs
to one of them; anything serving none of them does not ship.

| Question | Screen | The one thing it must show |
|---|---|---|
| Is it healthy? | **Health** | which models can be routed to, right now |
| How does it route? | **Pipeline** | where a task lands, and the ordered policy that put it there |
| What did it decide? | **Routes** | recent real decisions, replayable step by step |

Blocklist and Compaction are subordinate detail, not peers: they live inside
Health and Pipeline respectively unless they carry an active condition.

## 2. Rules of subtraction

Applied in order. When two rules conflict, the earlier one wins.

1. **Render nothing for nothing.** No empty card, no dashed placeholder box, no
   row that announces its own emptiness. A section with no data is absent, or a
   single muted line — never a framed void.
2. **One authority per fact.** Health is signalled by the Health tab's dot and the
   model list. Write mode is signalled by the Edit control. Never a second chip
   repeating a state something else already owns; two sources that can disagree
   are worse than none. The header's "checked HH:MM" is not an exception: it
   reports THIS CONSOLE'S last read, which nothing else on screen can say.
3. **A subtitle must carry a fact the title cannot imply.** "Circuit breaker /
   Cooldown state and last failure kind" is one fact written twice. Prose that
   explains the console's own internals ("metric cards below are the canonical
   numbers") never ships — that belongs in this file.
4. **No card without a reason.** A card exists to group things that are read
   together. One list, one table, or one control does not need a frame.
5. **Translate values.** `true` is not a metric. Booleans become words
   ("enabled"), enums become their operator meaning, timestamps become relative
   time. Raw JSON appears only where the operator is editing JSON.
6. **No invented vocabulary.** PRODUCT.md owns the domain words: profile, model,
   provider/rail, tier, rule, classifier, fail-safe, blocklist, breaker,
   decision. "worst-of-N", "five-state liveness", "posture", "endpoint pending"
   are ours, not the domain's — use plain words instead ("Health", "Models",
   "not implemented").
7. **The console never reports on itself.** An endpoint ledger, a proxy note, a
   count of which routes answered — diagnostics about the console belong behind
   a deliberate action, never in a primary viewport.

## 3. Signalling

**Colour is split by MEANING, not by hue.** This console lives inside Hermes One,
where the accent IS the skin's identity and the host uses exactly one at a time.
So the old rule ("colour means state, and nothing else, which is why `--accent` is
paper white") is restated in two halves that cannot be confused:

- **The skin's accent marks WHERE YOU ARE and WHAT YOU PICKED.** The selected
  tab's underline (the host's own 20px x 2px bar), the focus ring, the armed Edit
  mode, the selected scope, the decision being replayed, and the line a probed
  task matched. It never reports a condition.
- **The semantic four report CONDITION, and only condition.** Alive `--ok`,
  degraded `--warn`, quota `--info`, dead or refused `--bad`; unknown is `--muted`.
  The same colour means the same thing on every screen.

A gold underline therefore can never be read as health, and a green dot can never
be read as selection.

**A state colour has two forms, and the difference is measured.** The host's
`--success/--warning/--error/--info` are authored for FILLS. As text on their own
skin's background they bottom out at 1.38:1 (`--info`, neon-paint/light) against a
4.5:1 floor — measured across every palette in the host's style.css that declares
a full set, both polarities. So:

- a **dot** takes the raw hue, which is what makes it identifiable as green or
  amber, plus a 1px inset edge on `--line-strong` so a 6px circle is locatable
  even where the hue is nearly invisible;
- a **word** takes `--ok-text` / `--warn-text` / `--bad-text` / `--info-text`,
  each `color-mix(in srgb, <hue> 45%, var(--host-text))`. 45% is the measured
  answer: it clears 4.5:1 in every skin (worst 5.08:1) where 50% does not (4.44:1),
  and mixing toward the skin's TEXT rather than away from its background is what
  makes it darken on parchment and lighten on navy automatically.

Colour is never the only channel: every dot sits beside the state in words.

**Colour budget per viewport.** Only a refusal is painted in the decision sheet's
destination column. "Goes to the classifier" is a KIND of destination, not a
condition, and it is already named once on the Stage 1 heading — colouring it too
put five cyan destinations against a gold underline, an amber finding and two reds
in one viewport, which is what the host's guide forbids.

- **Tab** = destination + its live state: a condition dot, plus a count when a
  count is meaningful (rules, recorded decisions, active bans). The selected tab
  additionally wears the accent, and those two signals never collide because one
  is a dot and the other is an underline.
- **Cause colour** in a decision: deterministic rule `--ok-text`, classifier
  `--info-text`, refusal (veto / fail-safe) `--bad-text`.
- **A count without its window is a lie.** Hit counts on the sheet are taken over
  the traces currently on disk, so the stage line names that window ("counts over
  the last 47 decisions, since 6h ago"). Without it, a percentage measured before
  a fix reads as a claim about right now — which is exactly how the sheet came to
  advertise "34% fail-safe" hours after the cause was fixed.
- **Sequences are read down one spine.** The policy is an ordered first-match
  table and a trace is a short ordered path, so both are drawn as numbered
  vertical lines against a single spine — never as a node canvas. Fifteen tiny
  boxes to highlight three of them makes the reader hunt for the answer.
- **Compare down a column.** Destination and hit count are fixed columns, so the
  eye can scan them without re-finding them on each line.
- **A sequence that has no order gets no ordinals and no spine.** A tier whose
  `fallback_strategy` is `sequential` IS a list with a first and a next, so it is
  numbered down the spine like everything else here. A `random` chain is a SET:
  it loses the ordinals and the spine and wraps across the line instead of
  stacking down it, because a numbered random chain is a lie about which elo runs
  first. `pin_primary` is the honest middle — hop 1 is genuinely first, so it is
  drawn ordered, and the tail is drawn as the set it is. A strategy word the
  router does not know degrades to sequential (as `capabilities.order_chain`
  does) and SAYS SO; silently drawing a typo as a random set describes routing
  that never happens.
- **`pin_primary` is THREE-valued, and unknown never reads as pinned.** `true`
  and `false` are what a tier declares; absent from a CHAIN PLAN means nobody
  reported it, and then no hop is drawn as first and the panel says the ordering
  is unreported. Absent from the POLICY is different and is read as the router's
  documented default (`rules._pin_primary_of` → True), because that file is the
  one the router itself reads. The console once read absence as `true`
  everywhere, so a tier configured `random` + `pin_primary: false` printed "the
  primary stays first" and drew hop 1 as ordinal 1 over a chain whose index 0 had
  been shuffled — the console stating the opposite of what ran.
- **`cheapest_now` is ordered, and its order carries the hour it is true of.**
  Ascending effective price IS a sequence, so it keeps its ordinals; but the
  order differs from the declared YAML, and an order that differs for an unstated
  reason is indistinguishable from a bug. So it is labelled TIME-RELATIVE with
  the hour it was computed at. With no clock it degrades to sequential and says
  that instead — an order labelled "cheapest" that is really declared order is
  the most expensive kind of wrong this console can be.

## 3a. The three families of predicate

A rule's `when` block now mixes predicates about three different things, and
reading one as another is how a rule gets "fixed" by changing a number that was
never about what the operator thought:

  SHAPE       what the task looks like — `verb_class`, `has_code`, `size_lines`,
              `num_files`, `num_requirements`, `char_len`, `lang`, `keywords`
  CONTEXT     how much it needs held — `est_input_tokens`
  CAPABILITY  what the model must be able to do — `needs_vision`, `needs_tools`,
              `needs_structured_output`, `attachment_kinds`

Each clause renders as one chip in a real `<ul>`, so two conditions are never
announced as one string. **The family is never a hue.** The four semantic colours
report CONDITION and the accent reports SELECTION; a fifth colour meaning
"family" would break both. So a family is marked the way this console makes its
third text step — in TYPE and PLANE, plus its own word inside the chip:

  shape       no label, transparent plane, `--muted`      "verb is hard"
  context     CONTEXT label, `--surface-raised`, `--text` "over 400,000 tokens"
  capability  NEEDS label, `--line-strong` hairline       "vision"

The label is a word, not a border, so the family survives being read aloud. A
field this console has not learned is SHAPE — what every signal was before
context and capability arrived — never an invented fourth family.

## 3b. What an elo has to say for itself

An elo is a tier member: the primary or a fallback hop. Four facts, and each is
one an operator acts on:

- **model** and **rail**, in mono, because they are copied into a shell.
- **billing mode**, in router.yaml's own words (plan / subscription / metered /
  free) — a badge, not a state, so it takes the 11px metadata treatment and no
  hue. A MISSING mode does take `--warn-text`: a request whose rail is
  undeclared cannot be costed. A mode this console has not learned renders as
  written rather than being swallowed.
- **context window**, written to be compared and not counted — `1M`, `200K`.
  Unknown is never rendered as zero, which would claim the elo can hold nothing.
- **unverified**, when nothing published this elo's capabilities. It routes
  UNCHECKED: the filter can neither clear it nor reject it, and that is the
  operator's to know, not a blank cell.
- **what it costs at this hour** — the multiplier applied and the two effective
  prices the comparison ran on (`2× peak · $1.32 in / $3.96 out per 1M`), read
  from the BASE rate the registry stores times the window's multiplier. Rendered
  where it is news: inside a window, or wherever the ORDER is price-relative and
  an operator has to be able to check it. A model with no published per-token
  price is NEVER rendered as `$0` — it is billed in plan or subscription credits,
  and a zero would both say the opposite of the truth and make it look like the
  cheapest thing on screen. A genuine `0.00` (a free rail) is a price and stays.
- **why the time policy moved it**, when it did: `capped`, `demoted` and
  `promoted` in words that name the consequence ("moved to the end — deepseek is
  in an expensive window, so it is tried only if everything ahead of it fails").
  A flag alone describes a position, not a reason. `demoted` is the only field
  that may say "moved": it is derived from the permutation
  `apply_time_policy` RETURNED, and an empty `demoted` therefore means nothing
  moved and must render as nothing.

**Redundancy is counted in upstreams, not in vendor names.** Nous Portal resells
OpenRouter, so a chain hopping from one to the other survives nothing. The tier
head reports independent rails against hops, and a chain whose FIRST TWO hops
share an upstream is called out by name with the reorder to make — that is the
pair redundancy actually uses.

**Conditions an operator must never discover by accident**, all surfaced before
anything else in a chain plan, each naming what the router will do AND what the
operator can do:

- `unsatisfiable` — the derived requirement NO elo could ever meet: a `min_context`
  floor above every context window the router can reach. This is the CAUSE of the
  bypass below whenever both fire, so it is said FIRST and the bypass line then
  carries only what the router did — otherwise "no elo can meet these requirements"
  is stated twice and "add an elo that qualifies" is offered when nothing could
  qualify. It names the requirement and the ceiling it was measured against ("the
  widest context window the router can reach holds 1,050,000, needs 1,050,002"),
  read from the same two places `_unsatisfiable_requirements` reads: the registry,
  and the windows this chain declares. When `GET /capabilities` never answered the
  ceiling is scoped to the chain IN WORDS, because a ceiling taken from three
  declared hops is not "every model the router can reach"; when nothing published
  one, the requirement is named without a number, since a zero would claim the
  router can hold nothing. It takes the AMBER line, not the red one — nothing was
  refused, the router kept routing, and red is reserved for a control giving way.
  It is independent of `bypassed` (the filter reports it with `bypassed` false when
  a fail-open unknown hop stays eligible), and it is the whole difference between
  "this request is pathological" and "these particular elos were rejected" — which
  three coincidental `context_too_small` reasons carry only to an operator who
  reconstructs it.
- `bypassed` — the filter disqualified every elo and overrode itself to keep
  routing alive, so the task runs on a model that cannot meet its own stated
  requirements. When the bypass reports no per-elo reasons, that is said too: an
  absent Dropped section beside "bypassed" otherwise reads as "nothing was
  dropped", the opposite of what happened.
- `peak_priced` — PRICE, and a different fact from `demoted`'s POSITION: every elo
  `avoid_peak` matched inside a dearer window, whether or not moving it changed
  anything. The shipped T3/T4 chain is the case the split exists for —
  `avoid_peak: [deepseek, zai]` over hops that are already trailing leaves the order
  byte-identical, so `demoted` is EMPTY while `peak_priced` names both. That has to
  read as "these bill double and this chain cannot step around them", never as a
  policy that failed to fire, so the line states the bill first and then how far the
  reorder got: everything moved, nothing moved because the matches are already at
  the back, or — the mixed case — it names only what could NOT go further back
  (`peak_priced` minus `demoted`), because which hop moved is already said on that
  hop's own row. Amber, the colour that means paying double needs attention
  (§3c). The recovery — another hour, or a rail that is cheap at this one — is named
  once per viewport, so it is omitted when the `time_cap_bypassed` line above
  already carries it. Each elo's own multiplier and effective prices stay where
  §3b puts them, on the elo; this line's subject is the tier's policy.
- `time_cap_bypassed` — a COST control was dropped, deliberately, because
  enforcing it would have left the task with no rail. Same volume as the filter
  bypass, because it is the same class of event: the request pays peak price, and
  an operator who learns that from an invoice has been failed by this console.
- `strategy_degraded` — the declared strategy did not run (no clock, or no random
  source). The plan reports the declared word either way, so the console must say
  which one happened.
- `independent_rails` of 1 on a POST-FILTER chain — this task has no fallback at
  all, however wide the declared chain is. It is invisible in router.yaml because
  it is a property of the task, and it looks exactly like a healthy tier unless
  said out loud.
- `capability_unknown` — eligible by assumption.

**A rejection is words, never an enum.** `context_too_small` reads "its context
window is smaller than this task needs", and where the numbers exist they follow
it — "holds 200K, needs 500K" — because "too small" alone is not fixable. The
reasons themselves stay `--muted`: the DROPPED heading already says these were
refused, and painting every one of them put three reds beside the one real
refusal in the same viewport.

## 3c. The clock is one line, and it is injected

Three rails price by wall-clock window and the swing is 2x, which is large enough
to decide where a task goes. So the console carries exactly ONE persistent
affordance for it — a line above the warnings, on every screen, because the hour
is not any one screen's subject: it changes what all three of them mean.

- **Both clocks, each labelled.** The windows are published in UTC; the operator
  lives in UTC−03. A bare "07:14" is therefore wrong by exactly the difference
  between "the peak is on now" and "the peak is three hours away", so the line
  reads `07:14 UTC` beside `04:14 local (UTC−03:00)` and never one alone.
- **Per rail: what it costs now, and until when.** `deepseek 2× peak until 10:00
  UTC`. The multiplier is in the words, so colour is not the only channel; only
  the EXPENSIVE state takes a hue (`--warn-text`, the colour that means "this
  needs your attention", because paying double is exactly that). A cheap window
  stays muted: a discount needs no alarm, and green here would collide with
  alive. A flat rail is not listed — nothing to report is not a row.
- **The clock is INJECTED, never read**, in every function but one. `nowUtc()` is
  the only place a wall clock is touched; everything else takes `{hour, weekday}`
  as a parameter, with weekday 0 = Monday (the router's convention, not JS's) and
  `null` meaning time-agnostic — multiplier 1.0, no window reported, no guessing.
  This is `rules.py`'s own contract and it is here for the same reason: a
  formatter that read the clock itself would make every rendering test pass at
  05:00 UTC and fail at 07:00.
- **The declared windows live here, not behind a fetch.** `GET /capabilities` is
  an optional read (§7), and a blank clock line is indistinguishable on screen
  from "off-peak right now" — which is the confusion the line exists to remove.
  So the three rails' windows are a table in the file, mirroring the registry, and
  a registry entry that DOES publish `price_windows` wins over it, exactly as
  `declared` wins over the registry elsewhere.
- **A plan's own hour outranks the console's.** When a chain plan reports the hour
  it was planned against, every price under it is read at that hour and the panel
  says `planned at 03:00 UTC`. When it reports none, the console's hour is used —
  which is the hour this line already names, so there is still one authority.

## 4. Writing is a mode, and it is off by default

The earlier version of this section called it a lock and dressed it in `--amber`.
Both were wrong and both are fixed: the sidecar has never heard of a client-side
lock (every write is already gated by the per-extension token-v1 secret, the host's
CSRF token, a loopback-only bind and an optimistic `base_hash` that answers 409 on
drift), and amber is the colour that means "the ROUTER needs your attention" — an
operator choosing to edit is not the router degrading.

- One control owns write mode: **Edit / Done**. It names the action pressing it
  performs, not the state it is in. Armed, it reads as pressed in the host's own
  selected idiom — accent wash, accent text, accent-tinted border — because that
  is a selection, not a condition.
- Reading (the default): no write control is present in the DOM at all for the
  inspector — not disabled, absent — and the JSON twisty's Apply/Revert, which are
  static markup, are explicitly disabled.
- Editing: write controls appear, and the Pipeline note says the surface is armed.
- Every write still goes through plan → apply → confirm/revert with the
  `base_hash` guard. The UI never invents a second path. A no-op apply is refused,
  because the server snapshots to `.bak` before every write and applying nothing
  would destroy the only thing Revert can restore. A second click while a write is
  in flight is refused, because two overlapping plan+apply pairs race on the same
  `base_hash`.
- A write the environment cannot perform (no CSRF token, because the console is
  standalone rather than inside the Hermes One page) is refused up front with
  that reason.
- The committing button is the only FILLED element on a screen, and it is filled
  with the skin's accent over `--accent-fg` — the host's own primary-button rule.

## 5. Layout

- **The host's own panel shape, not a masthead of our own.** A `.view-head`
  (18px/600 sans title left, actions right, one hairline under it, min-height 41px
  and padding `8px clamp(14px,2.4vw,32px)` — all measured off the running shell's
  `.main-view-header`), then a `.tabs` strip in the host's nav idiom, then a
  scrolling `.body` that is the host's `.main-view-body`.
- **NO WORDMARK.** "ROUTER / HERMES ONE" is gone. The host's rail already says
  which surface you are on; a mark repeating it spent 9 characters and a whole
  type voice the shell never uses on information the operator already had. The
  header carries the view's name and its actions and nothing else.
- **The screen's question is answered in its first line.** Health opens with the
  rollup of the model set ("all 5 reachable"), Pipeline with the probe, Routes
  with what the log holds. There is no second heading repeating the panel title.
- **On a phone the clock yields, never the name of the surface.** At 390px the
  header's three items claimed 232px and truncated the title to "Capability R…";
  dropping the "checked HH:MM" text returns the 99px that fits it whole. The dot
  stays, and the words come back at any width when there is something to report
  (no read yet, or an unreachable sidecar).
- **One column under that header.** Three destinations do not earn a permanent
  vertical rail, and a host that already owns the left edge must never face a
  second one. Measure is capped (`min(1180px, 100%)`) so a line of prose stays
  readable on a wide monitor.
- One screen fills its width; nothing letterboxes.
- Density: 11–16px inside a group, 30–38px between groups. More space above a
  heading than below it.

## 6. Tokens (do not invent, and do not hard-code)

**There is no palette in this file's console.** Every colour token reads a
`--host-*` custom property that `hermes-theme-bridge.js` forwards from the running
shell's resolved theme, and the hex after the comma is a FAIL-SAFE — what the
console looks like when the shell cannot be read at all. The reason is measured:
the host ships 21 skins x light/dark, so a copied palette is wrong in 20 of 21,
and in any light skin this console was a black rectangle inside a parchment shell.

  PLANES  `--bg` <- `--host-bg` · `--surface` <- `--host-surface` ·
          `--surface-raised` <- `--host-surface-subtle` ·
          `--surface-hover` <- `--host-hover-bg`
  LINES   `--line` <- `--host-border` · `--line-strong` <- `--host-border2`
  TEXT    `--text` <- `--host-text` · `--muted` <- `--host-muted` ·
          `--faint` = `--host-muted` (the SAME value — see below)
  ACCENT  `--accent` <- `--host-accent` · `--accent-text` <- `--host-accent-text` ·
          `--accent-bg`/`-strong` <- the host's own washes ·
          `--accent-fg` <- `--host-accent-fg`, falling back to NEAR-BLACK. The
          host declares its accent foreground only under `:root.dark`, so in a
          light skin the fallback is what lands on the one filled button; against
          this file's own paper-white fail-safe accent a `#fff` fallback measured
          1.1:1, i.e. an invisible label on the only committing control there is.
  STATE   `--ok`/`--warn`/`--bad`/`--info` <- `--host-success`/`-warning`/`-error`/
          `-info`, plus the four `-text` forms and four `-bg` washes derived in an
          `@supports (color: color-mix(...))` block
  TYPE    `--sans` <- `--host-font-ui` · `--mono` <- `--host-font-mono` ·
          `--t-label` 11px <- `--host-font-size-xs` ·
          `--t-small` 12px <- `--host-font-size-sm` ·
          `--t-body` 14px <- `--host-font-size-md` · `--t-value` 16px ·
          `--t-head` 18px
  SHAPE   `--radius-sm`/`--radius`/`--radius-pill` <- the host's ladder ·
          `--focus` <- `--host-focus-ring`

`--faint` is deliberately the same colour as `--muted`: the host ships exactly two
text steps, and `--host-muted` already bottoms out at 3.47:1. A third, fainter step
derived from it measured 2.31:1. So the step below `--muted` is made in TYPE — 11px
uppercase tracked, the host's own metadata treatment — never in contrast.

**Four type steps, and they are the HOST'S.** 11px metadata / 12px small / 14px
body / 16px value / 18px heading. The previous 10.5px and 20px steps were a guest's
scale and read as an accident beside host chrome. `--sans` carries prose, labels
and section names; `--mono` carries model ids, counts, hashes, timestamps — things
an operator would copy. Numerals are tabular everywhere, so counts in a column line
up. A cause label ("fail safe strong") is a phrase, so it is sans, not mono.

## 7. Invariants (tests depend on these)

- Exactly one inline `<script>` and one inline `<style>`; no build step, no CDN.
- Never `innerHTML` / `insertAdjacentHTML` / `outerHTML` / `eval` /
  `new Function` / `document.write`. All text via `textContent` — decision
  traces contain attacker-influenceable task text.
- Nav items keep `class="tab"` + `role="tab"` + `data-tab` + `aria-controls`,
  and panels keep `id="panel-<tab>"`; one delegate drives selection.
- These ids are load-bearing for tests: `sheet`, `probeTask`, `ladder`,
  `routesTable`, `replayPath`, `chainPlan`, `clockbar`.
- `nowUtc()` is the ONLY reader of a wall clock, and `state.clock` overrides it.
  Every time-dependent function takes `{hour, weekday}` as a parameter, so the
  console's own tests pin an hour instead of inheriting the one they run at.
- The capability registry is an OPTIONAL read (`GET /capabilities`). A sidecar
  without it is not an error and never produces a note about the endpoint: the
  chain view reports those elos as unverified, which is the truth either way.
  Per-elo `declared` keys in router.yaml WIN over the registry, exactly as
  `capabilities.capabilities_for` resolves them, so an operator who corrects a
  stale window sees their own number.
- The chain plan is task-scoped: it is dropped the moment a new probe starts, and
  kept in state so a Refresh re-renders it instead of silently losing it.
- No `<svg>`: both sequences are lists, and the static test enforces it.
- Writes send `X-Hermes-CSRF-Token` when the host provides one.
- Under `(hover:none) and (pointer:coarse)`: 44px minimum targets, and inputs at
  `max(16px, 1em)`. That guard must NAME THE CLASSES — `.probe-input`, `.editor`,
  `.field input` — because a bare `input, textarea, select` scores (0,0,1) and
  every input here is reached by a class, which scores (0,1,0) and wins. Measured
  in a real iPhone 13 context, the bare form left all four inputs at 14px, i.e.
  the iOS focus-zoom trap it exists to prevent.
- Height is `100dvh`, not `100vh`: on iOS the visual viewport shrinks under the
  toolbar and a vh column overflows by exactly the toolbar's height.
- ONE authored moment: the screen you asked for eases in, 160ms, from an
  already-visible layout. Exactly one `@keyframes` in the file, and it yields to
  `prefers-reduced-motion`.
