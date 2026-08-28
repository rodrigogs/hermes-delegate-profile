---
name: Capability Router Console
description: An operator's control room that wears the running Hermes One skin — two screens, rows of hairlines, and every change offered on the row it changes.
colors:
  # There is no palette here. Every value reads a --host-* property the theme
  # bridge forwards from the running shell; the hex after the comma is the
  # FAIL-SAFE — what the console looks like when the shell cannot be read at all.
  skin-accent: "var(--host-accent, #f3f3f6)"
  skin-accent-strong: "var(--host-accent-hover, #ffffff)"
  accent-text: "var(--host-accent-text, var(--host-accent, #f3f3f6))"
  accent-wash: "var(--host-accent-bg, rgba(243, 243, 246, .08))"
  accent-wash-strong: "var(--host-accent-bg-strong, rgba(243, 243, 246, .15))"
  accent-fg: "var(--host-accent-fg, #0a0a0c)"
  plane-bg: "var(--host-bg, #0a0a0c)"
  plane-surface: "var(--host-surface, #101013)"
  plane-raised: "var(--host-surface-subtle, #15151a)"
  plane-hover: "var(--host-hover-bg, #1c1c23)"
  hairline: "var(--host-border, #212127)"
  hairline-strong: "var(--host-border2, #33333d)"
  text: "var(--host-text, #f3f3f6)"
  muted: "var(--host-muted, #9a9aa8)"
  ok: "var(--host-success, #4ade9b)"
  warn: "var(--host-warning, #f7b955)"
  bad: "var(--host-error, #ff6b7d)"
  info: "var(--host-info, #b490ff)"
  ok-text: "color-mix(in srgb, var(--host-success, #4ade9b) 45%, var(--host-text, #f3f3f6))"
  warn-text: "color-mix(in srgb, var(--host-warning, #f7b955) 45%, var(--host-text, #f3f3f6))"
  bad-text: "color-mix(in srgb, var(--host-error, #ff6b7d) 45%, var(--host-text, #f3f3f6))"
  info-text: "color-mix(in srgb, var(--host-info, #b490ff) 45%, var(--host-text, #f3f3f6))"
typography:
  headline:
    fontFamily: "var(--host-font-ui, Inter, ui-sans-serif, system-ui, sans-serif)"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.18px"
  title:
    fontFamily: "var(--host-font-ui, Inter, ui-sans-serif, system-ui, sans-serif)"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-0.15px"
  body:
    fontFamily: "var(--host-font-ui, Inter, ui-sans-serif, system-ui, sans-serif)"
    fontSize: "var(--host-font-size-md, 14px)"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  small:
    fontFamily: "var(--host-font-ui, Inter, ui-sans-serif, system-ui, sans-serif)"
    fontSize: "var(--host-font-size-sm, 12px)"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "var(--host-font-ui, Inter, ui-sans-serif, system-ui, sans-serif)"
    fontSize: "var(--host-font-size-xs, 11px)"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.05em"
  mono:
    fontFamily: "var(--host-font-mono, ui-monospace, SFMono-Regular, Consolas, monospace)"
    fontSize: "var(--host-font-size-sm, 12px)"
    fontWeight: 500
    lineHeight: 1.5
rounded:
  sm: "var(--host-radius-sm, 4px)"
  md: "var(--host-radius-md, 8px)"
  pill: "var(--host-radius-pill, 999px)"
spacing:
  hair: "2px"
  tight: "6px"
  inside: "11px"
  row-gap: "14px"
  block: "16px"
  group: "34px"
  gutter: "clamp(14px, 2.4vw, 32px)"
  action-slot: "84px"
components:
  button:
    backgroundColor: "{colors.plane-surface}"
    textColor: "{colors.text}"
    typography: "{typography.small}"
    rounded: "{rounded.md}"
    padding: "5px 11px"
    height: "28px"
  button-hover:
    backgroundColor: "{colors.plane-hover}"
  button-commit:
    backgroundColor: "{colors.skin-accent}"
    textColor: "{colors.accent-fg}"
    rounded: "{rounded.md}"
    padding: "5px 11px"
  button-commit-hover:
    backgroundColor: "{colors.skin-accent-strong}"
    textColor: "{colors.accent-fg}"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.small}"
    rounded: "{rounded.md}"
    padding: "5px 11px"
  button-quiet-hover:
    backgroundColor: "{colors.plane-hover}"
    textColor: "{colors.text}"
  button-destructive:
    backgroundColor: "{colors.plane-surface}"
    textColor: "{colors.bad-text}"
    rounded: "{rounded.md}"
    padding: "5px 11px"
  chip-shape:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.small}"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
  chip-context:
    backgroundColor: "{colors.plane-raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
  chip-capability:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
  badge:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  scope:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.small}"
    rounded: "{rounded.pill}"
    padding: "5px 12px"
  scope-selected:
    backgroundColor: "{colors.accent-wash}"
    textColor: "{colors.accent-text}"
  tab:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "9px 12px 10px"
  tab-selected:
    textColor: "{colors.accent-text}"
  row:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "0"
    padding: "11px 0"
  row-open:
    backgroundColor: "{colors.plane-raised}"
  input:
    backgroundColor: "var(--host-input-bg, var(--host-surface, #101013))"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "9px 12px"
  editor-box:
    textColor: "{colors.text}"
    rounded: "0"
    padding: "14px 16px 16px"
  support-note:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "0"
    padding: "0"
    width: "34ch"
  preset-chosen:
    backgroundColor: "{colors.accent-wash}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "11px 12px"
