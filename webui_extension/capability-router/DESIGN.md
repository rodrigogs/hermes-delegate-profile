# Capability Router Console — Composition & Wayfinding (Operate)

This file is the durable contract for the console's layout. It governs *where
things live and how state is signalled*, not features. If you change a panel,
keep it inside these rules. This is an **Operate** surface: scanability and
state legibility outrank expression. Brand lives in precise detail, never in
decoration. The visual world (dark control-room palette, `--sans`/`--mono`,
cards/chips/dots) is inherited verbatim; only composition changes.

## 1. Frame

- One `<main class="app-shell">`, a **2-column grid**:
  `grid-template-columns: var(--rail-w) minmax(0, 1fr); gap: 16px; align-items: start`.
  Max width and centering (`min(1520px,100%)`, `margin:0 auto`) stay on `.app-shell`.
- `--rail-w` is a CSS variable, **240px expanded / 56px collapsed**. The collapse
  is a `.app-shell.rail-collapsed` class that flips `--rail-w` to 56px; the whole
  layout reflows from that one variable, so the workspace (and the node canvas)
  reclaim the width in the collapsed state. The choice persists in
  `localStorage['cr-rail-collapsed']`.
- **Left column = `<aside class="rail">`** (sticky). **Right column =
  `<div class="workspace">`** containing the panels.
- Show/hide contract untouched: `.panel{display:none}` / `.panel.active{display:block}`
  + `@keyframes panel-in`. The workspace holds every panel; the `.active` toggle
  alone drives visibility.
- Drawer + backdrop stay `position:fixed`, outside the grid.

## 2. The rail (persistent, collapsible left nav = the tablist)

Top → bottom: **brand lockup → collapse toggle → health rollup → tablist →
foot (mode / Trace / Refresh)**.

- The rail **is** `nav.rail-nav[role="tablist"]`. Each destination is
  `<button class="tab rail-item" role="tab" data-tab="…" aria-controls="panel-…">`.
  **`class="tab"` is load-bearing** — the click delegate selects `.tab`; never
  remove it. `rail-item` carries all the new styling.
- Rail item internal grid: `grid-template-columns: auto 1fr auto` →
  `[icon] [label] [state]`. The `.rail-state` trailing cell holds a health
  `.dot` and a `.rail-badge`.
- **Collapsed state** (`.rail-collapsed`): labels (`.rail-label`) are hidden, the
  brand eyebrow/title hide (mark stays), health rollup shows dots only, foot
  buttons become icon-only. **The dot + badge stay visible in BOTH states** —
  signalling never depends on width. `.rail-label` in collapsed hover surfaces
  as a `title` tooltip (native).
- **Active treatment**: reuse the file's existing selected idiom.
  `.rail-item[aria-selected="true"]{ background:var(--surface-hover);
  box-shadow: inset 3px 0 0 var(--accent) }` — the same inset accent bar as
  `.route-row.is-selected`. No `::after` underline, no size change, no reflow.
- **Health rollup**: the three chrome chips move here verbatim — `#worstBadge`,
  `#reachabilityChip`, `#lastChecked`. Whole-system glance.
- **Foot**: `.mode-toggle` (Read/Edit), `#traceButton`, `#refreshButton`. JS keys
  them by id, so reparenting is free.

## 3. Per-rail state signalling (the point of the redesign)

Every rail item shows a **health dot** (color) and, where meaningful, a **count
badge** (textContent). Sources, using existing mappers only — never invent
classifiers, and **null-guard every source** (renderRail runs at init before the
first poll populates state):

| Rail item | Dot (health) | Badge (count) |
|---|---|---|
| Status | `state.unreachable`→dead; else `statusClass(worst_of_n\|worst\|overall\|status)`; else worst `normalizeLiveness()` across liveness rows | liveness target count |
| Pipeline | `mode==='edit'`→degraded (mirrors pipelineMode); else `policy`→alive, else dead | `policy.rules.length` |
| Replay | `endpointState.get('GET /routes')?.kind`: ok+routes→alive, pending→degraded, error→dead | `routes.length` |
| Blocklist | any breaker present→degraded/dead (`statusClass(entry.state)`); else alive | bans + breakers |
| Compaction | `GET /compaction` pending→degraded; `compaction` present→alive; else neutral | — (omit) |

Dot color is applied by setting the **wrapper** class `rail-state is-${cls}` so
the existing `.is-alive .dot / .is-degraded .dot / .is-quota .dot / .is-dead .dot`
rules fire. Badge hidden when count is 0 (`badge.hidden = n === 0`), enforced by
an explicit `.rail-badge[hidden]{display:none}` (author display beats UA hidden).

`renderRail()` is the **last** call in `renderAll()` and the last call in
`setMode()` (so the Pipeline edit dot flips on Read/Edit). It mutates only
pre-baked child spans via `textContent` + `className`/`.hidden`; never builds
nodes, never touches SVG, never throws on absent state.

## 4. Panel-head pattern

