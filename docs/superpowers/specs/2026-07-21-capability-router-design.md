# Capability Router for Hermes — Design (v1)

**Date:** 2026-07-21
**Status:** Approved design, pending implementation plan
**Repo:** `rodrigogs/hermes-delegate-profile` (this feature extends the existing plugin)

## Problem

The operator wants two capabilities in Hermes:

1. **Right capability per task (primary):** classify an incoming task's difficulty
   and dispatch it to the best agent — hard tasks to a strong model, easy tasks to a
   fast/cheap one — driven by **simple, objective, declarative rules**.
2. **Model blocklist (supporting):** stop specific models from being used. Concrete
   motivation: `gpt-5.6-sol` via `openai-codex` chronically stalls (accepts the
   connection, never streams a token), forcing a slow fallback to `glm-5.2` via `zai`.

## Confirmed intent (design contract)

- **Routing behavior = HYBRID.** Cheap deterministic rules (blocklist, keywords,
  size, has-code) run automatically every turn. The difficulty **classifier** (which
  costs a model call) fires ONLY when the cheap rules are uncertain or when explicitly
  invoked. Minimize per-turn latency/cost.
- **Primary goal = right capability per task.** Classifier + routing rules are the core;
  blocklist is supporting.
- **Router output = ALWAYS a Hermes profile, with an OPTIONAL model override.** The
  executor already exists and is hardened: `delegate_profile(profile, model?, timeout?)`,
  which spawns `hermes -p <profile> chat -q` in its own process group with a stall
  watchdog (validated live via the Hermes One webui on 2026-07-21).
- **Rules stay simple and objective.** No Turing-complete config.
- **Packaging = ONE plugin, extending `delegate-profile`.** Not a new plugin.
- **WebUI screen = DEFERRED.** MVP is file-config + CLI governance. No webui-repo patch.

## Top-level architecture

One plugin, co-located with `delegate_profile`. Blocklist, routing, classifier, and
fail-safe share **one decision path, one decision log (`cause=`), and one terminal
`delegate_profile()` call**. The fail-safe route must consult the blocklist, which stays
cheap only if they are co-resident.

The router is the **caller** of `delegate_profile`, so the model/provider axis is applied
as **arguments to the delegated call** — not an in-flight hook mutation. This makes the
entire capability axis hook-independent.

Three layers:

- **Pure core** — no IO, no state, no model call. Deterministic; unit-tested without Hermes.
- **Stateful shell** — state + IO injected at the edge (blocklist state, classifier call,
  cache/pin, decision log).
- **Adapter** — the only Hermes-coupled code; runs the pipeline and calls `delegate_profile`.

## Components (one purpose each)

### Pure core
- **`signals.extract(turn)`** → flat, depth-≤1 feature vector:
  `{char_len, has_code, size_lines, num_files, has_stacktrace, num_requirements,
  verb_class, lang, keyword_hits}`. One model-free pass.
- **`rules.match(features, blocked_model)`** — stateless top-down first-match over Table 1.
  Closed operator set; closed output `{profile?, model?, provider?, deny?, action?}`.
  Only **reads** the boolean `blocked_model`; never writes state.
- **`lint(config)`** — load-time validator: flat AND-map depth cap, closed ops/output,
  mandatory `default`, dead/shadowed/overlapping-row report. **Fails closed** on invalid config.
- **`explain(task)`** → `{matched_rule_id, output, matched_clauses, cause}`. One
  implementation, three consumers: dry-run CLI, regression harness (few-shot anchors ARE
  the fixtures), future webui rule-tester. Tooling can never drift from production behavior.
- **`tiers` map** — Table 2: tier → `{model, provider}`. Edited independently of the rubric.

### Stateful shell (state/IO injected)
- **`blocklist` pre-filter stage** — owns the only mutable ban state; unions operator
  manual bans with (deferred) auto-breaker cooldowns into the single boolean `blocked_model`;
  owns the `codex → glm-5.2` fallback chain. Rules only read its output.
- **`classify(task, features)`** — the gated cheap-model call: fresh, temp-0, token-capped,
  hard-timeout one-shot judge on a trusted-streaming provider (glm-5.2/zai). Feature vector
  fed in as pre-computed context. Recursion-guarded by an env sentinel (`HERMES_BRIDGE_DEPTH`
  precedent).