---

# Design System: Capability Router Console

## Overview

**Creative North Star: "The Guest That Wears the House's Skin"**

This console is a panel inside Hermes One, and it owns no identity of its own. Every
colour it paints reads a `--host-*` property the theme bridge forwards from the
running shell, so it repaints with all 21 skins and both polarities; the hex after
each comma is a fail-safe for a shell that cannot be read at all, not a palette. The
measured reason: a copied palette is wrong in 20 skins out of 21, and in any light
skin this console was a black rectangle inside a parchment shell — screenshotted.

It is an Operate surface: an operator's control room, not a dashboard. Groups are
made of hairlines and space, never cards. The previous version of this file mandated
a kicker + title + subtitle on every panel and a card around every block; that rule
produced 53 cards, 21 subtitles and three competing health rollups, and the
operator's verdict was "feio e muito poluído — pouco objetivo". Fewer elements, each
carrying a fact.

The surface is TWO screens split by what the operator is DOING, not by which noun
they are looking at. **Configuração** holds the policy — the ordered rule sheet, the
probe, the model groups, the last-resort block, the presets, the general settings,
the file editor — and every change is offered on the row it changes. **Operação**
holds the runtime: reachable models, out-of-rotation, the decision log and its
replay. Operação is not read-only by decree: it carries exactly one write, the
`Remover o bloqueio` on a manual ban's own row, because that is state the operator
put there and only that row can lift it. Nothing else on that screen is writable.
Three nouns (Tarefas / Modelos / Decisões) failed because no noun says where a
setting lives: the rule list and the file editor sat under one while the presets and
the group chains sat under another, which is the whole of "I don't know where I can
edit the settings". `panel-health` is gone; `selectTab('health')` survives as an
alias to Operação, because a caller asking for the models is still asking for a
screen that exists.

**Key Characteristics:**
- No palette: every colour is the host's, forwarded live; the hexes are fail-safes.
- The accent marks WHAT YOU PICKED; the semantic four report CONDITION. Never crossed.
- Hairlines and space instead of cards; one structural primitive, the row.
- Two type families — sans for prose and labels, mono for what an operator copies.
- Writing is armed per object, on the row that owns it, in a fixed action slot; there
  is no mode, so a control is drawn whenever its object can carry it.
- A finding that has a remedy carries the remedy on its own row.
- No `<svg>` in the document and no markup injection: a control is a word.
- Two screens, one column, `100dvh`, 44px targets and 16px inputs under a coarse pointer.

## Colors

The palette is whichever skin is running; this file only decides what each borrowed
colour is allowed to MEAN.

### Primary
- **The Skin's Accent** (`{colors.skin-accent}`, gold in default/dark, purple in
  catppuccin/light): marks WHERE YOU ARE and WHAT YOU PICKED — the selected tab's
  20px x 2px bar, the focus ring, the open row's 1px rail, the 3px edge on a row an
  instruction pointed at, the chosen preset and scope, the decision being replayed,
  the line a probed task matched, the caret, the text selection, and the one
  committing button. It never reports a condition.
- **Accent Foreground** (`{colors.accent-fg}`): the label on the single filled
  button. The host declares its accent foreground only under `:root.dark`, so in
  every light skin this fallback is what lands — and it is near-black, not `#fff`,
  because against this file's own paper-white fail-safe accent a `#fff` foreground
  measured 1.1:1: an invisible label on the only committing control there is.

### Secondary — the semantic four
Condition, and only condition. Same colour, same meaning, on every screen.
- **Alive** (`{colors.ok}`) · **Degraded / needs attention** (`{colors.warn}`) ·
  **Quota, and inference happened** (`{colors.info}`) · **Dead or refused**
  (`{colors.bad}`). Unknown is `{colors.muted}`.
- **The four `-text` forms** (`{colors.ok-text}` and siblings): the same hue mixed
  45% into the skin's own text colour, for a state written as a WORD.

