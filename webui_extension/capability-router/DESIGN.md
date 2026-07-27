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
          `--accent-fg` <- `--host-accent-fg`
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
  `routesTable`, `replayPath`.
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