- **`cache + pin`** — exact-hash tier cache (normalize whitespace/case → hash → cached tier)
  + per-session **model-floor** pin, upward-only ratchet.
- **`decision_log`** — one greppable `cause=` line per turn from the closed set:
  `blocklist_veto | breaker_cooldown | keyword_match | size_rule | has_code_rule |
  classifier | session_pin | default_fallthrough | fail_safe_strong`.

### Adapter (only Hermes-coupled code)
- **`route_hook`** — runs Stage 0 → (gated) Stage 1, enforces the trust boundary, writes
  the `cause=` line, then calls `delegate_profile(profile, model, provider, timeout)`
  directly with the chosen args.

## The rule format (simple, objective)

Two ordered first-match tables plus config blocks. Non-Turing-complete: no loops, no vars,
no priorities, no nesting. **OR is expressed by adding a row.** Output type is closed:
`{profile?, model?, provider?, deny?, action?}`.

Two orthogonal output axes decided by two mechanisms:
- **PROFILE = role** (coder/reviewer/researcher) — resolvable from cheap objective signals
  (`has_code` → coder; "review/audit/PR" → reviewer). Rarely needs a model call.
- **MODEL = capability** (fast/cheap ↔ strong) — the difficulty axis. The classifier only
  ever decides this axis, never the role.

```yaml
# router.yaml — the sole policy source, beside delegate_profile.
enabled: true

classifier:                       # the judge obeys the blocklist; trusted provider
  model: glm-5.2
  provider: zai
  temperature: 0
  max_tokens: 128
  timeout_seconds: 8

fail_safe:                        # timeout / parse-fail / low-conf-at-boundary lands here
  profile: coder                  # a TRUSTED strong target, NEVER gpt-5.6-sol,
  model: claude-opus              # and still routed THROUGH the blocklist pre-filter
  provider: anthropic

blocklist:
  manual_ban:                     # static, config-owned, always enforced (fail-closed)
    - { model: gpt-5.6-sol, provider: openai-codex, reason: accept-but-never-stream }
  fallback_chain: [gpt-5.6-sol, glm-5.2]
  auto_breaker: { enabled: false }   # DEFERRED — stage exists, engine off

# --- Table 1: ordered, first rule whose ALL when-clauses hold wins. ---
rules:
  - id: block-codex-stall               # blocklist deny row sits at the top
    status: stable
    when: { model: { in: [gpt-5.6-sol, openai-codex] } }
    then: { deny: true }                # veto -> fallback_chain climbs

  - id: trivial-mechanical-edit
    status: stable
    when: { verb_class: { eq: trivial }, has_code: { eq: true }, size_lines: { lte: 40 } }
    then: { profile: coder, model: T1 } # route free, NO classifier

  - id: hard-verbs                       # debug/refactor/secure/concurrent/prove/optimize
    status: stable
    when: { verb_class: { eq: hard } }
    then: { profile: coder, model: T4 } # fail TOWARD capability, free

  - id: review-request
    status: stable
    when: { keywords: { contains: review } }
    then: { profile: reviewer, action: classify }  # role known, capability uncertain

default: { action: classify }           # the hybrid switch — never silent

# --- Table 2: tier -> capability. Consumed once; never re-enters Table 1. ---
tiers:
  T1: { model: glm-5.2-fast,  provider: zai }
  T2: { model: glm-5.2,       provider: zai }
  T3: { model: claude-sonnet, provider: anthropic }
  T4: { model: claude-opus,   provider: anthropic }
```

Semantics:
- Ordered list, top-down; **first rule whose ALL `when` clauses hold wins**; emit its
  `then`; stop.
- `when` = flat AND-map of `attribute: {op: value}`. **OR = another row** or a value list
  (`in: [...]`). Never nested boolean trees.
- Closed operator set: `eq ne in nin gt gte lt lte contains starts_with ends_with matches`.
  `matches` (regex) gated to one allowlisted field and flagged in review.
- Signal namespace flat, depth ≤ 1, validated at load. Reject deeper paths.
- Mandatory `default` routing to a real destination — never a silent no-match.
- Per-rule metadata `{id, description, status: experimental|test|stable}`.
- `then.model` accepts a concrete id or a `Tn` alias resolved via Table 2.