### Neutral
- **Planes** (`{colors.plane-bg}` / `{colors.plane-surface}` /
  `{colors.plane-raised}` / `{colors.plane-hover}`): page, control face, the raised
  plane an open row rises to, the pointed-at plane. Raised and hover take the host's
  authored washes rather than a mix toward text: measured, mixing toward the text
  colour INVERTS in catppuccin/light (bg `#EFF1F5`, surface `#FFFFFF`), so a raised
  plane read as recessed.
- **Hairlines** (`{colors.hairline}` / `{colors.hairline-strong}`): every group
  boundary, every row separator, the spine of a sequence, the inset edge on a dot.
- **Text** (`{colors.text}`) and **Muted** (`{colors.muted}`): exactly two steps.

### Named Rules
**The No Palette Rule.** No colour is authored here. If a value cannot be traced to
a `--host-*` property, it is not a token — it is a bug, and the only hexes in the
file are the fail-safes for an unreadable shell.

**The Split Meaning Rule.** The accent reports SELECTION; the four report CONDITION.
A gold underline can therefore never be read as health, and a green dot can never be
read as selection. A fifth meaning (predicate family, billing mode, destination kind)
gets no hue at all — it is made in TYPE and PLANE, plus its own word.

**The 45% Mix Rule.** A state colour has two forms and the difference is measured.
The host's `--success/--warning/--error/--info` are authored for FILLS; as text on
their own skin's background they bottom out at 1.38:1 (`--info`, neon-paint/light).
So a DOT takes the raw hue plus a 1px inset edge on `{colors.hairline-strong}`, and a
WORD takes the `-text` form: 45% hue into the skin's TEXT. 45% clears 4.5:1 in every
skin (worst 5.08:1) where 50% does not (4.44:1); mixing toward text rather than away
from background is what makes it darken on parchment and lighten on navy.

**The Two Text Steps Rule.** `--faint` is deliberately the same value as
`{colors.muted}`, because `--host-muted` already bottoms out at 3.47:1 and a third
step derived from it measured 2.31:1. The step below muted is made in TYPE — 11px
uppercase tracked, the host's own metadata treatment — never in contrast.

**The Colour Budget Rule.** Only a refusal is painted. Rejection reasons under a
DROPPED heading stay muted (the heading already says they were refused; painting them
put three reds beside the one real refusal); "goes to the classifier" is a KIND of
destination and is named once on its stage heading rather than coloured five times.
An off-catalogue attempt is a finding in a `{colors.warn-text}` note, not a red row:
the model still runs.

**The Never The Only Channel Rule.** Every dot sits beside its state in words; every
multiplier is in the text beside its amber. Colour is confirmation, never the fact.

**The Amber Is A Verdict Rule.** Amber means the ROUTER needs attention — an
exception count, a rule that never fired, an expensive window, a missing billing
mode, an id the catalogue does not know. It never marks the operator's own doing (an
open editor is not a degradation), and a disclosure about the DATA rather than the
policy takes the muted dot instead (the window-stale line halos on the hairline,
precisely so it cannot read as amber).

## Typography

**Display / Body Font:** the host's UI sans (`--host-font-ui`, Inter and the system
stack behind it).
**Mono Font:** the host's `--host-font-mono`.

**Character:** the shell's own voice, borrowed whole. There is no guest face: the
previous 10.5px and 20px steps were a guest's scale and read as an accident beside
host chrome.

### Hierarchy
- **Headline** (600, 18px, 1.3, -0.18px): the panel title and the inspector head.
  Measured off the running shell's `.main-view-title`. Hidden under `.is-embedded`.
- **Title / value** (600, 16px, -0.15px): the lede's answer and a fact's value —
  scannable from a metre away without being a headline. Mono variant at 500 for a
  model id.
- **Body** (400, 14px, 1.5): prose, rule names, section leads, verdicts.
- **Small** (400, 12px): supporting facts, rails, prices, notes, JSON.
- **Label** (600, 11px, 0.05em, uppercase): section names, fact labels, stage names,
  cause words, `editando`, badges, a chain block's own head. This is the third text
  step, and it is also the whole treatment of a support note in the header (11px,
  muted, capped at `34ch`, never pressable-looking).
- **Mono** (500, 12–14px): model ids, rails, counts, hashes, timestamps, context
  windows — anything an operator copies into a shell.

### Named Rules
**The Copy-It Rule.** Sans carries prose, labels and section names; mono carries
machine facts. A cause label ("fail safe strong") is a phrase, so it is sans. A rule
title is prose and its slug is a machine fact, so they do not share one face — at one
size in one mono they made the pt-BR name and the id peers and left the row with no
step of its own.

**The Tabular Rule.** `font-variant-numeric: tabular-nums` on the body, so counts
line up down a column.

