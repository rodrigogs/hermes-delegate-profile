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
| How does it route? | **Pipeline** | the policy graph, editable under an explicit lock |
| What did it decide? | **Routes** | recent real decisions, replayable step by step |

Blocklist and Compaction are subordinate detail, not peers: they live inside
Health and Pipeline respectively unless they carry an active condition.

## 2. Rules of subtraction

Applied in order. When two rules conflict, the earlier one wins.

1. **Render nothing for nothing.** No empty card, no dashed placeholder box, no
   row that announces its own emptiness. A section with no data is absent, or a
   single muted line — never a framed void.
2. **One authority per fact.** Health is signalled by the rail dot and the model
   list. Mode is signalled by the lock control. Never a second chip repeating a
   state something else already owns; two sources that can disagree are worse
   than none.
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

- **Rail item** = destination + its live state: a health dot, and a count badge
  when a count is meaningful (rules, recent routes, active bans). This survives
  the collapsed rail and is the only always-visible health signal.
- **Model state** uses the router's own five states, coloured from the inherited
  tokens: alive `--green`, degraded `--amber`, quota `--violet`, dead `--red`,
  unknown `--faint`. The same colours mean the same things in the graph, the
  route list and the rail.
- **Cause colour** in a decision: deterministic rule `--green`, classifier
  `--violet`, refusal (veto / fail-safe) `--red`.

## 4. Writing is a locked door

- One control owns write mode: the **lock**. It states the current mode and the
  action pressing it performs, is the only filled/accent element in the rail, and
  never shares a shape with a status chip.
- Locked: no write control is present in the DOM at all — not disabled, absent.
- Unlocked: write controls appear, and the surface says it is armed.
- Every write still goes through plan → apply → confirm/revert with the
  `base_hash` guard. The UI never invents a second path.
- A write the environment cannot perform (no CSRF token, because the console is
  standalone rather than inside the Hermes One page) is refused up front with
  that reason.

## 5. Layout

- **Rail** (left, collapsible) when the console owns the window; a **horizontal
  deck** when it is embedded or narrow — a host that already owns the left edge
  must never face a second vertical navigation.
- One screen fills its width. The graph grows into whatever space the rail and
  the inspector are not using.
- Density: 12–16px inside a group, 20–24px between groups. More space above a
  heading than below it.

## 6. Inherited tokens (do not invent)

`--bg #090b10` · `--surface #10141c` · `--surface-raised #151b26` ·
`--surface-hover #1b2331` · `--line #263144` · `--line-strong #3a4a65` ·
`--text #ecf2fb` · `--muted #92a0b7` · `--faint #62708a` · `--accent #8fb8ff` ·
`--accent-strong #4a8cff` · `--green #5ee1ad` · `--amber #f6bf5f` ·
`--red #ff7f8d` · `--violet #bf9cff` · `--mono` SFMono stack (machine facts) ·
`--sans` Inter (prose) · `--radius 10px`.

`--sans` carries prose and labels; `--mono` carries model names, counts,
timestamps and anything an operator would copy. That pairing is the typographic
system — not decoration.

## 7. Invariants (tests depend on these)

- Exactly one inline `<script>` and one inline `<style>`; no build step, no CDN.
- Never `innerHTML` / `insertAdjacentHTML` / `outerHTML` / `eval` /
  `new Function` / `document.write`. All text via `textContent` — decision
  traces contain attacker-influenceable task text.
- Nav items keep `class="tab"` + `role="tab"` + `data-tab` + `aria-controls`,
  and panels keep `id="panel-<tab>"`; one delegate drives selection.
- These ids are load-bearing for tests: `pipelineSvg`, `routesTable`.
- Writes send `X-Hermes-CSRF-Token` when the host provides one.