Anti-spaghetti guard: if a predicate cannot be evaluated as `feature op constant` against
the current turn, it does not belong in config — it belongs in the classifier prompt.

## Hybrid gating + classifier rubric

**Stage 0 — every turn, zero model calls:**
1. **Blocklist pre-filter** prunes banned + (later) cooled models; if the requested model
   is blocked → `deny` → fallback chain. Runs *before* rules pick.
2. `signals.extract()` computes the feature vector.
3. `rules.match()` runs Table 1. A concrete `{profile, model, provider}` → **route now, done.**
4. Fall-through / `action: classify` → Stage 1.

Direct deciders (no judge): **trivial** = `verb_class=trivial ∧ has_code ∧ small size_lines`
→ T1; **hard** = `has_stacktrace ∨ hard-verb keywords ∨ very large size/files` → T4 (fail
toward capability). **Length is a tie-breaker only** — a 2-line "prove this is thread-safe"
outranks a 500-line reformat.

**Stage 1 — classifier, minority path only.** Fresh temp-0, token-capped, hard-timeout
one-shot on glm-5.2/zai (one-shot structurally avoids the long-session degradation glm-5.2
is prone to). Feature vector fed in as context so the model spends judgment only on
linguistic/cognitive/ambiguity signals. One few-shot anchor per tier (= regression fixtures).

**Rubric — 4 discrete anchored tiers, never a numeric scale** (numeric scales drift):
- **T1 TRIVIAL** — single mechanical edit, no reasoning (rename, format, typo) → fast/cheap.
- **T2 SIMPLE** — one well-specified file, standard pattern, boilerplate → cheap.
- **T3 MODERATE** — bounded multi-step, 2–5 files, some design choice → mid.
- **T4 HARD** — cross-cutting, unknown-cause debug, correctness/concurrency/security/
  ambiguity, novel design → strong.

**Output, reasoning-first key order** (models attend to what they already emitted):
```json
{"signals":"1-2 sentences","tier":"T1|T2|T3|T4","confidence":"high|med|low","needs_capability":"one clause"}
```
Runtime rules:
- **`confidence` is a one-way UPWARD ratchet only:** low, or a tier straddling a routing
  boundary, bumps up one tier; never downgrades to cheap; never a numeric self-confidence.
- **Fail-safe** on timeout/parse-error/low-conf-at-boundary → `fail_safe` (trusted strong),
  routed **through the blocklist** — neither the judge's own call nor the fail-safe ever
  lands on a cooled/blocked model.
- **Recursion guard:** judge's own dispatch tagged exempt via the env sentinel; classifier
  output never re-enters Table 1.
- **Amortization:** exact-hash cache kills repeats; session pin fixes the **model floor**,
  and only Stage-0 clearly-hard signals may break it **upward**.
- **Rubric and Table 2 are separately editable** — change judgment without touching policy.

## Blocklist

- Separate pre-filter stage, its own state, runs first. The pure rule engine only reads
  the boolean `blocked_model`.
- Non-bypassable: only operator config sets policy. Inbound turn content feeds objective
  signals but **never** selects profile/model/provider or overrides `deny`.
- **v1 = a static manual deny row** (top of Table 1) + fallback chain. Fully solves the
  known `gpt-5.6-sol / openai-codex` stall with zero mutable state.
- **Fail-CLOSED:** the manual ban lives in `router.yaml`, enforced independently of any
  mutable state file. If a (later) breaker `state.json` is missing/corrupt, cooldowns are
  treated as empty — but config deny rows still fire. The blocklist never fails open.

### Two enforcement surfaces
- **Delegation path (v1 scope):** the router refuses to emit a blocked `{model, provider}`
  target. Fully covered without any hook.
- **Main agent in-band LLM calls (fast-follow, not v1-blocking):** to make the blocklist
  equally non-bypassable for the main agent's own calls, register an `llm_request`
  middleware that rewrites/refuses `api_kwargs["model"]` before the provider call
  (same-provider only). Lands as a fast-follow once confirmed; does not block v1.

## WebUI screen approach

**Deferred (YAGNI). Ship the CLI that must exist anyway for correctness:**
- `router explain <task>` — the rule-tester (`{matched_rule_id, output, matched_clauses, cause}`).
- `router lint` — dead/shadowed/overlapping-row validator, fail-closed.
- `router blocklist` — show banned (+ later cooled) models.
- `router log --tail` — the `cause=` stream.