**The One Measure Rule.** Prose is capped at `74ch` with `text-wrap: balance`, and
the cap is attached to the KIND of content, not to a particular class: a measure that
belongs to a class only holds where that class went, and prose kept landing
elsewhere. At full panel width lines ran to 146 characters, twice a readable line.
`balance` and not `pretty`: measured on this surface, `pretty` left last lines at
24%, 13% and 5% of the first line's width; `balance` puts them at 91–95%. Data is
exempt on purpose — chain rows, chips, the `Ordem:` knob line and every mono value,
which a measure would only wrap.

**The Label Is Not A Sentence Rule.** Tracked uppercase is a treatment for a label;
58 characters of it is a passage read by shape, so a sentence goes to a placeholder
or a note instead.

## Layout

**The host's own panel shape.** A `.view-head` (18px/600 title left, actions right,
one hairline under it, min-height 41px, padding `8px clamp(14px, 2.4vw, 32px)` — all
measured off the running shell's `.main-view-header`), then a two-tab strip in the
host's nav idiom, then a scrolling body that is the host's `.main-view-body`
(`22px clamp(14px, 2.4vw, 32px) 44px`). The header does not scroll. Under
`.is-embedded` the head's actions and notes shrink to 25px of content, because 25 +
16px of padding is the 41px the shell's own `.panel-head` measures.

**No wordmark.** The host's rail already says which surface you are on. The old
"ROUTER · HERMES ONE" mark spent 9 characters and a whole type voice the shell never
uses on information the operator already had. Under `.is-embedded` the title and the
tab strip are not drawn at all — the review counted "Capability Router" three times
on one screen (rail label, sidebar head, masthead) — but they stay in the DOM,
because the host sidebar reads the tabs' state and clicking a sidebar row clicks a tab.

**One column, capped measure.** Two destinations do not earn a permanent vertical
rail, and a host that already owns the left edge must never face a second one.
`min(1180px, 100%)`, centred. One screen fills its width; nothing letterboxes.

**Density.** 11–16px inside a group, 30–38px between groups (`.group` is 34px), and
more space above a heading than below it. The first group on a screen takes no top
margin — the panel's own padding is that space.

**The action slot is a column.** Every list that can be edited reserves a fixed 84px
right-hand track, so "what can I do with this line" is answered at one x down the
whole screen. This is a grid on the sheet row, the group head, the last-resort block's
head and the settings row alike: as flex, the group's control sat at 788px, 787px and
796px on three consecutive rows, and a reader hunting for it has to re-find it every
time. On Operação the same slot is where a manual ban's `Remover o bloqueio` lands.

**The warnings stack is sticky** at the scrollport top (`z-index: 2`, on the page
plane). The invalid-policy warning is the only actionable message on the screen and
it used to disappear the moment the operator scrolled to the rule it named.

**A write's own confirmation lives outside the block it belongs to.** The unban
message sits after the out-of-rotation group, not inside it: when the last block is
lifted the group hides, and a confirmation that hides with the thing it confirms has
not confirmed anything.

**Responsive.** At ≤860px the decision list and its replay stop being side by side;
everywhere above that they stay side by side even inside the host panel, because
stacked, picking a decision appeared to do nothing — the replay was below the fold.
At ≤700px (the host panel with the workspace open) the lede stacks and payload grids
go to one column. At ≤640px — the host's own breakpoint, where it hides the rail —
the header becomes one column so the surface's name never truncates ("Roteador de
modelos" ellipsed to "Roteado…" at 390px), the tab strip scrolls with snap instead of
wrapping, facts go to two columns (four columns truncated "AGGRESSIVENESS" to
"AGGRESSIVE", a label that silently lies about which number it names), rows become
one wrapping line, and the action slot stops being a column and becomes a line
left-aligned under the row it acts on.

**Height is `100dvh`**, not `100vh`: on iOS the visual viewport shrinks under the
toolbar and a vh column overflows by exactly the toolbar's height. Under
`(hover: none) and (pointer: coarse)` targets are ≥44px (decision rows 56px) and
inputs are `max(16px, 1em)` — and that guard NAMES THE CLASSES (`.probe-input`,
`.editor`, `.field input`, `.field select`), because a bare `input, textarea, select`
scores (0,0,1) and every input here is reached by a class, which scores (0,1,0) and
wins. Measured in a real iPhone 13 context, the bare form left all four inputs at
14px — the iOS focus-zoom trap the block exists to prevent. The keyboard inset is
measured on this document with the host's own formula, because a separate document
does not inherit the host's.

### Named Rules
**The Sequence Down One Spine Rule.** The policy is an ordered first-match table and
a trace is a short ordered path, so both are drawn as numbered vertical lines against
a single 1px spine — never as a node canvas. Fifteen tiny boxes to highlight three of
them makes the reader hunt for the answer.