Every panel's **first child** is `<div class="panel-head">` — the single home for
a panel's identity + its own KPIs, which is how the Status kv duplication dies:

```
<div class="panel-head">
  <div class="panel-head-main">
    <p class="section-kicker">STATUS</p>
    <h2 class="panel-title">Router posture</h2>
    <p class="card-subtitle">One line: what this panel is for.</p>
  </div>
  <div class="panel-kpis"><!-- chips/badges unique to THIS panel --></div>
</div>
```

- KPIs are this panel's alone. A number in the panel-head is **not** repeated in
  a kv-list below it. Panel KPIs use **their own ids** — never reuse the
  rail-health chip ids (`#worstBadge`/`#reachabilityChip`/`#lastChecked`), which
  are unique and owned by renderChrome.
- Per-panel mode/actions (`#pipelineMode`, `#compactionState`, `#replayRouteChip`)
  live in `.panel-kpis`, right-aligned.

## 5. Density & dead-space rules

- **No blank prime space.** `#statusDetails` is curated to only fields shown
  nowhere else (`last_event`, `reason`, `mode`); metric cards, worst badge, and
  pipeline nodes are the canonical homes for rules/tiers/classifier/valid/enabled.
- **Summarizer is merged into Compaction** (same `state.compaction` source, no
  unique datum). `#summarizerDetails` relocates into the Compaction panel.
- **Node canvas fills the freed width**: `.pipeline-svg` drops `min-height:420px`
  (small floor instead) and the Pipeline **inspector column is collapsible**, so
  the graph gets real width — especially with the rail collapsed.
- **The node editor is never an empty box.** With no node selected,
  `#pipelineInspector` shows policy KPIs + a one-line teach.
- **Raw JSON is structured, not dumped.** Replay step I/O renders as a labeled
  meta line + `in:` / `out:` blocks. Empty-default JSON views stay behind their
  trigger state.
- The Advanced JSON `<details>` stays collapsed and is the **only** write path
  for bans/new rules — do not add a second.

## 6. Inherited token table (verbatim — do NOT invent)

| Token | Value | Use |
|---|---|---|
| `--bg` | `#090b10` | page |
| `--surface` | `#10141c` | cards, panels |
| `--surface-raised` | `#151b26` | buttons, node boxes |
| `--surface-hover` | `#1b2331` | hover, **active rail item** |
| `--line` | `#263144` | hairlines |
| `--line-strong` | `#3a4a65` | strong borders |
| `--text` | `#ecf2fb` | primary text |
| `--muted` | `#92a0b7` | secondary |
| `--faint` | `#62708a` | kickers, labels |
| `--accent` | `#8fb8ff` | **active rail bar**, links |
| `--accent-strong` | `#4a8cff` | primary action |
| `--green / --amber / --violet / --red` | `#5ee1ad / #f6bf5f / #bf9cff / #ff7f8d` | alive / degraded / quota / dead |
| `--mono` | SFMono stack | machine facts, badges, kickers |
| `--sans` | Inter stack | prose, labels |
| `--radius` | `10px` | corners |
| `--shadow` | `0 18px 50px rgba(0,0,0,.38)` | rail float |
| `--focus` | `0 0 0 3px rgba(143,184,255,.28)` | focus ring |

New tokens: `--rail-w` (layout variable only). New classes are compositional:
`rail`, `rail-nav`, `rail-item`, `rail-icon`, `rail-label`, `rail-state`,
`rail-badge`, `rail-brand`, `rail-health`, `rail-foot`, `rail-collapse`,
`workspace`, `panel-head`, `panel-head-main`, `panel-title`, `panel-kpis`.

## 7. Responsive contract

Preserve today's collapse. In `@media (max-width:1000px)`: `.app-shell{
grid-template-columns:1fr}`, `.rail{position:static}`, `.rail-nav{
flex-direction:row;overflow-x:auto}`, and `.rail-item{width:auto}` (so items
don't each balloon to full width in the row scroller) — the rail degrades to a
horizontal scroller above the workspace, honoring the `min-width:320px` floor.
The desktop collapse toggle is hidden below this breakpoint.

## 8. Hard invariants (tests + JS contract)

- Exactly **one** bare `<script>` and one `</script>`. No second script.
- The inline script must never contain `innerHTML`, `insertAdjacentHTML`,
  `outerHTML`, `eval(`, `new Function`, `document.write` — even in strings or
  comments. Mutate via `textContent` + `className`/`.hidden` only.
- These literals survive verbatim: `data-tab="pipeline"`, `id="panel-pipeline"`,
  `id="pipelineSvg"`, `id="routesTable"`, and tokens `renderPipeline`, `/routes`,
  `svgEl`, `createElementNS`, `renderReplayStep`, `textContent`.
- Rail buttons keep `role=tab` + `data-tab` + `aria-controls` + `class="tab"`.
  The delegate and `aria-selected`/`.active` toggling stay byte-for-byte.