These need no Hermes UI API and are the day-1 governance surface. **Phase 2 (only if a panel
API is confirmed):** a thin, read-mostly view on the existing Hermes dashboard (`:9119`) —
render `router.yaml` with per-rule `status` badges, tail the `cause=` log, one form POSTing
a task to `explain()`. The UI holds zero business logic; it is a view over the same core
functions. No new server, no new store.

## API dependencies to confirm before building

| # | Capability needed | Mechanism | Status | Degrade path |
|---|---|---|---|---|
| 1 | Executor takes a `profile` arg | `delegate_profile(profile, model?, timeout?)` | **✅ validated live via webui 2026-07-21** | — |
| 2 | Blocklist veto on main agent's in-band LLM calls | `llm_request` middleware rewrites `api_kwargs["model"]` (same provider) | ⚠️ middleware path confirmed in API map (`conversation_loop.py:1268/1282`); veto/refusal semantics to verify | delegation-path enforcement only (v1); document in-band not intercepted until middleware lands |
| 3 | Apply model/provider (capability knob) | `delegate_profile` CLI args — **hook-independent** | ✅ router is the caller | — |
| 4 | Stall signal for the auto-breaker | time-to-first-token / stream-started marker | ❌ not clean (`exit 124` is a wall-clock timeout, not a stall detector) | keep auto-breaker **deferred**; static deny row covers the known case |
| 5 | Classifier recursion guard | env sentinel (`HERMES_BRIDGE_DEPTH` precedent) | ✅ precedent exists | dedicated bypass dispatch path recognized by identity |
| 6 | Plugin config + state | `load_config` + `cfg_get(plugins.entries.<id>)`; JSON under `get_hermes_home()/<plugin>/` | ✅ canonical pattern (disk-cleanup) | — |
| 7 | Classifier model call from a plugin | `ctx.llm.complete_structured(...)` or `register_auxiliary_task` | ✅ first-class plugin API | — |

## Explicit DEFER (YAGNI)

- **Auto-breaker (stall counter + cooldown + auto-trip).** Ship the static manual deny row
  + fallback chain instead. Reason: one known bad model does not justify day-1 mutable
  state, and its only grounded stall signal (`exit 124`) is ambiguous until a stream-started
  marker exists (dep #4) — shipping it now would false-trip and cool the strong model on the
  hardest tasks. Keep the breaker as a defined stage (`auto_breaker.enabled: false`) so it
  drops in as a pure `(state, event) → (state, blocked_set)` reducer with clock injected,
  the moment a second flaky model appears and the stall signal is confirmed.
- **Bespoke webui screen** — CLI + `cause=` log first; panel only if a dashboard API is confirmed.
- **Semantic / embedding cache** — exact-hash only. Near-identical wording can hide very
  different difficulty; semantic cache is a later, high-threshold option.
- **Mutate-capable hook for the model axis** — unnecessary; the CLI-arg path is primary.
- **Two-artifact lib/plugin split** — pure modules in one plugin deliver the testability
  without the versioning tax. Split only if the operator later needs independent release cadence.
- **T3 mid-tier tuning, rule sprawl** — track rule count as a health metric; when the list
  explodes, push decisions into the classifier, not more rows.

## Testing strategy

- **Pure core:** unit tests without Hermes. The few-shot rubric anchors are the regression
  fixtures, fed through `explain()`. `lint()` gets adversarial invalid-config tests
  (fail-closed assertions).
- **Stateful shell:** inject fake state/IO/model-call; test cache hit/miss, session pin
  upward-only ratchet, blocklist fail-closed on missing state.
- **Adapter:** integration test reusing the existing `delegate_profile` E2E harness (real
  cross-profile spawn, assert routed target + no orphans).

## Provenance

Design grounded in two parallel investigations (2026-07-21):
1. A read-only map of the Hermes plugin API (hooks, middleware, `ctx` surface, config/state
   patterns) with file:line evidence.
2. A 5-phase design workflow: prior-art research (RouteLLM/cascade routing, LiteLLM/Portkey
   rule config, simple rules-DSL design, LLM-as-difficulty-classifier), a synthesized brief,
   three candidate architectures (minimalist / separation-of-concerns / extensible-platform),
   an adversarial judge panel, and a synthesized recommendation.