**The Unordered Gets No Ordinals Rule.** A `sequential` chain is numbered down the
spine; a `random` chain is a SET — it loses the ordinals and the spine and wraps
across the line, keeping only the 26px indent so the eye does not have to re-find the
model. A numbered random chain is a lie about which elo runs first. The substitute
queue on Operação is drawn in that same unordered form.

**The Compare Down A Column Rule.** Destination and count are fixed tracks
(`fit-content(34%)` and 52px on the sheet; a 118px cause column on a decision row,
because the longest real cause "FAIL SAFE STRONG" needs 112px at 11px and at 104px it
wrapped and made every fail-safe row 20px taller than its neighbours). Two ways this
has failed, both measured: `auto` on the destination track sized to its content's
intrinsic width (278px) and starved the `minmax(0, 1fr)` rule column to 0px; and
capping the CHILD with a percentage is circular — the child's containing block is a
track the child itself sizes, so the destination resolved to 0px and every
destination word vanished while the arrow stayed. The cap belongs on the TRACK.

## Elevation & Depth

Flat. There is no shadow vocabulary and no elevation ladder: depth is a hairline, a
plane change, and space. A group is a heading, a 1px rule under it, and rows
separated by 1px — never a card. The only `box-shadow`s in the file are not
elevation: the focus ring, the 3px state halo behind a dot, the inset 1px accent rail
on a selected or open row, and the inset 1px edge that makes a 6px dot locatable.

### Named Rules
**The No Card Without A Reason Rule.** A card exists to group things read together.
One list, one table or one control does not need a frame, and nothing gets a second
frame inside something already marked — the in-row editor is a raised plane with one
hairline, not a box inside a box, and a payload is evidence under its label behind a
single left hairline rather than two bordered cards inside a bordered column.

**The 1px Accent Rail Rule.** Every "you picked this" mark is the same object: an
inset 1px accent rail on the leading edge, plus the accent wash where a whole row is
chosen. The open row, the probed rule that matched, the replayed trace step, the
picked decision — one reading, learned once. Its one louder sibling is the 3px accent
left edge on a row an instruction just sent the operator to, which is a pointer and
not a state.

**The Nothing Live Is Dimmed Rule.** No element is faded by opacity while its own
controls stay pressable. Measured at `.55` on parchment: `Editar` fell to 2.54:1 and
the row title to 3.95:1, against a 4.5:1 floor. Dimming survives only where nothing
in the dimmed thing can be pressed — a rule switched off in the file, a trace step
still ahead of the reader.

**The Render Nothing For Nothing Rule.** No empty card, no dashed placeholder, no row
that announces its own emptiness. A section with no data is absent (the
out-of-rotation group, the substitute queue inside it, the clock strip, the count
column, the whole last-resort block) or a single muted line. A count column with
nothing to count is not built at all — not filled with em dashes, and not left with a
branch that renders a placeholder no reader ever sees.

## Shapes

Radii come from the host's ladder and nowhere else: 4px for a control that sits
against an edge (a tab's top corners, a row's trailing corners), 8px for buttons,
inputs, code blocks and the preset row, and the pill for chips, badges and scope
selectors only — the host reserves pills for chips and badges, so an action never
becomes one. A row has no radius on its leading edge, because its leading edge is the
spine or the accent rail.

The recurring silhouette is the ROW: a grid of `auto | minmax(0, 1fr) | auto`, 11px
of vertical padding, one hairline below, no border on the last one. The rule sheet
row, the model row, the out-of-rotation row, the settings row and the decision row
are all that primitive with different tracks — the third list on a screen must not be
a third layout.

The one deliberately heavier edge in the file is 3px of accent on the left of a row
an instruction pointed at, with 10px of padding behind it, inside a form. It is used
for exactly that: the chain row a "swap this for a catalogue model" instruction sent
the operator to, and the requirement field a warning named. A hairline would not be
findable in a form of identical rows; a plane or a hue would read as state.

## Components

### Buttons
- **Shape:** 8px radius, 1px hairline border, `{colors.plane-surface}` behind it,
  12px/600 label on a 16px line box — so 5 + 16 + 5 + 2 = 28px, the measured height
  of the host's own header button, which makes the header compute to 44px like every
  native panel. Left to the inherited 1.5 line-height it came out 32px and the header
  48px: the kind of drift that reads as "not quite ours" without ever being nameable.
- **Quiet (the row action):** same shape and height, no plane and a transparent
  border until pointed at, muted label. It carries `Editar` on a sheet row, a group
  head and a settings row, and `Remover o bloqueio` on a manual ban's row. Ten filled
  rectangles down a list would out-shout the policy they sit beside. The border is
  real and transparent, not absent: a control that gains a border on hover shifts the
  text under it.
- **Commit:** the only FILLED element on a screen, filled with the skin's accent over
  `{colors.accent-fg}` — the host's own primary-button rule.
- **Destructive:** `{colors.bad-text}` label on the plain face, strong hairline;
  hover adds the 14%-alpha bad wash.
- **Two full-face buttons inside a finding:** where a warning offers a choice, both
  choices are ordinary `.btn`s in that note — no quiet variant for the one you are
  meant to take, no accent on either. Neither writes; one navigates, one dismisses.
- **Absent, not disabled,** where an action must not be offered: with a lint error the
  save button is DETACHED from the DOM (and re-inserted before "Ver o que muda" when
  the error clears), because a disabled control still reads as "this is the thing to
  press", and pressing your way out of an error state is the wrong lesson.

### Chips
- **Predicate clauses** (one chip per clause, in a real `<ul>`, so two conditions are
  never announced as one string): shape = pill, transparent, muted; context = raised
  plane, text colour; capability = strong hairline, text colour. The family is the
  chip's CLASS and, where the text is a fragment rather than a pt-BR sentence, a real
  word inside the chip — never a border colour, which does not survive being read
  aloud.
- **Tier destination:** a real button in the pill shape that reveals the chain it
  points at, in place, with the same list the groups draw. Expanded takes the accent
  wash, accent text and accent-tinted border: that is a selection, not a condition.
- **Badge** (billing mode): 11px tracked uppercase in the pill, no hue — billing is
  not a condition. A MISSING mode takes `{colors.warn-text}`: a request whose rail is
  undeclared cannot be costed.
- **Scope selector:** pill, muted, `aria-pressed`; chosen takes the accent wash.
  One chip vocabulary; a second near-identical shape for the same concept is how a
  surface comes to look like two products.

### Rows / Containers
There are no cards. The container IS the row, and its states are: default (hairline
below), hover (`{colors.plane-hover}`, only where the row leads somewhere), open
(`{colors.plane-raised}` + 1px accent rail + accent-tinted bottom hairline),
matched/picked (accent wash + rail). Internal padding 11–12px vertical, 0
horizontal — no right padding on the sheet row, because its action slot has to end on
the same x as the one on a group row and a settings row; 10px of it put two distinct
x positions across sixteen slots.

### Inputs / Fields
1px hairline, `var(--host-input-bg, …)` behind it, 8px radius, mono text — these hold
model ids, JSON and task text. Focus moves the BORDER to the accent, and the shared
`:focus-visible` ring is a 3px accent-tinted halo on the host's own focus token. A
field's label is 11px tracked uppercase; a field's own answer (how many models can
serve this group) is a note inside the field, not an error bubble elsewhere. Under a
coarse pointer every input is ≥16px.

The same field primitive is what an inline remedy is made of. A rule whose
destination names a group the table does not have carries the ordinary destination
`<select>` on its own row, labelled `Escolha um destino que exista:` — a field, not a
special widget, because the remedy for a broken value is the control that sets that
value. Choosing writes to the DRAFT and opens that rule's editor, where the change is
visible and only `Salvar` writes it.

### Navigation
Two tabs in the host's nav idiom: a quiet 14px/500 muted label that takes
`{colors.accent-text}` when selected, plus the host's own 20px x 2px accent bar on
the bottom edge. Each tab carries the live state of what it leads to — a condition
dot, plus a count only when a count is meaningful, in `{colors.warn-text}` when the
count is of exceptions. Full `role="tablist"` semantics: only the selected tab is in
the tab order and arrows/Home/End move between them, and one `selectTab` serves the
tab clicks, the lint jump and the host sidebar alike. Inside the host the strip is
undrawn and the shell's sidebar mirrors it.

### The In-Row Editor (signature)
Writing is armed per OBJECT. Every row that can be edited carries its own `Editar` in
the fixed action slot; pressing it opens the form INSIDE that row, the slot swaps to
the word `editando` (with the same 11px of right padding the button spent, so the
column does not shift), and `Salvar / Ver o que muda / Cancelar` stand where `Editar`
stood. The form is a raised plane with one accent-tinted top hairline, and its first
line says what is being changed and that nothing is written yet. Its entrance reuses
the file's one keyframe.

There is no global write mode, and its removal was measured rather than judged:
inside the host document, arming it changed the body of the page by exactly 0
characters (2207 before, 2207 after) — it wrote its one announcing sentence into a
node the next render overwrote, so the whole visible payload of arming a write was a
button label flipping. A mode whose scope is invisible is worse than no mode. The
read-only default it was right about survives, and now comes from the affordance: a
row with nothing to configure carries no control at all.

The mode's removal is also what makes every control on a row unconditional. Anything
still gated on `state.mode` after this is a control that will never be drawn again —
the manual ban's unban button and the broken row's remedy select were both found that
way, and both are now drawn whenever their object exists. `writable()` and the server
gate the write; nothing gates the drawing of it.

### The Header Support Note
One muted 11px line in the header actions, capped at `34ch`, saying that the file may
be edited and may not be saved yet. It is a note and never looks pressable, because
the action it describes belongs to the rows. It is toggled off the same error set the
warning banner reads, so the two cannot disagree about whether the file is blocked.
Its copy is a known defect and not a pattern to copy: it says "o erro acima" while
sitting in the header, which is ABOVE the banner it points at.

### Out of Rotation (signature)
Two kinds of absence in one list, told apart by what a control could honestly do.
A **manual ban** is a row in the `is-dead` state — raw-hue dot, the elo in mono, the
reason under it, the word `banido` in the 45%-mixed bad form — with
`Remover o bloqueio` in the action slot, always. A **breaker cooldown** is a row in
the `is-degraded` state with the time still owed in mono (`faltam 300s`) and NO
control at all: it expires on its own, and a button promising to remove what time
removes would be a false control. The write is the whole `manual_ban` list minus the
lifted item, because the server replaces lists wholesale; the confirmation appears
after the group so it survives the group hiding when the last ban goes.
The **substitute queue** is drawn under both lists, headed
`Substituto da lista de reserva geral`, in the unordered chain form — and only while
the block itself is on screen, because with nobody out of rotation there is nothing
to substitute for.

### The Numbered Sheet, the Clock Strip, the Preset Choice, the Last Resort
- **Sheet:** an `<ol>` with a 1px spine down the left margin, a stage node on the
  spine, a tick out to each row, ordinals in mono, and destination + count in fixed
  tracks. Precedence is the vertical axis.
- **Clock strip:** one persistent line above the warnings on every screen (the hour
  changes what both screens mean), both clocks labelled, one rail per line, and only
  the expensive state takes a hue. A flat rail is not a row.
- **Presets:** a real radio group — each row is a `<label>` wrapping a real radio, so
  the whole row is the target and the browser's own arrow keys move between them. The
  chosen row takes the accent wash and the one in force says so in accent 11px.
- **Last resort:** a group with the same head, the same 84px slot and the same
  `Editar`, whose note comes before its chain in the order every other block reads
  in. Its `Editar` JUMPS: it opens the sheet's last step and scrolls it into view
  rather than opening a second form, because that block and that row are two views of
  ONE object. With no last resort configured there is no block at all, only the one
  muted line saying so.

### Named Rules
**The One Write Vocabulary Rule.** One `WRITE` map owns every phrase the screen says
— not just the write verbs (plan, save, saving, revert, cancel, conflict, no-op,
in-flight, lint error, missing action, no credential) but `loading`,
`refresh`/`refreshing`, `routing`/`routingOn`/`routingOff`, `banned`, `cooldownLeft`,
`classifier`, `textEdit`, and the named gesture `remove`/`removing`. Static markup
carries none of it: one pass at boot stamps the file editor's three buttons, the
reload button and the file editor's own note from the map, and the draft hint quotes
the buttons by the map's labels. The defect it prevents is a phrase existing twice in
the file — a test counts each literal — and the reason a gesture is named twice
(resting and in flight) is so a refusal reads "não é possível remover o bloqueio",
never a generic "salvar" that does not match the button that was pressed.

**The Control Only For What A Control Can Do Rule.** A row gets a button only where
pressing it changes the thing named. A manual ban gets `Remover o bloqueio`; a
breaker cooldown gets nothing, because it lapses on its own. Offering to remove what
time removes is a lie the surface tells once and the operator remembers.

**The Remedy On The Row Rule.** A finding that has a remedy carries the remedy where
the finding is. A destination naming a group that does not exist carries the
destination select. An attempt the catalogue does not know names the id and offers the
two things that exist: swap it for a catalogue model — which opens that group's chain
editor with THAT attempt's row marked, so nobody counts rows — or leave it, which
writes nothing and hides the note until the next read. Two remedies, both real; a
warning with no way out is a dead end, and this console was rebuilt to remove them.

**The One Attach Point Rule.** The editor is a singleton. Where one object is shown
in two places, the second place links to the first and never opens a second form:
two attach points for one form mean it lands in whichever renderer ran last.

**The Ranked, Not Recited Rule.** Where a thing has five knobs and four are the
engine's default, the DECLARED ones are the line and the defaults collapse into one
control that says how many there are. Recited inline they read "na ordem escrita
(padrão do motor) · o primeiro fica fixo (padrão do motor) · teto de preço 1.5× · …":
170 characters in which the same parenthesis appears three times and one knob is news.

**The Count, Not A Sentence Rule.** A count column holds a count ("26×", "nunca") and
its cell is NOT BUILT when the log does not cover the policy — the track collapses and
the reason is stated once, by the disclosure above the list. Never per row: the
sentence form put the same 60 characters on ten consecutive rows in the widest column
and overflowed the host panel by 236px with the third column clipped. Because the
cell is absent in that case, a "stale" branch inside the cell is unreachable by
construction and does not exist.

**The Editor Node Is Fetched, Never Held Rule.** The form is a singleton moved next
to whatever is being edited, so it becomes a child of a row that the next render
clears. It is fetched through an accessor that rebuilds it under the same id when the
document no longer has one. Measured in a browser: without it, the first `Editar`
built a form and every one after it marked a row and rendered nothing.

**The Browser's Own Surfaces Take The Skin Rule.** Selection, caret and scrollbars
are drawn too: a default blue selection inside a gold-on-navy shell, a white caret
and a platform-grey scrollbar belong to no design system.

## Do's and Don'ts

### Do:
- **Do** read every colour from a `--host-*` property, and treat the hex after the
  comma as the fail-safe for an unreadable shell.
- **Do** keep the accent for selection and the four for condition, and give a fifth
  distinction TYPE and PLANE plus its own word instead of a hue.
- **Do** write a state as a word in the 45%-mixed `-text` form, and a state as a dot
  in the raw hue with a 1px inset edge — with the word always beside the dot.
- **Do** make groups out of a heading, one hairline and space, at 34px apart, with
  more space above a heading than below it.
- **Do** put every editable object's own control in the fixed 84px action slot, and
  open its form inside the row it changes.
- **Do** draw a control whenever its object exists — nothing on this surface is gated
  on a mode, because there is no mode left to arm.
- **Do** give a finding its remedy on its own row, and mark the row an instruction
  sent the operator to with the 3px accent edge.
- **Do** keep every phrase the screen says in the `WRITE` map, and stamp static
  labels from it at boot.
- **Do** cap prose at `74ch` with `text-wrap: balance`, attached to the kind of
  content rather than to a class, and leave data uncapped.
- **Do** draw an ordered sequence numbered down one 1px spine, and an unordered one
  as a wrapped set with no ordinals.
- **Do** keep the domain's words — profile, model, provider/rail, tier, rule,
  classifier, fail-safe, blocklist, breaker, decision — and invent none.
- **Do** keep `100dvh`, 44px coarse-pointer targets, and `max(16px, 1em)` inputs
  named BY CLASS.

### Don't:
- **Don't** author a palette, a wordmark, or a type step the host does not ship. The
  10.5px and 20px steps were a guest's scale and read as an accident beside host chrome.
- **Don't** frame a single list, table or control, and don't put a frame inside
  something already marked as open or picked.
- **Don't** derive a third, fainter text step: measured at 2.31:1. Make the step in
  type instead.
- **Don't** fade anything by opacity while its own controls stay pressable — measured
  2.54:1 for a live `Editar` at `.55`.
- **Don't** paint every reason, every destination kind, or the operator's own actions.
  Amber is a verdict about the router; a disclosure about the DATA takes the muted dot.
- **Don't** add a global mode, or any control whose scope is not the thing you can
  see. Arming the old one changed the page by 0 characters.
- **Don't** offer a control for something that resolves itself — a breaker cooldown
  gets a countdown, never a button.
- **Don't** open a second editor for an object that already has one; link to it.
- **Don't** disable a control that must not be pressed if it can be absent instead,
  and don't build a cell only to fill it with em dashes or a branch nobody reaches.
- **Don't** put a write's confirmation inside a block that hides when the write
  succeeds.
- **Don't** let a phrase exist twice in the file, or let static markup own a word the
  `WRITE` map should.
- **Don't** use `<svg>` or an icon font in this document, and never `innerHTML` /
  `insertAdjacentHTML` / `outerHTML` / `document.write` — decision traces carry
  attacker-influenceable task text, so all text goes through `textContent`. A control
  is a word. (The rail icon the extension registers with the host is the host's own
  `iconPath` API, outside this document.)
- **Don't** size a destination or count column with `auto`, or cap it on the child:
  both collapsed the column to 0px, measured.
- **Don't** repeat a fact two surfaces already own — a second chip for a state
  something else signals, a badge counting what a numbered list counts, a heading
  repeating the panel title, or a per-row copy of the window a count was taken over.
- **Don't** add a second `@keyframes`, a second button vocabulary, or a second chip
  shape. One authored moment (160ms ease-out on the screen you asked for, reused by
  the in-row form), and it yields to `prefers-reduced-motion`.
