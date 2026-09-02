# hermes-smart-router — Architecture Map

**Snapshot of `main` @ `c3962a5`, 2026-09-01.** Produced by reading every source and
test file in full, then adversarially auditing the result against the code; the
audit's confirmed corrections are folded in. Sections marked **RESOLVED** are
places where two independent readings disagreed and the code settled it.

Two rules for reading it, the same two both specs under `docs/superpowers/specs/`
open with:

* **THE CODE IS THE AUTHORITY.** Where this document and the code disagree, the
  code is right and this document has a bug — report it as one.
* **Line references rot.** They were correct at `c3962a5`. Treat a `path:line` as
  "look here", not as a citation; the symbol name beside it is the durable half.

A dated snapshot is deliberate. The alternative — a document promising to track
`main` — is the shape that produced most of the staleness catalogued in §8, and
this repo's convention is to correct a claim in place and keep the measurement
rather than to quietly rewrite history. **§10 lists what has already superseded
this snapshot**, and is the first thing to read if you are working after it.

---

## 1. What this project is

A Hermes Agent plugin that does two things under one install: it provides `delegate_profile`, a tool that spawns a subagent as an independent OS process tree (`hermes -p <profile> chat -q "<prompt>"`) so the child cannot crash the parent, can be a different Hermes version, and gets the target profile's *full* configured toolset — none of which the in-process `delegate_task(profile=…)` can offer (`__init__.py:7-21`). Around that executor it ships a **capability router**: a pure, non-Turing-complete first-match rule table over a flat signal vector, a 43-entry hand-verified model/price registry, a per-turn attempt-chain planner (capability filter → dollar cap → time policy → ordering), an auto-breaker, a durable decision trace, and an optional LLM difficulty classifier gated behind a host trust grant. Policy lives in one hot-reloaded YAML (`router.yaml`, gitignored, seeded from the tracked `router.example.yaml`), edited through a lint-gated, hash-guarded, revertible write path exposed over a loopback sidecar to a self-contained operator console. The whole design is organised around one rule: the pure core reads no clock and no global randomness — both impurities are created at exactly one edge and injected downward as parameters, which is what makes "why did the 04:00 cron route there" answerable at 14:00. Roughly half the bytes in this repo are prose: docstrings and comments that record the measured production defect each branch closes, and tests whose names are the contract sentences.

---

## 2. Runtime topology

```
                        ┌──────────────────────────── the WSL2 box ────────────────────────────┐
                        │                                                                      │
  operator browser      │   hermes-webui.service :8787  ── consented extension proxy ──┐       │
  ┌───────────────┐     │     (Hermes One shell; mints token-v1, owns CSRF)             │       │
  │ Hermes One    │◄────┼──── GET / ; static router-nav.js + router-nav.css             │       │
  │  shell        │     │                                                              │       │
  │ ┌───────────┐ │     │   /api/extensions/hermes-smart-router/sidecar/*               │       │
  │ │ iframe    │ │     │        + X-Hermes-Sidecar-Token  ────────────────────────────►│       │
  │ │ srcdoc    │─┼─────┼──────────────────────────────────────────────────────┐        │       │
  │ │ console   │ │     │                                                      ▼        ▼       │
  │ └───────────┘ │     │        hermes-router-sidecar.service :8791  (python -m router.one_sidecar)
  └───────────────┘     │        ├─ GET  /health /status /policy /blocklist /liveness              │
                        │        │      /capabilities /compaction /lint /explain                   │
                        │        │      /routes  /routes?id=<id> (one full trace, 404 unknown)      │
  dashboard tab ────────┼──────► ├─ POST /explain /plan /apply /apply/revert                       │
  (dashboard/dist)      │        ├─ GET  /console  (serves console.html bytes, gzip-cached)        │
      via Hermes        │        └─ RouterService(router.yaml)  ── re-reads YAML per request       │
      dashboard         │                    │                                                    │
      /api/plugins/*    │                    ├── reads  router.yaml            (hot)              │
  (dashboard/plugin_api)│                    ├── writes router.yaml + .bak     (plan/apply)        │
                        │                    ├── reads  breaker-state.json                        │
                        │                    └── reads  routes.jsonl(+.1..3) + attempts.jsonl      │
                        │                                     ▲                                   │
   ┌────────────────────┼─────────────────────────────────────┼───────────────────────────────────┤
   │  hermes-gateway / agent process  (loads this plugin)      │ (single writer)                   │
   │   register(ctx) → tool `delegate_profile`                 │                                   │
   │                 → hook  post_tool_call                    │                                   │
   │                 → hook  pre_kanban_dispatch ──────────────┤                                   │
   │        │                                                  │                                   │
   │        ├─ router.adapter.route()  ── decision ───────────►│ DurableDecisionLog._persist        │
   │        ├─ router.blocklist  ◄──rw── breaker-state.json                                        │
   │        └─ _attempt(): subprocess.Popen(start_new_session=True)                                │
   │                 │                                                                             │
   │                 ▼   own process group; ttfb/idle/hard watchdog; killpg on any stall            │
   │        ┌────────────────────────────────────────────┐                                         │
   │        │ hermes -p <profile> chat -q <prompt>       │  ← different Hermes version allowed      │
   │        │   └─ grandchildren: MCP servers, LSP,      │    env: HERMES_PROFILE, HERMES_HOME,     │
   │        │      model-stream HTTP clients             │         HERMES_DELEGATE_PROFILE_DISABLE=1│
   │        └────────────────────────────────────────────┘                                         │
   └───────────────────────────────────────────────────────────────────────────────────────────────┤
                        │   hermes-memory-sidecar.service :8792  (out of repo; healthchecked only) │
                        │                                                                          │
                        │   TIMERS (systemd --user)                                                 │
                        │   ├─ hermes-router-sidecar-stale-check.timer  (boot+1m, every 2m)         │
                        │   │     → scripts/sidecar_stale_check.py: GET :8791/status,               │
                        │   │       stat newest router/*.py, systemctl restart if disk is newer     │
                        │   └─ hermes-stack-update.timer (Sat 04:15 ±30m, Persistent)               │
                        │         → scripts/update_hermes_stack.py apply --yes                      │
                        │                                                                          │
                        │   HERMES CRON (external scheduler, not in repo)                           │
                        │   ├─ python -m router.price_watch_runner --state …  (daily; vendor pages) │
                        │   └─ scripts/cron/watch-webui-prs.py                (upstream PR watch)   │
                        └──────────────────────────────────────────────────────────────────────────┘
```

**Prose.** Five kinds of runtime exist.

1. **Plugin-in-Hermes.** `register(ctx)` (`__init__.py:1671`) is the only host entry point. It registers one tool (`delegate_profile`, toolset `delegation`, `__init__.py:1767`) and two hooks: `post_tool_call` (`__init__.py:1778`) and `pre_kanban_dispatch` (`__init__.py:1785`). Only the first hook is declared in `plugin.yaml:10-11` — **the manifest is one hook behind the code**. Registration is idempotent per `id(ctx)` and marks the ctx serviced *last* so a mid-registration failure lets a retry re-run (`__init__.py:1787-1789`).

2. **Subprocess child.** `_attempt` builds `[hermes, -p, profile, chat, -q, prompt] (+ -m model) (+ --provider provider)` (`__init__.py:1489-1493`) and `_spawn` (`__init__.py:291`) launches it with `start_new_session=True` (POSIX) / `CREATE_NEW_PROCESS_GROUP` (Windows), capturing the pgid at spawn time. Two reader threads stamp a monotonic heartbeat; `_run_watched` (`__init__.py:421`) enforces a strictly nested ttfb ≤ idle ≤ hard ladder and tree-kills via `killpg` on any non-`exited` reason. An `atexit`-registered `_POOL.kill_all` guarantees no subagent outlives the interpreter (`__init__.py:1328`). Concurrency is bounded by a `BoundedSemaphore` pool (`__init__.py:1265`).

3. **Sidecar HTTP.** `router/one_sidecar.py` is a stdlib-only `ThreadingHTTPServer` bound loopback-only (`build_server` raises `ValueError` for a non-loopback host, `router/one_sidecar.py:755`), on port **8791** — a number duplicated in **six** non-doc places (argparse default `router/one_sidecar.py:765`, the unit's ExecStart, `webui_extension/hermes-smart-router/manifest.json:8`, `scripts/sidecar_stale_check.py:36`, `scripts/smoke-live-sidecar.sh:24`, and the weekly stack update's healthcheck `scripts/update_hermes_stack.py:587`), plus README×3, `HERMES_CUSTOMIZATION_MANIFEST.md`×7 and 2 tests. Changing the argparse default alone changes NOTHING on the box: `systemd/hermes-router-sidecar.service:17` passes `--port 8791` explicitly. Every route except `/health` and `GET /console` is gated on `X-Hermes-Sidecar-Token`, compared constant-time over *bytes* with surrogateescape (`router/one_sidecar.py:350-360`).

4. **Browser frontends.** Two, deliberately shape-parallel. The primary is the Hermes One extension: `router-nav.js` registers a rail button through `window.HermesPanelNav` and mounts `console.html` into an iframe via **`srcdoc`, never `src`** — the sidecar sends framing-deny headers, and srcdoc inherits the host origin, which is the only way the console can read the host's CSRF token (`webui_extension/hermes-smart-router/router-nav.js:21-27, 56-59`). The second is the older dashboard plugin: `dashboard/plugin_api.py` (FastAPI, seven read routes) + `dashboard/dist/index.js` (React over `window.__HERMES_PLUGIN_SDK__`).

5. **Cron/timers.** Two systemd user timers (stale-code poller, weekly stack update) and two Hermes cron jobs (price-page provenance watcher, upstream PR watcher). **Nothing in this repo enables the stale-check timer** — the installer writes the three files (`scripts/install_hermes_one_router.py:289-318`) but no script or doc runs `systemctl --user enable --now …stale-check.timer`.

**IPC is a file.** `routes.jsonl` is the channel between the plugin (single writer, running under a *profile-scoped* `HERMES_HOME` that varies per delegation) and the sidecar (reader, pinned to one profile). Both sides resolve the path through `routes_path()` (`router/durable_decision_log.py:60`), which peels a trailing `profiles/<name>` off `HERMES_HOME` so every profile and the sidecar converge on one file. `_state_dir()` (`router/blocklist.py:27`) does the same peel for `breaker-state.json`.

---

## 3. The routing decision pipeline, end to end

Two callers enter this pipeline. **Tool path:** `_route_task(goal, requested_model, classify_fn, prompt_text)` (`__init__.py:746`). **Kanban path:** `_on_pre_kanban_dispatch(...)` (`__init__.py:1055`), which reads the card's title+body via `_read_kanban_task`, passes `classify_fn=None` and `assignee=<card assignee>`, and logs into a `_KanbanShadowLog`.

| # | Stage | Module : function | What happens |
|---|---|---|---|
| 0a | Prompt composition | `__init__.py:683` `_compose_prompt` | `f"Context: {ctx}\n\nTask: {goal}"` or bare goal. Called **before** routing so the router sizes the turn from the exact bytes the child will receive (`__init__.py:1366-1368`). The ONE definition of the child prompt. |
| 0b | Sentinel clearing | `__init__.py:1373-1380` | An empty or `auto` profile is cleared. `auto` is never a profile name — clearing it makes a router decline surface as `profile is required` rather than advising `hermes profile create auto`. |
| 0c | Policy load | `__init__.py:605` `_load_router_config` | Re-read per decision (hot). Seeds `router.yaml` from `router.example.yaml` on first load; falls back to reading the example on a read-only install; `{}` on any exception — and because `enabled` defaults to **False** (`__init__.py:643, 782, 1084`), a malformed policy silently disables routing. |
| 1 | Price-window overlay | `router/rules.py:966` `with_global_price_windows` | Top-level model-keyed `price_windows` merged into every tier primary and hop that does not declare its own. Precedence: per-elo declaration > overlay > code registry. Called at `router/adapter.py:234`. |
| 2 | Impurity creation | `router/adapter.py:244-246` | `seed = None if rng is not None else _turn_seed(task)`; `turn_rng = rng or random.Random(seed)`; `when = now or datetime.now(timezone.utc)`. **The only wall-clock read on the decision path.** `_turn_seed` is blake2b, not `hash()`, because str hashing is `PYTHONHASHSEED`-salted and unreplayable. |
| 3 | Stage 0 — blocklist pre-filter | `router/blocklist.py:105` `is_blocked` ← `router/breaker.py:108` | Unions manual bans with breaker cooldowns into ONE boolean. A hit returns `finish("blocklist_veto", {deny: True, fallback: …})` before signal extraction (`router/adapter.py:301-315`). |
| 4 | Signal extraction | `router/signals.py:266` `extract(prompt_text or task)` | The 14-key flat vector. Pure, depth ≤ 1, no clock, no IO (AST-asserted). |
| 5 | Feature injection | `router/adapter.py:504` `_clock_features` + `:489` `_role_features` | Adds `utc_hour`/`utc_weekday` (0 = Monday) and `assignee`. Absent means **absent**, never zero — a time- or role-keyed clause is inert rather than matching a guessed value. |
| 6 | Table 1 first match | `router/rules.py:202` `match` → `:1802` `_all_clauses_match` → `:1851` `_eval_clause` | Top-down, no priorities. `enabled: false` rows skipped; an empty `then` is skipped so the row behind decides; the default always produces an output. Closed operator set (`_VALID_OPS`, `router/rules.py:100`); `matches` gated to `verb_class` alone. |
| 7 | Tier alias resolution | `router/rules.py:243` `resolve_tiers` → `:1889` `_resolve_tiers` | `Tn` → model + provider + fallback[] + the tier's planning knobs (`fallback_strategy`, `pin_primary`, `billing_mode`, `requirements`, `time_cap`, `time_policy`, declared capabilities), copied never shared. |
| 8 | **Gate 1** — concrete route? | `router/adapter.py:353` | If `"action" not in output and output["model"]` → jump to stage 13. Otherwise fall into Stage 1. |
| 9 | Stage 1 — exact-hash cache | `router/cache.py:18` `hash_task` (sha256 of `normalize(task)`[:16]) | Keyed on the **goal alone**, not the composed prompt. Cold in production (no live caller passes `cache=`). |
| 10 | Stage 1 — classifier | `__init__.py:635` `_make_classify_fn` → `ctx.llm.complete` → `router/classify.py:69` `build_prompt` | 4 anchored tiers (T1 TRIVIAL … T4 HARD), temp 0, ~128 tokens. Gated by the host trust grant `plugins.entries.<id>.llm.allow_{provider,model}_override` and **fails closed**. |
| 11 | Safety ratchet | `router/classify.py:110` `safety_ratchet` | Upward-only. Unknown tier → T4; any confidence ending in `low` bumps one tier. |
| 12 | Session-pin floor | `router/adapter.py:1124` `_apply_session_floor` → `:1081` `_resolve_tier_cfg` → `:1102` `_adopt_tier_policy` | Upward-only tier floor; promotes route **and** policy together, evicting `_TIER_POLICY_KEYS` the new tier does not declare. Cold in production (no live caller passes `session_pin=`). |
| 13 | **Terminal funnel** | `router/adapter.py:257` `finish(cause, output, matched_rule_id=)` | Three ordered steps, and the order is the invariant: **plan → vet → record**. |
| 13a | Chain planning | `router/adapter.py:520` `_plan_chain_for` → `router/rules.py:248` `plan_chain` | Runs **last**, after the pin floor, because the floor identifies a tier by looking `output["model"]` up in the tier table. Fixed stage order inside: `derive_requirements` (`router/capabilities.py:1803`) → `filter_chain` (`:1342`, membership by capability) → `_apply_time_cap` (`:1658`, membership by dollar ceiling) → `_apply_time_policy` (`:1513`, position) → `_effective_strategy` (`router/rules.py:1553`) → `order_chain` (`router/capabilities.py:1429`) → `independent_rails` (`:1890`) → `_multipliers_for`. |
| 13b | Veto | `router/adapter.py:657` `_veto_blocked` → `:893` `_vet_plan_chain`, `:799` `_reachable_replacement`, `:631` `_selection_vetoes` | Vets the declared primary **first**, then every planned hop, against `Blocklist.is_blocked(model, that hop's own provider)` AND Hermes' `model_selection_guards.selection_warnings`. Substitutes, widens to the declared clean hops, denies, or bypasses loudly. |
| 13c | Record | `router/decision_log.py:399` `DecisionLog.record` (or `DurableDecisionLog` / `_KanbanShadowLog`) | Coerces an out-of-set cause to `unknown_cause`; stamps `attempted_model`/`attempted_provider` from `plan_head_of(bounded)` onto a **copy** of the output; bounds `rejected` to 8 with a `rejected_truncated` count. |
| 13d | Chain attachment | `router/adapter.py:546` `_with_chain` | Attaches `chain` **only when the plan differs from the declared order** — its absence means "the declared order stands", which is byte-identically what the executor rebuilds. |
| 14 | Executor target list | `__init__.py:706` `_routed_targets` → `:730` `_dedupe_targets` | Prefers the planned `chain`; otherwise rebuilds `[model] + fallback`. When the caller named no model, `targets[0]` is the **planned head**, not the declared tier primary. A target is never attempted twice. |
| 15 | Attempt loop | `__init__.py:1481` `_attempt` → `_spawn` → `_run_watched` | Per target: build argv, spawn into its own session, drive the watchdog ladder. |
| 16 | Failure classification | `__init__.py:501` `_classify`, then `:583` `_is_exhaustion`, then `:524` `_reported_agent_failure` | Fixed override order: exhaustion → `quota_exhausted`; else a `None` kind + a reported CLI failure → `agent_error`. A nonzero-exit child is never reclassified as `agent_error`. |
| 17 | Breaker feedback | `__init__.py:1213` `_record_breaker_outcome` → `router/blocklist.py:150/176` | Fresh `Blocklist` per delegation; key is `model@provider` with the **attempted** provider passed positionally; load→mutate→save under a per-path process-wide lock. Fire-and-forget (`except Exception: pass`). |
| 18 | Loop control | `__init__.py:1597-1598` | Continue only while `retryable`; `bad_args`/`unknown_profile`/`binary_not_found`/`nonzero_exit` stop the loop. Pool slot released in a `finally`. |

**Kanban divergence:** stages 9–11 are skipped entirely (no classifier per card — that is the per-turn cost the design keeps off the hot path). The decision's `profile` half is unusable because `kanban_db._PRE_DISPATCH_MUTABLE_FIELDS` is exactly `(model, provider)`; `_kanban_role_out_of_scope` (`__init__.py:952`) is the ONE definition of that question, shared by the trace stamp and the live return path. In shadow mode the hook returns `None`; in live mode it returns `{model, provider}` — but refuses a head whose provider is empty (`__init__.py:1006-1011`).

---

## 4. Module map

Purity legend: **pure** = no IO, no clock, no global randomness, no module-level mutable state; **pure+state** = in-memory mutable state only; **edge** = injected impurity or Hermes-coupled but no IO of its own; **IO** = touches filesystem, sockets, processes, or env.

### Runtime source

| Path | LOC | Purity | Owns |
|---|---|---|---|
| `__init__.py` | 1793 | IO | The entire plugin boundary: `register(ctx)`, the `delegate_profile` tool + its JSON envelopes, the watchdog ladder, process-group spawn/tree-kill (`_spawn`/`_kill_tree`/`_run_watched`/`_Tail`), failure taxonomy (`_classify`/`_is_exhaustion`/`_reported_agent_failure`), the router seam (`_route_task`/`_make_classify_fn`/`_routed_targets`), the bounded pool + atexit registry, the kanban shadow/live hook + `_KanbanShadowLog` + `shadow_gate_rate`, breaker feedback, and the advisory `post_tool_call` hook. |
| `plugin.yaml` | 12 | — | Hermes manifest: `name: delegate-profile`, v0.3.0, `provides_tools: [delegate_profile]`, `provides_hooks: [post_tool_call]`. Behind the code (no `pre_kanban_dispatch`) and describes nothing of the router. |
| `router/__init__.py` | 1 | pure | One comment declaring the package contract: "pure core (no IO, no state, no model calls)". No code, no exports. |
| `router/rules.py` | 2310 | **pure** | The policy language: `match`/`resolve_tiers`/`plan_chain`/`explain`, the fail-closed `lint` + `lint_findings` + advisory `lint_warnings`, the closed operator/output/time-knob sets, shadow-containment algebra, `with_global_price_windows`, and the clock/rng injection points. |
| `router/capabilities.py` | 2530 | **pure** | `MODEL_CAPABILITIES` (43 elos, 9 providers, each citing a first-party page + read date), the closed key sets, `capabilities_for`/`satisfies`, the four chain-shaping stages (`filter_chain`/`apply_time_cap`/`apply_time_policy`/`order_chain`), the price-window vocabulary + pricing functions, `derive_requirements`, `upstream_group`/`independent_rails`, and the three lint validators `rules.lint` appends verbatim. |
| `router/signals.py` | 559 | **pure** | `extract(turn)` → the 14-key flat vector, plus `EXTRACTED_/INJECTED_/KNOWN_FEATURE_NAMES` — the `when` field vocabulary `rules.lint` validates against. AST-guarded against any clock read or IO import. |
| `router/classify.py` | 157 | **pure** | Stage-1 rubric only: `TIER_ANCHORS` (T1–T4), `build_prompt`, `build_prompt_from_config`, and the upward-only `safety_ratchet`. No model call, no hardcoded tier table (absent `tiers` → `{}`, never a stale default). |
| `router/breaker.py` | 343 | **pure** | The `(state, event, timestamp) → (new_state, blocked_set)` circuit reducer: `FAILURE_WEIGHTS`, the sliding window, exponential cooldown backoff, the single-probe HALF_OPEN slot, versioned (de)serialization. Clock is a parameter. |
| `router/threshold.py` | 82 | **pure** | Compaction-threshold curve for Hermes core config (`p_eff`, `summarizer_cap`, `apply_dynamic_thresholds`). **Not on the routing path** — its only consumer is `router/one_sidecar.py`. |
| `router/cache.py` | 75 | pure+state | The stateful shell's two objects: the exact-hash task cache (`normalize` → `sha256[:16]`) and `SessionPin`, the upward-only tier floor. |
| `router/decision_log.py` | 524 | pure+state | The in-memory append-only log: `VALID_CAUSES` (14, closed) + `unknown_cause` coercion, `CHAIN_PLAN_KEYS`/`empty_chain_plan`/`bound_chain_plan`/`chain_plan_of` (the persist + type-checked read-back whitelist), the `plan_head_of`/`attempted_head_of` pair, `attempts_of`, `format_line`. |
| `router/adapter.py` | 1280 | edge | The decision edge: `route()` → the single terminal `finish()` funnel; creation of the per-turn seed and the one wall-clock read; role/clock feature injection; the blocklist + selection-guard veto over the **plan**; the session-pin floor; `_RULE_ID_CAUSES`/`_cause_from_rule` (the one rule-id→cause table `rules._determine_cause` fetches at call time); the fail-safe paths. |
| `router/blocklist.py` | 299 | IO | The ONLY owner of mutable ban state: manual-ban matching, the flat positional `fallback_chain`, `breaker-state.json` load/atomic-save, and the per-path process-wide lock over load→mutate→save. |
| `router/durable_decision_log.py` | 313 | IO | One JSON line per decision appended to `routes.jsonl`: `routes_path()`/`attempts_path()` (profile-peeled), the in-process write lock (a trace line can exceed `PIPE_BUF`), size-bounded rotation (5 MiB × 4), `merge_attempts` (the executor journal join), and never-raising readers. |
| `router/service.py` | 2457 | IO | `RouterService` — the shared read model for every web surface (`status`/`policy`/`blocklist`/`liveness`/`capabilities`/`explain`/`lint`/`routes`/`route`), the **only** guarded write path (`plan`/`apply`/`apply_revert`: `_HOT_KEYS` allowlist, lint gate, `base_hash` optimistic concurrency, `.bak` snapshot, temp+`os.replace`), plus `resolve_compaction_auxiliary`. Also the module's single `_utc_now`, truncated to the hour. |
| `router/cli.py` | 1235 | IO | The shell-side governance surface, "the tool of last resort": `explain`/`chain`/`lint`/`blocklist`/`log`, the `--at`/`--time-agnostic`/`--seed`/`--prompt-text` injection points, the four-step guarded plan-resolution ladder, the pricing rows, and the human-readable chain-plan renderer. |
| `router/one_sidecar.py` | 788 | IO | The loopback HTTP host: token resolution + constant-time auth, the two route frozensets, `SidecarApp.dispatch` (socket-free), gzip negotiation with a console byte-cache, the static `/console` shell, and the RESTART-class compaction staging that hands a candidate config to an out-of-repo dead-man switch. |
| `router/price_watch.py` | 259 | IO (no network) | The offline pricing-page change detector: `ProviderAdapter` (anchor → literal-line extraction with a page-metadata refusal), `WatchResult`'s five outcome buckets, atomic JSON state, and the review-card emission. Imports nothing from Hermes. |
| `router/price_watch_runner.py` | 238 | IO | The executable cron edge: `DEFAULT_ADAPTERS` (five watched supplier clauses), the credential+policy registration gate, the one `urllib` fetch, kanban card creation, `run_daily`, `main`. |
| `router/fixtures/anchors.yaml` | 20 | data | Four few-shot classifier anchors, one per tier. **Loaded by nothing** — `Classifier(config)` is constructed with no anchors. |
| `router.example.yaml` | 592 | data | The tracked shipped policy AND the repo's longest design document (~70% prose): `enabled`, `shadow`, `classifier`, `fail_safe`, `blocklist`+`auto_breaker`, `compaction`, Table 1 (8 rules), `default`, Table 2 (T1–T4). |
| `router.yaml` | 592 | data (gitignored) | The live hot policy. **Currently byte-identical to the example** (verified `diff -q`). Re-read per request; `rm` re-seeds. |
| `dashboard/plugin_api.py` | 690 | IO | The dashboard plugin backend: seven read routes (`/status`, `GET`+`POST /explain`, `/lint`, `/blocklist`, `/log`, `/rules`), delegating shared composition to `RouterService` private helpers via `getattr` with local mirrors, injecting the clock and the sized-from prompt at the edge. |
| `dashboard/manifest.json` | 13 | data | Dashboard descriptor: `name: hermes-smart-router`, tab `/router` after `skills`, entry `dist/index.js`. |
| `dashboard/dist/index.js` | 320 | edge (browser) | Five React cards (Status/Rules/Blocklist/Explain/Log) over the plugin SDK. Registers under `"delegate-profile"` against `/api/plugins/delegate-profile` — **disagrees with its own manifest**. Untested, un-CI'd, and predates every rule in `DESIGN.md` (hex tier colours, raw `JSON.stringify` dumps, goal-only `/explain`). |
| `webui_extension/hermes-smart-router/console.html` | 11739 | edge (browser) | The whole operator console in one file: exactly one inline `<style>` and one inline `<script>` IIFE (verified), the six-panel markup, all state, the sidecar I/O wrapper `call()`, ~14 renderers, the write spine (`plan`→`doApply`), the `WRITE` phrase map, the JSON editor + scanner, and `globalThis.__router` for tests. |
| `webui_extension/hermes-smart-router/router-nav.js` | 269 | edge (browser) | The mount, not a UI: registers the Router rail button with `HermesPanelNav`, creates the `.main-view` panel with an iframe, fetches `/console` on every open and swaps only on byte change, mirrors the console's six tabs into the host sidebar via `MutationObserver`. |
| `webui_extension/hermes-smart-router/router-nav.css` | 96 | data | Dresses exactly two things in the HOST document (nav button, sidebar rows); restricted to the four host-defined tokens. |
| `webui_extension/hermes-smart-router/manifest.json` | 12 | data | Extension descriptor: id, one script, one stylesheet, sidecar origin `http://127.0.0.1:8791`, `health_path: /health`, `proxy_auth: token-v1`. |
| `webui_extension/hermes-smart-router/DESIGN.md` | 609 | doc | The durable information-design contract for the console (three questions, colour/meaning split, clock rule, §7's test-enforced invariant list). |
| `scripts/__init__.py` | 0 | — | Exists only so `from scripts import …` works from the suite. |
| `scripts/install_hermes_one_router.py` | 374 | IO | Extension+units installer: copies assets (never symlinks), merges `extensions.json` preserving siblings, sweeps `_RETIRED_EXTENSION_IDS`, renders both units, and raises `ProfileHomeRefused` before writing anything when asked to bake an *inherited* agent-profile `HERMES_HOME` into an operator unit dir. |
| `scripts/update_hermes_stack.py` | 741 | IO | Transactional 3-component replacement for the banned `hermes update`: snapshot (bundles+patches+tarballs) → merge into the *active local branch* → reinstall router bundle → validate → restart Router→Memory→WebUI → healthcheck, with automatic rollback and a per-user flock. |
| `scripts/install_hermes_stack_updater.py` | 184 | IO | Installs that controller as `hermes-stack-update.service` + weekly `.timer`, and copies it into the live plugin checkout. |
| `scripts/collapse_profile_routing.py` | 669 | IO | One-shot dry-run-by-default migration removing `('model',)`, `('fallback_providers',)`, `('auxiliary','vision')` — PLUS a conditional prune of an emptied `auxiliary` parent (`:112-114`) and, for a mapping-shaped `auxiliary.vision`, only `_NESTED_TARGET_KEYS` inside it with every sibling preserved (`:120`) from every `~/.hermes/profiles/*/config.yaml`, with mode preservation, pre-flight parse of all targets, and a post-write re-parse of every discovered file. |
| `scripts/sidecar_stale_check.py` | 95 | IO | The stale-code poller: read `/status`'s `process_started_at`, stat newest `router/*.py`, `systemctl --user restart` when disk is newer. Always exits 0. |
| `scripts/smoke-live-sidecar.sh` | 87 | IO | On-box live smoke — **21** `chk` assertions, not the 8 once listed here: restart, `/health`, console tab marker, 401/200 token gate, 409 stale apply, 400 missing compaction confirm, live `router.yaml` md5 unchanged, real route → `/routes` replay, the `/status` provenance triple (`process_started_at`/`code_mtime`/`config_mtime`), the fresh-boot "code not newer than process" check, `/liveness` 200, `/routes?id=nope` → 404, and the plan validity/`base_hash` pair. |
| `scripts/cron/watch-webui-prs.py` | 207 | IO | Unrelated stdlib-only cron watchdog over four upstream `hermes-webui` PRs; silent unless something changed. |
| `systemd/hermes-router-sidecar.service` | 25 | data | Unit template (`@PLUGIN_DIR@`/`@HERMES_HOME@`/`@WEBUI_STATE_DIR@`/`@PYTHON@`), `--host 127.0.0.1 --port 8791`, `Restart=always`, `PrivateTmp=true`, `PYTHONDONTWRITEBYTECODE=1`. |
| `systemd/hermes-router-sidecar-stale-check.service` | 10 | data | `Type=oneshot` for the poller; **no `[Install]`** — timer-triggered only. |
| `systemd/hermes-router-sidecar-stale-check.timer` | 15 | data | Placeholder-free: boot+1m, every 2m, `AccuracySec=30s`. Records why a poller replaced the `.path` unit. |
| `pyproject.toml` | 86 | data | Metadata-only build (`py-modules = []` disables auto-discovery — nothing importable is installed), pytest ini (`pythonpath = ["."]`, `--strict-markers`, `filterwarnings = error`), coverage (`branch = true`, omit `tests/ scripts/ .worktrees/`). |
| `.github/workflows/ci.yml` | 128 | data | Two jobs: `test` (3.11+3.12, `--cov-fail-under=100`) and `webui` (JS suites by **glob**, `node --check` of shipped assets, the one-inline-script assertion). |
| `README.md` | 356 | doc | The public promise. Stale in ~6 places (see §8). |
| `PRODUCT.md` | 90 | doc | Product schema + the five numbered Product Principles that constrain future work. |
| `.gitignore` / `LICENSE` | 44 / 21 | data | Ignores `router.yaml`, `state/`, `*.db`, coverage artifacts, `.worktrees/`. MIT. |
| `docs/superpowers/specs/2026-07-21-capability-router-design.md` | 292 | doc | v1 contract: HYBRID gating, the three-layer split, the two-table non-Turing-complete rule format, the 4-tier rubric, the YAGNI defer list. |
| `docs/superpowers/specs/2026-08-17-conditional-routing-design.md` | 969 | doc | v2 contract revised *against the shipped code*: context signals, the frozen `capabilities.py` interface, per-tier knobs, the fixed stage order, the lint error/warning split, two retrospectives. |
| `docs/superpowers/specs/2026-08-17-time-windowed-routing-addendum.md` | 510 | doc | The time layer: which vendors price by clock, `price_windows` as the one encoding, the pricing functions, `cheapest_now`'s billing bucketing, the `demoted` (position) vs `peak_priced` (price) split. |
| `docs/operations/CONDITIONAL_ROUTING_DEPLOY.md` | 318 | doc | Deploy/rollback: restart classes, the profile-collapse step, verification that checks the *running* path, and a §6b table of 168-hour behavioural partitions. |
| `docs/operations/HERMES_CUSTOMIZATION_MANIFEST.md` | 564 | doc (pt-BR) | Box inventory across three repos + the reconciliation routine + the `update_hermes_stack.py` contract. Dated snapshot (2026-07-27). |
| `docs/operations/HERMES_EXTENSION_NAMING_MIGRATION.md` | 204 | doc (pt-BR) | Extension-ID renaming programme; the seven compatibility contracts an ID with a sidecar carries; phases A–E. |

### Test suite

| Path | LOC | Owns |
|---|---|---|
| `tests/conftest.py` | 551 | The containment layer: autouse `_no_real_spawn` (`:453`) stubs `_spawn`/`_run_watched` on every live plugin copy (identified by **resolved source path**, never module name) and fails loudly if it cannot, with a per-test canary proving the scope; the `real_spawn` marker + `DELEGATE_PROFILE_E2E` two-key escape hatch; module-level `router.yaml` seeding (`:31`); autouse `_isolate_route_trace` (`:523`) redirecting `HERMES_ROUTE_TRACE_FILE` and neutralising `cfg_get`. |
| `tests/router/test_rules.py` | 4826 | Matching, lint, explain, tier knobs, `plan_chain` stage order, shadow edges, degrade ladders, version-skewed registry, the child-interpreter package-shape guard, the purity AST guard. |
| `tests/router/test_service.py` | 3892 | Every `RouterService` method and degrade branch; clock pinned everywhere; agreement asserted against `adapter.route`, `capabilities.effective_price`, and the plugin's `_routed_targets`. |
| `tests/router/test_capabilities.py` | 3490 | The registry shape, fail-open/fail-closed rules, 168-hour week sweeps, the `_BILLING_RANK` unit argument, and an AST test forbidding any clock read or IO import. |
| `tests/router/test_cli.py` | 2414 | Every rendered CLI line and degrade path; owns the file-wide autouse `_frozen_clock`. |
| `tests/router/test_adapter.py` | 2298 | Stage 0/1, seed, injected clock, planner degradation, `TestTheVetoBindsWhatRuns`' four incident shapes, half-edited-policy substitution, role features. |
| `tests/test_webui_extension.py` | 2247 | Static contract over the extension assets (XSS sinks, one wall clock, six tabs, panel placement, pt-BR vocabulary, write-label single-source, CSS token scope) + `plugin_api` ↔ `RouterService` shape parity. |
| `tests/test_delegate_profile_runtime.py` | 1257 | Hermetic runtime path with every host/process seam faked; failure envelopes, exact argv+env, `_routed_targets`, planned-vs-declared failover, the three-surface breaker-key agreement. |
| `tests/router/test_one_sidecar.py` | 1168 | Socket-free dispatcher: token gate outcomes, per-route method guard, compaction refusals, provenance, console↔route-table agreement. |
| `tests/test_delegate_profile.py` | 965 | Arg validation, profile resolution, the same-profile inline path, ladder/config resolution, the **real POSIX process-tree tests**, `_Pool`, the `post_tool_call` keyword contract, and the opt-in cross-profile E2E. |
| `tests/router/test_signals.py` | 757 | Feature extraction, vision both directions, whole-word markers, exported-vocabulary anti-drift, purity AST guard. |
| `tests/test_collapse_profile_routing.py` | 694 | Purity, three fixture profile shapes, **mode preservation**, post-write re-parse, exit codes, partial-write reporting. |
| `tests/router/test_decision_log.py` | 644 | `unknown_cause`, the attempted head (writer/reader agreement), planner↔read-back key pairing on real phase-2 plans. |
| `tests/test_kanban_shadow_dispatch.py` | 630 | The whole kanban subsystem incl. the `shadow_gate_rate` arithmetic and boundary. |
| `tests/test_router_integration.py` | 620 | The `_route_task` bridge, the per-thread recursion guard, argv-level proof the routed target is dispatched, and `test_spawn_guard_applies_to_the_module_under_test`. |
| `tests/router/test_price_windows_overlay.py` | 555 | The overlay's three-level precedence, hot round-trip, defect-naming refusals, `price_windows_origin` on every read surface. |
| `tests/router/test_edge_cases.py` | 544 | The branch-coverage tail across adapter/blocklist/breaker/rules/cli, incl. the concurrent `record_failure` lost-update regression. |
| `tests/router/test_breaker.py` | 522 | CLOSED→OPEN→HALF_OPEN→CLOSED, serialization, blocklist integration, the two probe-slot regressions; autouse hermetic `HERMES_HOME`. |
| `tests/test_install_hermes_one_router.py` | 522 | Manifest idempotence/ordering, retired-id sweep, `ExecStart` interpreter, the four `ProfileHomeRefused` acceptance cases. |
| `tests/router/test_one_sidecar_e2e.py` | 500 | Real loopback server: handler dispatch, gzip/Vary/Cache-Control, console byte-exactness + gzip cache, malformed-JSON 400, loopback guard, `main()`. |
| `tests/router/test_durable_decision_log.py` | 402 | Path resolution, the measured disk ceiling, oversized-entry exception, OSError swallowing, two-readers-agree. |
| `tests/router/test_price_watch.py` | 353 | Card-only-on-change, registry immutability, fetch-failure state preservation, the four `og:`/`meta`/`title` refusals, the permanent-unwatchable bucket, silent re-anchor rebaselining. |
| `tests/test_shipped_policy_names_real_rails.py` | 274 | The registry's alias notes as a machine-checked contract against the shipped policy; `fail_safe == T1`; `classifier pair == chain[0]`; `fallback_chain == tier union`. |
| `tests/router/test_price_watch_runner.py` | 271 | The authority gate, secret-free `.env` name reading, fail-closed policy parsing, the literal xiaomi/zai anchor strings. |
| `tests/router/test_shell.py` | 215 | Blocklist matching, cache normalization, session-pin ratchet, `DecisionLog` basics. |
| `tests/test_update_hermes_stack.py` | 208 | Dirty-tree survival, snapshot recovery after a stash conflict, archive-escape refusal, timer shape, cache-before-restart ordering. |
| `tests/router/test_classify.py` | 156 | Prompt construction and the ratchet (casing/hedge normalisation, unknown → T4, absent tiers → `{}`). |
| `tests/router/test_replay_end_to_end.py` | 153 | The whole replay chain with production wiring: `route()` → durable log → service → authenticated sidecar dispatcher. |
| `tests/router/test_write_path_end_to_end.py` | 149 | The operator's exact console sequence (plan→inspect→apply→confirm→revert), asserting on `router.yaml` bytes. |
| `tests/test_classifier_trust.py` | 148 | Both halves of the host LLM-trust contract, and that the shipped classifier provider is one a tier uses. |
| `tests/test_exhaustion.py` | 114 | `_is_exhaustion`, `_reported_agent_failure`, `_TERMINAL_FAILURE_RE`, the false-positive boundary; asserts `FAILURE_WEIGHTS['quota_exhausted'] >= 5`. |
| `tests/test_sidecar_stale_check.py` | 100 | Stale restarts; fresh does not; an unreachable `/status` must not restart. |
| `tests/test_threshold.py` | 84 | The calibrated curve, the 512k floor, the 650k summarizer cap, no-mutation/idempotence. |
| `tests/test_js_suites_run.py` | 50 | The anti-rot gate: every `tests/test_*.js` is executed **by glob** from inside pytest; an empty glob fails. |
| `tests/test_console_logic.js` | 13003 | 496 Node cases running the console's IIFE in a `vm` over a hand-written DOM stub: replay, write spine, JSON scanner, pt-BR vocabulary, responsive swaps, price/time layer, a11y — and a cross-check of price windows against `router/capabilities.py` via a real `python3` subprocess. |
| `tests/test_router_nav_mount.js` | 196 | 6 cases pinning `router-nav.js` as mount-only (srcdoc never src, refetch-on-reopen, byte-identical means no swap, a failed refetch keeps a live console). |
| `tests/fixtures/render_sheet.outer.html` | — | The byte-for-byte `#sheet` `outerHTML` golden that froze the DOM across the `renderSheet` refactor. |

---

## 5. State and files on disk

### Read and written at runtime

| Path | Who | Mode | Notes |
|---|---|---|---|
| `<plugin>/router.yaml` | plugin `_load_router_config` (`__init__.py:605`); `RouterService._load` (`router/service.py:840`); `router.cli --config`; sidecar `--config` | R per request; W via `plan`/`apply` | Gitignored. Seeded from `router.example.yaml` by the plugin only (never by `RouterService`). Hot. |
| `<plugin>/router.yaml.bak` | `RouterService.apply` / `apply_revert` (`router/service.py:2229, 2238`) | W then R | **Exactly one level deep** — two applies cannot both be undone. Written *before* the config; a no-op apply is refused so it cannot spend the one revert. |
| `<plugin>/router.example.yaml` | plugin seeding; read-only-install fallback | R | Tracked. The readable original — `yaml.safe_dump` on the write path destroys comments. |
| `<hermes-root>/hermes-smart-router/state/breaker-state.json` | `router/blocklist.py:44` `_state_path` | R/W atomic (`mkstemp` + `os.replace`) | Profile-independent (`profiles/<name>` peeled). Never created by a read path. **Never pruned** — a CLOSED entry with an empty event list persists forever. |
| `<hermes-root>/hermes-smart-router/state/routes.jsonl` (+ `.1`…`.3`) | `router/durable_decision_log.py:83` | W append (plugin, single writer); R (sidecar, `RouterService`, `shadow_gate_rate`) | ≤ 5 MiB × 4 ≈ 20 MiB, rotated *before* a line would cross the cap. One documented exception: an entry larger than the cap lands whole in its own file. `HERMES_ROUTE_TRACE_FILE` overrides. |
| `<hermes-root>/hermes-smart-router/state/attempts.jsonl` | `router/durable_decision_log.py:86` `attempts_path` | R only (this repo) | Written by Hermes **core**; schema `route-attempts/1`; joined on `(task_id, run_id)` by `merge_attempts` on every read. No size or age bound anywhere in this repo. |
| `~/.hermes/config.yaml` (or `profiles/<p>/config.yaml`) | `_watchdog_cfg` (`__init__.py:158`) R; sidecar compaction R + candidate | R; never written in place | RESTART-class. The compaction apply stages a `mkstemp` candidate and hands it to `~/bin/hermes-safe-restart.sh`. |
| `<state-dir>/sidecar-auth/hermes-smart-router.token` | `resolve_token_path` (`router/one_sidecar.py:176`); `scripts/sidecar_stale_check.py:32-35` | R | Minted by Hermes One on operator consent. Ladder: `HERMES_EXT_SIDECAR_TOKEN_FILE` > `HERMES_WEBUI_STATE_DIR/…` > `HERMES_HOME/webui/…` > `%LOCALAPPDATA%` > `~/.hermes/webui/…`. |
| `<plugin>/webui_extension/hermes-smart-router/console.html` | `render_console` (`router/one_sidecar.py:300`); gzip cache `_gzip_console` (`:312-324`), stat-keyed on `(st_mtime_ns, st_size)`; missing file -> JSON 404 at `:310` | R | Served at `GET /console`; a missing file is a JSON 404, not a crash. |
| `~/.hermes/profiles/<p>/state.db` | the delegated CHILD | W | The only witness the opt-in E2E test has; requires `HERMES_STATE_DB_GUARD_BYPASS=1` in the child env. |
| `~/.hermes/profiles/<p>/webui/models_cache.<p>.json` | `scripts/update_hermes_stack.py:580` | unlink | Deleted **before** the WebUI restart (the WebUI imports Hermes in-process). |
| `~/hermes-one-extensions/extensions.json` + `hermes-smart-router/` | `scripts/install_hermes_one_router.py` | R/W (`rmtree` + `copytree`) | Sibling entries and order preserved; retired ids swept from both the manifest and disk. **Hand edits inside the destination dir are destroyed on every install.** |
| `~/.config/systemd/user/hermes-router-sidecar*.{service,timer}`, `hermes-stack-update.{service,timer}` | the two installers | W | Templates are placeholder-driven; a missing or unknown `@TOKEN@` is a hard error. |
| `~/.hermes/update-backups/hermes-stack/<UTC>-<hex8>/` | `scripts/update_hermes_stack.py:287` | W | `repos/*.bundle`, `*.patch`, `*.untracked.tar.gz`, `support.tar.gz`, `metadata.json` (paths + SHAs only, never credentials). |
| `~/.hermes/update-locks/hermes-stack.lock` | `_lock` (`scripts/update_hermes_stack.py:648`) | flock | Exclusive; taken even by `status`/`plan` (both fetch). |
| `~/.hermes/backups/collapse-profile-routing-<stamp>/` | `scripts/collapse_profile_routing.py:290` | W | The only recovery from the collapse (PyYAML cannot preserve comments). |
| `~/.hermes/profiles/*/config.yaml` (×15) | `scripts/collapse_profile_routing.py` | R; W only with `--apply --stamp` | Removes three key paths plus the two conditional behaviours above; mode carried across the atomic replace. |
| `~/.hermes/state/webui-pr-watch.json` | `scripts/cron/watch-webui-prs.py:21` | R/W atomic | The review-id watermark. |
| price-watch state (`--state`, path not recorded in-repo) | `router/price_watch.py:93` | R/W atomic | `providers.<key>.{url, anchor, literal, sha256, verified_at, last_failure*, permanent, since, reason}`. |
| `~/bin/hermes-safe-restart.sh` | `router/one_sidecar.py:148-167` | exec | Out-of-repo dead-man switch: owns config.yaml mutation, backup, restart, health poll, auto-revert. 30 s timeout calibrated to a launcher that returns immediately. |

### Environment

**Read:** `HERMES_PROFILE`, `HERMES_HOME`, `HERMES_ROUTE_TRACE_FILE`, `HERMES_KANBAN_BOARD` (default `capability-router` — `router/price_watch_runner.py:171`, a pre-rename name nothing tracks), `HERMES_EXT_SIDECAR_TOKEN_FILE`, `HERMES_WEBUI_STATE_DIR`, `HERMES_CORE_CONFIG_FILE`, `HERMES_DELEGATE_PROFILE_{TIMEOUT,TTFB,IDLE,KILL_GRACE,MAX_CONCURRENT,QUEUE_WAIT}`, `HERMES_AGENT_DIR`/`HERMES_PLUGIN_DIR`/`HERMES_ONE_DIR`/`HERMES_ONE_EXTENSIONS_DIR`/`HERMES_SYSTEMD_USER_DIR`, `DELEGATE_PROFILE_E2E`, `HERMES_STATE_DB_GUARD_BYPASS`, `PYTEST_CURRENT_TEST`.
**Written into the child env:** `HERMES_PROFILE`, `HERMES_HOME` (only if absent), `HERMES_DELEGATE_PROFILE_DISABLE=1`.
**Written into the *parent* process env:** `HERMES_ROUTE_ATTEMPTS_FILE = str(attempts_path())` (`__init__.py:1110`) — because the kanban worker has a profile-scoped `HERMES_HOME` and does not load this plugin. Nothing in this repo reads it back, and because `_attempt` does `os.environ.copy()` it also leaks into later `delegate_profile` children.

---

## 5b. Compaction — the one write that leaves this repo

Everything else the sidecar writes lands in `router.yaml`, which only this plugin
reads. Compaction is the exception: it writes into **Hermes' own `config.yaml`**
and then restarts the gateway. It is the highest-blast-radius operation in the
system and it has its own confirmation phrase, so it gets its own section.

**What it is for.** Hermes compacts a conversation that outgrows its window by
summarising it with a *second, cheaper* model. Two things have to be decided: WHICH
model summarises (and what it falls back to), and AT WHAT FILL FRACTION compaction
should fire per main model. The first is declarative policy; the second is a curve.

**The declarative half** — `compaction:` in `router.yaml`. Shipped:
`{provider: zai, model: glm-4.5-flash, fallback_mode: "tier:T1"}`.

| key | meaning |
|---|---|
| `provider` / `model` | who summarises. **Held to a stricter bar than a routing elo: REFUSED when the capability registry cannot describe it**, where an unknown routing elo merely warns. A wrong routing elo degrades one request; a wrong compaction model fails exactly when the conversation is already too large to carry. |
| `base_url` | optional custom endpoint. |
| `fallback_mode` | OPTIONAL. Absent = no queue (Hermes uses the main agent's chain). `"standalone"` = this block carries its own `fallback_chain`. `"tier:<NAME>"` = a **reference** to that tier's queue, resolved at apply time and never copied into `router.yaml` — one authority, so editing the tier edits the compaction queue in the same breath. A tier deleted after the block was written is refused BY NAME at apply time. |
| `required_provider_context_window` | optional floor in tokens (`router/service.py:834`). |

`resolve_compaction_auxiliary(block, tiers)` (`router/service.py:692`) is the ONE
place that turns this into the `auxiliary.compression` mapping Hermes reads. Both
the write-path lint (`_validate_compaction`) and the RESTART-class apply call it,
which is what makes the refusal consistent between "the console would not let me
save this" and "the apply refused it".

**The curve half** — `router/threshold.py`, whose only consumer is the sidecar
(this is the "path it IS on"). `p_base(window) = 0.85 − 0.0776·log2(window/128000)`,
adjusted by `delta(aggressiveness) = 0.10 − 0.002·aggr`, clamped to `[0.55, 0.90]`
and floored at `0.75` below a 512k window. `summarizer_cap(w)` then derives the
largest source context that fits the summariser's budget. Calibrated from Vectara's
context-engineering finding that a large advertised window is not a usable one.

Its inputs come from `RouterService.compaction_windows()`: the key set is every
model the live policy can route to plus the compaction model, and each window is
the registry's `context_window`. A policy naming elos the registry cannot describe
serves an EMPTY threshold map rather than a wrong one — `p_base(0)` is a
math-domain error and a fabricated small window compacts far too early.

**The two surfaces.**

* `GET /compaction?aggr=0..100` — read-only preview: the per-model fractions, the
  summariser window, the derived `threshold_tokens`, plus the RESOLVED
  `compaction` block and any `compaction_errors`. A refusal rides in that field
  rather than as a 400, because this is a read path the console opens alongside a
  broken config.
* `POST /apply {action: "compaction", confirm: "COMPACT", aggressiveness: N}` —
  the RESTART class. `confirm` must equal `_COMPACTION_CONFIRM` exactly
  (`router/one_sidecar.py:123`), server-side as well as in the console. It reads
  Hermes' core config, produces a **candidate** via `apply_dynamic_thresholds`,
  merges the resolved `auxiliary.compression` into it (leaving every sibling the
  operator keeps under `auxiliary` untouched), writes the candidate to a
  `mkstemp` file, and hands the path to `~/bin/hermes-safe-restart.sh`.

**The dead-man switch is out of repo.** `hermes-safe-restart.sh` owns the actual
config mutation, the backup, the restart, the health poll and the auto-revert. This
repo only stages a candidate. Two consequences: nothing here can prove the restart
is safe, and the 30 s runner timeout is calibrated to a launcher that returns
immediately (`router/one_sidecar.py:148-167`). The candidate temp file is never
unlinked on success — the launcher owns it. Whether a detached `systemd-run` unit
can even read a path inside the sidecar's `PrivateTmp=true` namespace is an open
question (§9).

---

## 6. Invariants, ranked by blast radius

### Tier 1 — breaking these bills money or leaves orphaned processes

1. **The pgid must be captured at spawn and passed into `_kill_tree`** — `__init__.py:335-338`. Once the group leader is reaped its pgid is unresolvable, and the grandchildren (MCP servers, model-stream HTTP clients) hold sockets and burn API tokens. `_attempt` falls back to `proc.pid` if `os.getpgid` fails (`__init__.py:1517`).
2. **`_run_watched` never returns with a live tree** — `__init__.py:481-492`. Every non-`exited` reason tree-kills; `exited` waits within grace and then tree-kills.
3. **`atexit`-registered `_POOL.kill_all`** — `__init__.py:1266-1272, 1328`. No subagent outlives the interpreter.
4. **The test suite cannot reach a real dispatch without two independent keys** — `tests/conftest.py:99-123`. Marker *and* `DELEGATE_PROFILE_E2E=1`, resolved once per node; a guard that cannot install is a `pytest.fail`, never a silent pass (`tests/conftest.py:474-507`).
5. **A cost control must never cause an outage** — `router/capabilities.py:1784` (`apply_time_cap` bypasses when no named elo survives) and `:1409-1418` (`filter_chain` restores the original chain), with diagnostics **retained** so `bypassed` is the flag a consumer must check first.
6. **Never widen a blocklist lookup with `is_blocked(model, "")`** — `router/adapter.py:826-838, 934-938`. An empty *queried* provider matches a provider-scoped ban (`router/blocklist.py:246`), so ORing it in refuses rails the operator never named.
7. **Config deny rows fire independently of any state file** — `router/blocklist.py:73-78, 264-273`. A missing or corrupt `breaker-state.json` means empty cooldowns, never an open blocklist.
8. **`--apply` requires `--stamp`; dry-run is the default** — `scripts/collapse_profile_routing.py:611-615`. And every target is parsed before the first byte is written (`:450-451`).
9. **`hermes update` is never invoked; components advance by merge into the active local branch** — `scripts/update_hermes_stack.py:4-9, 436-437`. A reset would discard the core fork's memory patches.

### Tier 2 — breaking these silently routes traffic wrong

10. **One vetted terminal site, three steps in order: plan → vet → record** — `router/adapter.py:257, 287-297`. Recording before the veto once made a banned target the trace's chosen model while the caller got a substitution.
11. **`plan_chain` runs last, after the session-pin floor** — `router/adapter.py:17-20, 270-271`. The floor identifies a tier by looking `output["model"]` up in the tier table; a reordered head silently unenforces the ratchet.
12. **The head is never refused and the chain is never empty, simultaneously** — `router/adapter.py:706-715, 974-1006`, with the loud last-resort bypass at `:1017-1019`.
13. **`chain` is attached only when the plan changed the declared order** — `router/adapter.py:571`. Its absence is byte-identically what `_routed_targets` rebuilds (`__init__.py:706`).
14. **The router's `chain` is authoritative when present** — `__init__.py:706-727`. Rebuilding `[primary] + fallback` downstream is what kept the capability filter and `fallback_strategy` inert on live traffic while the console displayed a filtered chain.
15. **The breaker key `record_failure` writes must be the key `is_blocked` reads** — `__init__.py:1223-1240, 1538-1544`. The **attempted** provider is passed positionally, never re-derived. Three surfaces (running path, `breaker_status()`, `RouterService.liveness()`) are asserted to agree on one `model@provider` string.
16. **Route and policy always come from the same tier** — `router/adapter.py:58-79, 1119-1121`. `_adopt_tier_policy` evicts any `_TIER_POLICY_KEYS` the new tier does not declare; a T1→T3 ratchet once planned T3's chain with T1's `time_cap`.
17. **(model, provider) is one decision** — `router/adapter.py:436-441, 1266-1273`. The provider is assigned or **popped**; `setdefault` let `gpt-5.6-terra @ zai` reach a spawn.
18. **The requirements floor can only tighten** — `router/rules.py:1527-1550` `_tier_floor_of` drops falsy booleans, because `derive_requirements` unions by overwriting and `{vision: false}` would erase a signal-derived `vision: True` and route a screenshot to a blind model.
19. **`min_context` means INPUT tokens and is measured against `_input_ceiling`** — `router/capabilities.py:2081-2116`. `MAX_REGISTERED_CONTEXT` and `_unsatisfiable_requirements` use the same reading (`:1024-1027, 2132-2135`).
20. **The ladder is strictly nested `ttfb ≤ idle ≤ hard`, by clamping not validating** — `__init__.py:192, 205-206`.
21. **The cause set is closed; an unknown cause records as `unknown_cause`, never as `fail_safe_strong`** — `router/decision_log.py:441-446`. Recording it as the worst real outcome buries the upstream error exactly where an operator counts outcomes.
22. **One rule-id→cause labeller, fetched at call time** — `router/rules.py:2024-2030` imports `adapter._cause_from_rule` (a deliberate deferred-import cycle) so `/explain`, the sidecar, the dashboard and the trace cannot drift. Do not re-add a mirror table.
23. **`plan_chain`'s stage order is fixed: capability filter → time_cap → time_policy → fallback_strategy** — `router/rules.py:262-269, 320-333`. Membership before position, so a trace can never show a promotion a later filter removed.
24. **One clock reading, normalised once, used by every stage and every reported key** — `router/rules.py:304-314`. Deciding it per stage is how `time_agnostic: True` would sit next to a cap that fired.
25. **`when=None` is time-agnostic, never a wall-clock read; unknown time facts are OMITTED, not null** — `router/rules.py:275-282, 1759-1770`. `Number(null)` is 0 in JS: a null hour renders midnight and a null `time_cap` a 0× ceiling.
26. **The reported `strategy` is the one that actually ran** — `router/rules.py:290-297, 1553-1581`. A sequential chain labelled `random` is indistinguishable from a routing bug.
27. **A `None` price is never coerced to `0.0`** — `router/capabilities.py:1246-1247`. A fabricated 0.0 wins every cost comparison for the wrong reason.
28. **`cheapest_now` buckets by `billing_mode` first and compares dollars only inside a bucket** — `router/capabilities.py:2338-2363`; `subscription` shares the `metered` bucket deliberately (`:290-299`).
29. **`hours_utc` is half-open `[start, end)`; a midnight-crossing window is two entries; overlaps are a hard lint error** — `router/capabilities.py:1168-1173, 2266-2292`. This is what keeps wrap-around arithmetic out of every consumer.
30. **The ratchet fails upward** — `router/classify.py:130-135`. Unknown tier → T4; any confidence ending in `low` bumps one tier. A missed ratchet sends unsure work to the cheapest tier.
31. **`needs_tools` fails closed to True; `needs_vision` means visual *input* and matches on word boundaries** — `router/signals.py:185, 436-441`. Containment made `libpng-dev` a vision turn that lost both text-capable fallbacks.
32. **`blocked_model` is injected, never a feature, and the author's operator is evaluated** — `router/rules.py:1823-1838`. A hardcoded `eq` made `{ne: true}` false exactly when the model was healthy.
33. **`lint()` is hard errors only; warnings never block a write** — `router/rules.py:466-472, 669-686`. Undecidable shadow containment is silence, never an error (`:2107-2112`).
34. **The write path is lint-gated, hash-guarded, snapshot-then-write** — `router/service.py:2121-2127, 2208-2236`. `_HOT_KEYS` (`:352`) is the entire top-level write surface; removal is an explicit `null`; lists replace wholesale.
35. **Persisted trace fields are additive** — `router/decision_log.py:18-29, 162-176`. A new field means a new optional key + a whitelist entry + a type-checked read-back group; `output["model"]` keeps meaning the *declared* primary, `attempted_model` is the addition.
36. **Writer and reader share one definition of the chain head** — `router/decision_log.py:338` `plan_head_of` / `:361` `attempted_head_of`.
37. **Every intra-package import is relative-first, absolute-second, including function-scope ones** — `router/service.py:94-103`, `router/rules.py:54-60`, `router/one_sidecar.py:28-38`. Absolute-first meant that under Hermes' `hermes_plugins.<slug>.router.*` shape `_caps` fell to `None` and the whole capability/time layer went inert in the only shape production uses. Guarded by a child-interpreter test whose `sys.path` cannot reach the repo (`tests/router/test_rules.py:4528-4756`).

### Tier 3 — breaking these degrades an operator surface

38. **Everything except `/health` is token-gated; `/health` is 200 in both token states on purpose** — `router/one_sidecar.py:388-419`. The same process serves `/console`, and a 503 would take down the screen that must explain the failure. `/health` must resolve the token through the *same* authority as the gate (2026-08-26: for three hours `/health` said `ok` while every gated route answered 503).
39. **The token comparison is constant-time over BYTES with surrogateescape** — `router/one_sidecar.py:350-360`. `hmac.compare_digest` raises `TypeError` on a non-ASCII `str`, and http.server decodes headers as latin-1, so a header like `café` escaped dispatch and returned zero bytes instead of a 401.
40. **The server refuses to bind anything but loopback** — `router/one_sidecar.py:755-759`.
41. **Every response carries `Cache-Control: no-store` and `Vary: Accept-Encoding`, through one choke point** — `router/one_sidecar.py:694-718`.
42. **No raw-markup sink anywhere in the console's inline script** — asserted at `tests/test_webui_extension.py:119-129`. Decision traces carry attacker-influenceable task text; every string reaches the DOM via `textContent`.
43. **`nowUtc()` is the console's only wall-clock read** — `webui_extension/hermes-smart-router/console.html:2277`; enforced by a scan allowing exactly one bare `new Date()` within 200 chars of its definition (`tests/test_webui_extension.py:142-172`).
44. **One chain renderer serves both console surfaces** — `console.html:7811` `renderChainPlan(plan, opts)` takes the box, so Simular and Decisões cannot grow separate chain vocabularies.
45. **A control that cannot write is ABSENT, not disabled** — `console.html:4358-4376` detaches `#jsonApply` while lint errors exist; `saveButton()` exists because `getElementById` cannot find a detached node.
46. **The console draft is never `state.policy`** — `console.html:4082-4085, 9311-9313`. Mutating the snapshot makes `plan()`'s staleness guard read the operator's own edit as external drift and refuse every save.
47. **A recorded decision is priced at its own hour, then its `ts`, then no hour — never the browser's** — `console.html:7845, 7917`.
48. **`CAUSE_WORDS` must cover `VALID_CAUSES` exactly** — `console.html:3533-3555`, asserted from the Python side (`tests/test_webui_extension.py:2187-2247`).
49. **The JS suite list is a glob in both places that run it, and an empty glob fails** — `tests/test_js_suites_run.py:25-30`, `.github/workflows/ci.yml:103-107`. `node --test <missing path>` **exits 0**: the job was green while running 135 of 139 tests.
50. **`console.html` carries exactly one inline `<script>`** — asserted at `ci.yml:120` and three places in `tests/test_webui_extension.py`, because every harness extracts the first match.
51. **A pool slot is acquired before any spawn and released on every exit path; `at_capacity` returns before acquisition and must not release** — `__init__.py:1471, 1606-1607`.
52. **Pipes are always closed after reaping** — `__init__.py:317-323`. `Popen.wait()` reaps but leaves parent-side fds open, and `filterwarnings = error` (`pyproject.toml:52`) attributes the leak to whatever test runs next.
53. **Profile validation runs before the same-profile shortcut** — `__init__.py:1401-1404`; `default` is always valid (`:236-241`); when every resolver fails, `_profile_exists` returns False and refuses to spawn (`:253`).
54. **The router recursion guard is thread-LOCAL** — `__init__.py:594-602`. A process-global flag made one in-flight classifier suppress every concurrent route, surfacing as `profile is required`.
55. **File mode is carried across the atomic replace** — `scripts/collapse_profile_routing.py:302-324`. `mkstemp` creates at 0600 and `os.replace` keeps the temp file's mode: every rewritten config silently went 0664 → 0600.
56. **The WebUI model cache is deleted before the WebUI restart; restart order is Router → Memory → WebUI** — `scripts/update_hermes_stack.py:577-586`.
57. **The sidecar unit FILE name is never renamed in the same change as its extension ID** — `docs/operations/HERMES_EXTENSION_NAMING_MIGRATION.md:180-182`. The old enabled unit would hold port 8791 and the new one would fail with "address already in use".

---

## 7. Conventions a contributor must follow

**Code — structure.**
- Every plugin helper is module-level and `_`-prefixed so it is monkeypatchable by name. New logic must go through a named module-level seam or it becomes untestable. Seams keep their historical **call shape**: `_route_task` is called with 3 positional args without context and 4 with (`__init__.py:1384-1387`); `_record_breaker_outcome`'s provider is the 4th **positional** arg with a default so a `*args` stand-in keeps working.
- Impurity is injected, never reached for. `signals`/`rules`/`capabilities`/`breaker`/`classify`/`threshold` take the clock and the rng as parameters; only `adapter`, `service`, `cli` and `blocklist` read one, one place each.
- One question per helper, and one authority per question, fetched at call time to dodge import cycles: the cause labeller, the chain head, the composed prompt, the tier materialiser, the kanban role question. **Mirroring a table is the defect, not the fix** — and where a mirror is unavoidable it is asserted equal by a test (`service._empty_chain_plan` == `rules._empty_chain_plan`; served breaker weights == `breaker.FAILURE_WEIGHTS`).
- Closed sets are **read from the module that owns them** with a local frozenset fallback *and* an `isinstance((frozenset, set))` check — a registry exporting a bare string would turn `in` into a substring test and open the write gate (`router/rules.py:778-826`).
- Closed sets are extended by **adding a member**, never a new family. "OR is expressed by adding a row."
- Every intra-package import is relative-first / absolute-second, with `# pragma: no cover - flat layout used by the test harness` on the fallback. Optional sibling symbols get a nested double-try collapsing to `None` or identity, with a comment naming what degrades. Capability introspection is by `inspect.signature` resolved once at import, **never** by catching `TypeError` — a genuine `TypeError` inside a callee must not be masked as "does not take this argument".
- Every `hermes_cli` / `hermes_constants` import is lazy, inside a function, wrapped in `try/except` — the plugin must import and run outside a live Hermes process.
- Stdlib only in `blocklist`/`breaker`/`decision_log`/`durable_decision_log`/`one_sidecar`; PyYAML is the plugin's only runtime dependency.

**Code — behaviour.**
- **Failure taxonomy over exceptions.** The tool never raises for expected failures: it returns `json.dumps(...)` with `failure_kind` + `retryable` so an orchestrator can decide retry/fallback/give-up (`__init__.py:44-46`).
- **Read paths never raise; write paths raise only for a caller-shape error.** `liveness`/`capabilities` catch bare `Exception` with `# noqa: BLE001 - a read path must not raise` and return a documented degraded envelope with the reason attached.
- **Degrade, never guess, and LABEL the degrade in the output**: `plan_source`, `plan_time_aware`, `pricing: unavailable`, `strategy_degraded_reason`, `bypassed`, `time_agnostic`.
- Exception sets stay narrow where the failing code is ours; a broad `except Exception` requires a comment explaining that the callee is foreign or optional (`router/rules.py:762-775` is the canonical example).
- Every public function returns a **fixed-shape** dict on every branch, including degrades. Unknown facts are **omitted**, never `None` — the `Number(null) === 0` argument.
- Sentinels over magic values (`_REMOVE = object()`), epsilon over float equality (`_MULTIPLIER_EPSILON = 1e-9`), deep copies out of read paths so a consumer cannot mutate live parsed config or a cached registry answer.
- Diagnostics are strings, never exceptions, and are pre-shaped `model '<id>': …` / `tier '<Tn>': fallback[<i>]: …` so the consumer appends them verbatim. `capabilities` must **never** import `rules`.
- `# pragma: no cover` always carries a sentence naming which caller makes the branch unreachable and why the guard is kept anyway.

**Documentation-in-code.** Docstrings and comments are the design record and are load-bearing. The shape is: contract → the **measured** defect it closes, with numbers and dates → why every alternative is worse. Examples in the tree: *"Measured on 158 real cards: 135 (85%) died that way"*, *"of 47 recorded routing decisions, ZERO used the classifier"*, *"measured 1252 bytes on disk against the 600 it promised"*, *"read 2026-08-27"*, *"issue #14726"*. **A change that invalidates such a comment must replace the measurement, not delete it.** Time claims must be verified by sweeping all 168 hours of a week, never sampled — an earlier comment was wrong because it sampled 07:00Z and 15:00Z.

**Tests.**
- Test names are full English sentences stating the property (`test_a_failing_fallback_hop_binds_the_breaker_every_surface_reads`, `test_the_first_attempt_is_never_a_manually_banned_elo`); class names too (`TestTheVetoBindsWhatRuns`). Each docstring records the defect it guards.
- **Assert agreement between two producers**, not one side's value: `route()` vs `rules.explain` on the same turn; CLI `chain --prompt-text` vs `RouterService.explain`; `lint()` vs `lint_findings()`; `plan_head_of` vs `attempted_head_of`; `independent_rails` vs the console's `upstreamGroup` **parsed out of the HTML** rather than retyped. The verdict-by-verdict form passes again the moment the two sides drift a second time.
- Assert **non-vacuity** explicitly before asserting the property ("this ban would not touch the T2 chain", "this incident refused nothing, so it asserts nothing").
- Prefer properties over literals for anything an operator may edit ("no attempted elo is blind for a vision turn", not a fixed chain). Derive sets from the registry, never hand-list them.
- Time is a parameter or a monkeypatched single `_utc_now`; four module-level fixed clocks (`PEAK_MONDAY`/`PEAK_SATURDAY`/`OFF_PEAK`/`CHEAP_WINDOW`). Reading a real clock in a test is itself the defect the injected-clock design prevents.
- Load the plugin with `spec_from_file_location(<name>, REPO_ROOT / "__init__.py")` + `exec_module` (the module is literally `__init__.py`), bound as `dp`/`_dp` at module level.
- **Never call `monkeypatch.undo()`** — it is function-scoped and shared with the autouse conftest fixtures, so undoing reverts the spawn guard and the trace isolation for the rest of that test. Hold the real function in a module global instead (`tests/router/test_cli.py:493-501`).
- Simulate version skew with proxies over the **live** registry (`_RegistryWithout`/`_RegistryWith`), never with stubs — a stub only proves a stub was called. Provoke `OSError` with the real filesystem, not by patching `open`.
- Adding a plan key means adding it to all three plan shapes *plus* the exact-key-set assertion *plus* the literal degraded plan.
- JS: flat `test('sentence', …)` from `node:test`, no `describe`, grouped by `// ── section ──` banners carrying the rationale and the card id. Static scans strip comments first, because the console's comments quote the very strings the scans forbid.
- CI gate is **100% branch coverage** over the plugin + `router/` + `dashboard/plugin_api.py` on Python 3.11 **and** 3.12; `scripts/*` is omitted with the reason inline.

**Commits.** Conventional-Commit prefix + optional scope + a long **declarative Portuguese** sentence stating the resulting behaviour (median 75 chars, p90 101 — the 50-char rule is not in use). Type histogram over 222 commits: fix 85, feat 73, test 16, refactor 11, merge 11, docs 9 — and the tail matters, because it is where the convention is not followed: redesign 4, chore 3, polish 1, debug 1, plus one subject with no Conventional prefix at all ("traduzir a causa da decisão…"). Scopes: `console` 61, `router` 42, `router-ui` 11, `sidecar` 6; scopes may themselves be Portuguese (`feat(preços)`, `feat(compactação)`). A `t_<hex8>` Hermes kanban card id is the provenance anchor and appears in the subject (`(t_06277a38)`), in code comments (`card t_9388289e`, `spec t_c90c5336`), as the first words of a test docstring, and as the worktree branch name (`delegate-profile/t_<hex8>-<40-char slug>`). Merges usually use a descriptive `merge: <sentence>` subject — but not always: 2 of the 13 merge commits carry git's default (`e5aad02`, `2d1955b`), so match the convention rather than inferring it from every commit. **Merge, do not rebase** when a branch has fallen behind — rebase replays, and twice a hunk ended up on the "theirs" side of a console rewrite unnoticed (`81440ee`). Bodies are essays with SHOUTED section headings (`O QUE MUDA`, `VERIFICAÇÃO`), concrete numbers, a **mutation-testing tally** ("Onze mutações sobre as asserções novas, onze pegas"), and a closing test tally. A claim in a body must be reproducible; where the author could not verify something, the body records it as a defect instead. **No AI attribution, no `Co-Authored-By`** on new commits (the 7 existing ones credit the human).

**Docs.** Specs are `docs/superpowers/specs/YYYY-MM-DD-<slug>.md` and immutable in name — superseded names inside them are left alone as history. Both v2 specs open by declaring **the code the authority on disagreement**. Corrections are marked in place (*Corrected in phase 2*) and state the concrete defect. Named tests are cited as guarantees, by full node id. Runbooks are pt-BR with English identifiers, fixed-shape tables, fenced reproduction commands, and a rule that they must be corrected **in the same commit** as the code they describe.

**Vocabulary.** Identifiers, comments, docstrings and diagnostics: English. Operator-facing console strings, spec prose and commit subjects: pt-BR. Fixed terms: `profile` = the role axis (`then.profile`), `model` = the capability axis (the tier), `elo` = one (model, provider) rail entry, `rail`/`provider`, `hop`, `chain` = the planned attempt order, `tier` T1–T4, Table 1 = `rules`, Table 2 = `tiers`, `cause=` = the closed-set log label. The console's own glossary forbids `elo`/`hop`/`rail`/`shadowed`/`stale` as *rendered* words and allows `tier`/`breaker`/`fail-safe`/`blocklist`/`profile` only as a parenthesised gloss.

---

## 8. Gotchas and encoded history

### The plugin boundary

- **Config-key mismatch — RESOLVED.** `_watchdog_cfg` actually reads `plugins.entries.**hermes-smart-router**.watchdog` (`__init__.py:158`, verified), while the module docstring (`:49`), `_watchdog_cfg`'s own docstring (`:147`), `_cfg_value`'s (`:166`) and the operator-facing `at_capacity` error text (`:1477`) all say `delegate-profile`. The test pins the `hermes-smart-router` spelling (`tests/test_delegate_profile.py:688`). **An operator following the docs or the error message edits a key nothing reads.**
- **Stale schema doc.** The `timeout` property tells the model "Default: 300 (5 min)" (`__init__.py:1755`) while `_DEFAULT_TIMEOUT_S = 600` (`:122`), pinned at 600 by a test. Verified both.
- `plugin.yaml` declares only `provides_hooks: [post_tool_call]` while `register()` also subscribes `pre_kanban_dispatch` (`__init__.py:1785`), and the manifest still describes the plugin purely as subprocess-isolated delegation with no mention of the router or the kanban hook.
- **`_make_classify_fn` is invoked once, at `register()` time** (`__init__.py:1352`), so the whole `classifier:` block is frozen for the process, whereas `_route_task` re-reads `router.yaml` on every call. Enabling the router in YAML after load gives you routing with `classify_fn=None`. This contradicts `router.example.yaml:4`'s flat "editing router.yaml is live with no restart".
- **Two disagreeing classifier defaults — RESOLVED, both present.** `__init__.py:650` (the code that actually dispatches) defaults to `glm-5.2`; `router/classify.py:55` defaults to `glm-5.3-flash`. `glm-5.2` is one of the four ids the alias test forbids the *policy* from naming, because the vendor silently auto-routes it. Reachable only when `classifier.model` is absent from the file.
- **The classifier fails closed on a missing host trust grant.** Production evidence: of 47 recorded decisions, **zero** used the classifier and 16 ended in `fail_safe_strong` with `no_classifier`/`classifier_error`. A refused override must still look like a working classifier that errors, never like "no classifier configured" (`tests/test_classifier_trust.py:80`).
- **`classifier.chain` and `on_total_failure: heuristic` are declarative-only.** Only the flat `model`/`provider` pair dispatches; `on_total_failure` has no consumer anywhere in the repo. `router/fixtures/anchors.yaml` is loaded by nothing — the few-shot half of `build_prompt` is unwired in production.
- **An explicitly requested `model` is paired with the ROUTER's provider** (`__init__.py:1390-1396, 1587`): `{model: 'operator-choice'}` plus a decision declaring `zai` spawns `-m operator-choice --provider zai`.
- **Naming a `profile` explicitly disables the whole failover chain** (`__init__.py:1373, 1587`). Cross-rail fallback exists only on the `auto`/omitted path.
- `_attempt`'s docstring claims "never raises" but only `_spawn` is wrapped; an exception out of `_run_watched` escapes the handler as an exception rather than a JSON envelope. `binary_not_found`/`spawn_error` envelopes are not merged with `base`, so they lack `subagent_id`/`profile`/`model`/`elapsed_s`, and no breaker outcome is recorded for them.
- **`_reported_agent_failure` was tightened three times, each widening documented** (`__init__.py:524-571`): pinning `"after 3 retries:"` made `"after 1 retry"`/`"after 5 retries"` read as SUCCESS — returning an error banner as the agent's answer *and* suppressing cross-rail fallback; terminal failures that never retry (401/403 abort, TLS failure, refused connection, DNS failure) needed `_TERMINAL_FAILURE_RE`; and a generic "API failed" line counts only when it also names an exhaustion cause, so prose *about* a 429 is not mistaken for one.
- `preexec_fn=os.setsid` is deliberately not used (unsafe with threads, and this handler runs inside a threaded agent). `_SIGKILL_NUM = 9` is a numeric literal so the module stays importable on Windows.
- `_KanbanShadowLog.record` deliberately calls `DecisionLog.record(self, …)` — the **grandparent** — so the unstamped entry is never persisted; it stamps `shadow`/`task_id`/`run_id` onto `self._entries[-1]` and calls `_persist` once. Any change to the parent's persist ordering breaks the stamp.
- `HERMES_DELEGATE_PROFILE_DISABLE=1` is written into the child env as anti-recursion and, since 2026-09-02, is READ BACK in `register()`: a process with it set is not offered the tool. It was a comment rather than a control for as long as the README, PRODUCT.md and this document asserted the guarantee as fact. Only the TOOL is withheld — both hooks stay registered, because the child is still an agent that may dispatch kanban cards and gating `register_hook` would silently turn off shadow routing in every delegated process.
- Indentation anomaly at `__init__.py:1447`: a comment at column 0 inside the nested handler.

### The pure core

- **`time_cap`/`time_policy` key typos were a money defect, not a tidiness one.** `{avoid_peek: […]}` / `{max_multipler: 1.5}` read as working cost controls, passed the fail-closed gate, and did nothing — the operator found out from an invoice. The key sets are closed now and an unknown key is a hard error naming the key (`router/rules.py:139-147, 1202-1250`).
- **A `billing_mode` typo on a fallback hop is `cheapest_now`'s outer sort key.** `meterd` drops the elo into the unknown bucket and sorts it last. Measured: gpt-5.5 (subscription, $30/1M out) ordered ahead of glm-4.7-flashx (metered, $0.40/1M out) while `lint` returned `[]`.
- **`[16.5, 24]` used to lint clean and run as `[16, 24)`** — a window starting half an hour before the one written. **`multiplier: .nan`** used to lint clean (`nan <= 0` is False) and made the elo permanently un-capped, un-peak-priced and unorderable with no diagnostic. **`context_window: .inf`** linted clean and took the whole decision down with an `OverflowError` that `plan_chain`'s defensive except does not catch. All three now refused by the same helpers the running path uses.
- The malformed-hours message must name the value the operator **wrote**; it used to hardcode "16.5 is not an hour boundary", sending whoever typed `[-1, 6]` hunting for a fractional hour nowhere in their file.
- `_as_number` rejects bools because `True` is an `int`: `max_multiplier: true` would silently become a cap of 1.0. `weekdays: []` is treated as **malformed**, not "every day". A weekday gate that is present but malformed makes the window inert.
- **`eligible = filtered.get('eligible') or chain`** (`router/rules.py:323`): an empty eligible list silently falls back to the full chain, doubling the filter's own bypass — but `bypassed` still comes from the filter, so this second net is invisible in the trace.
- **`cap_exempt` never reaches the plan — CONFIRMED and CONTRADICTS THE SPEC.** `apply_time_cap` returns it (`router/capabilities.py:1788`) but `plan_chain` drops it and it is absent from `CHAIN_PLAN_KEYS`; the exact-key-set test locks the omission in. The addendum demands the opposite (`docs/…time-windowed-routing-addendum.md:356-364`) then hedges one sentence later. Consequence: an operator cannot tell "not over the ceiling" from "over it and exempt because it is plan-billed" — T1's `max_multiplier: 1.5` removes nothing at any of 168 hours on today's roster and reports only a raw `multipliers` entry.
- **`_matches_clauses` any-op vs `_all_clauses_match` all-op.** `_matching_clauses` records a chip when *any* op on a field matches while the matcher requires *all*; safe only because `explain()` asks exclusively about the rule that already matched.
- `match()`/`explain()` do `rule['id']` and raise `KeyError` on an id-less row — lint refuses it first, so the engine assumes the gate ran. No test pins that path.
- Runtime is permissive where lint is strict, on purpose: the registry coerces `min_context: "200000"` so a stale file still routes while lint refuses it; `_resolve_tiers` **normalises** a non-string strategy and a non-bool `pin_primary` rather than validating.
- Window validation is asymmetric by design: an operator's overlapping YAML windows are hard errors, the same defect inside `MODEL_CAPABILITIES` is advisory — the operator cannot fix the registry from YAML.
- `_ORDER_CHAIN_ACCEPTS_WHEN` is probed **once at import**; a test that monkeypatches `capabilities.order_chain` must patch the flag too.
- **STALE DOCSTRING — RESOLVED.** `router/rules.py:2075-2077` says `decision_log.record()` coerces an unknown cause to `fail_safe_strong`; the code records `unknown_cause` (`router/decision_log.py:441-446`, verified) with a comment rejecting the old behaviour by name. The same stale sentence also sits in RUNTIME code at `router/adapter.py:1186`, plus one test file (`tests/router/test_rules.py:4363`). The argument it supports (the cause set is closed) still holds.
- `_REVIEW_KEYWORDS` (`router/signals.py:37`) is **dead code** — verified: `_keyword_hits` (`:406`) uses its own inline 5-word set without `evaluate`, so a rule keyed `keywords: {contains: evaluate}` can never fire.
- `keywords` is built by iterating a Python `set`, so its order varies across processes; harmless for routing, but a persisted trace compared byte-wise across processes can differ.
- `_infer_file_count` reads the **raw** turn with IGNORECASE while every other detector reads `lower`; `"3-5 files"` yields the upper bound 5, and each inferred file adds 4000 tokens. `_detect_stacktrace` markers include a bare `"error:"`, so ordinary prose sets `has_stacktrace`.
- `_apply_session_floor` **keeps a stale provider** (`router/adapter.py:1170-1171`, no `else`-pop) unlike the two other paths that pop it.
- The final fall-through fail-safe loses both the role and the rule id (`router/adapter.py:463-466`), while the two Stage-1 fail-safes pass both.
- The cache-hit path does not ratchet the pin and records `cause=classifier`, so a cache hit is indistinguishable from a live call except in `steps[]`.

### Ban state and the trace

- **`is_blocked` is NOT a pure read, and the adapter calls it many times per turn.** The first query for an expired-OPEN key transitions to HALF_OPEN, **consumes the probe slot**, and answers False; later queries in the same turn answer True (`router/breaker.py:120-136`, verified). Reproduced on shipped code: the decision kept `output.model == glm-5.3` while `chain_plan.blocked` listed `glm-5.3@zai` and `attempted_model` became `mimo@xiaomi` — the probe slot was burned by a *planning* query while a different elo was dispatched. HALF_OPEN has no expiry, so the rail then stays blocked indefinitely with `cooldown_remaining_s: 0.0`, which the CLI prints as "expiring now" forever. The adapter tests deliberately pin cooldowns far in the future so this "cannot fire mid-test and make this flaky" — so the interaction is **untested**.
- `breaker_status()` mutates in memory, does **not** persist and does **not** take the lock: an entry whose cooldown just expired disappears from that one read, so `liveness()` reports it `alive` while disk still says OPEN. The sidecar's "cannot mutate breaker state" claim holds only on disk.
- `blocked_entries` never prunes, so `failure_count` includes events already outside `window_seconds`; events are cleared only by `_reset`.
- `_match`'s docstring says "block regardless of provider" but a provider-scoped ban does **not** block the model on another rail — the fail-closed half is that an *empty queried* provider matches a provider-scoped ban, which is exactly why the adapter refuses to widen lookups.
- `breaker_cooldown` is in `VALID_CAUSES` but **no code path produces it** (the union hides ban-vs-cooldown, so the veto always reports `blocklist_veto`); the console still ships a label for it. `blocklist_substituted` is **not** in the set and would be coerced, so the substitution travels in `output["cause"]` while the log keeps the pipeline's cause. `profile_ignored` is retired (produced until 2026-08-26, when 135 of 158 measured decisions were being dropped whole) but kept so old trace files remain nameable.
- **The substitution is capability-blind and the code says so** (`router/adapter.py:765-771`): `blocklist.fallback_chain` is a flat model list, so with glm-5.3 banned a vision turn can be handed a blind model while the plan still held a sighted one. Reported rather than smuggled in.
- Rotation history: the size test used to be `st_size >= cap`, letting every file overshoot by a whole entry — **measured 1252 bytes on disk against an advertised 600** on a 200-byte cap. `size and …` is load-bearing: an empty file must never rotate, since `_rotate` ends in `os.replace` whose OSError is swallowed and would drop the first entry on a fresh install.
- `create_card` is invoked *before* `_write_state` with no try/except, so a Kanban failure aborts the price-watch run and loses every other provider's state update from that run.
- The state directory was renamed `delegate-profile/state` → `hermes-smart-router/state` in `40f533d` with on-disk history migrated by hand and **no read fallback**, deliberately.
- `_BLOCKER_LOCKS`' comment claims the registry is "kept weakref-able"; the implementation is a plain `str → Lock` dict that is never pruned. Neither `breaker-state.json` nor `attempts.jsonl` has any size or age bound in this repo.

### Service, sidecar, frontends

- **Asymmetric validation.** `_load()` runs only `rules.lint`, while the write gate is `rules.lint + _validate_fail_safe + _validate_compaction`. A file already on disk with a malformed `fail_safe`/`compaction` reads `valid: True` on `/status` and `/lint` and runs through `explain()`, but the same content is refused by `plan`/`apply`. Not commented anywhere.
- **`explain()` never vets the previewed chain against the blocklist — CORRECTED.** An earlier reading of this called the hardcoded `blocked_model=False` (`router/service.py:2030`) the defect. It is not: `blocked_model` is an INJECTED `when:` field (`router/rules.py:169`) carrying whether the CALLER'S REQUESTED model is banned, and `RouterService.explain(task, at, prompt_text)` takes no requested model — so `False` is the only correct value, and a rule keyed on `blocked_model` is legitimately inert in a dry-run that requested nothing. What IS true is narrower and still real: `explain()` constructs no `Blocklist` at all, so the previewed `chain_plan` can list a banned or breaker-open elo that production's `_veto_blocked` would remove or substitute. **The preview is not the vetted plan.** Note the CLI does not share this gap — `router explain --model X` and `router chain --model X` both build a `Blocklist` and pass the real boolean, so the two surfaces disagree about their own contract.
- A **removal cannot round-trip** through `plan()['policy']`: `policy` is the already-merged result and a merge cannot see absence, so replaying it restores the knob and is answered `no_op: True`. Removals must be sent as the change.
- `_policy_references` raises `TypeError` on a malformed scalar `tier.fallback` (`fallback: 5` → iterating `5`), which is why `_policy_provider_index` wraps it while `liveness()` degrades wholesale. The string branch of its fallback-chain loop is **inert** (it feeds only a `continue`) while the comment above it describes a mapping the code never performs.
- `_max_prompt_chars` (1 MiB ≈ 291k tokens) **cannot reach** the shipped `huge-context-read` rule, which fires above 400k tokens. Asserted as a fact rather than fixed, because 1 MiB is ~0.4 s of CPU per request on an unbounded-body HTTP path; the refusal message must keep pointing at `router chain --prompt-text`.
- `_next_window_change` accepts a bare int as the pre-weekday registry spelling and reports the missing fields as `None` rather than guessing "today" — guessing was wrong by up to **45 hours** across a weekend.
- `POST /apply` is overloaded: `body.action == "compaction"` selects the RESTART-class path, and otherwise it is the hash-checked policy commit. There was also a `/apply/confirm` alias whose whole body was `return self._commit_policy(body)`; it is DELETED on this branch (§10) — it had no client anywhere in the repo and its comment described a console button `console.html:2065` records as removed ("two buttons, one effect" was unexplainable on screen). Re-posting the same plan to `/apply` gives the clean 409 the alias was said to provide. The compaction candidate temp file is never unlinked on success (the launcher owns it), and the 30 s runner timeout is calibrated to a launcher that returns immediately.
- `resolve_core_config_path` must not append `profiles/<name>` when `HERMES_HOME` is already profile-scoped — doing so produced `…/profiles/rodrigo/profiles/rodrigo/config.yaml` and every compaction died with ENOENT.
- `PrivateTmp=true` in the unit + `mkstemp` for the candidate + a launcher that backgrounds via `systemd-run` is an unresolved namespace question (§9).
- `fetchAll` asks `/routes?limit=200` deliberately: at the sidecar default of 50 against 71 recorded, every hit count was understated ~27% and a rule that fired only in the dropped rows rendered "never fired".
- `eloWindows(caps)` has **no rail fallback** on purpose: falling back to `RAIL_WINDOWS[provider]` made flat-priced glm-4.6 render "2× peak · $1.20 in / $4.40 out" while `price_multiplier` returned 1.0. `priceMultiplier` returns on **first** match because `capabilities._multiplier_at` does; it used to accumulate, so for an overlapping pair the router priced hour 9 at 2.0 and the console displayed 3.0.
- Three-valued reads are load-bearing and collapsing any is a documented past bug: `pin_primary` (null ⇒ no hop drawn first), `pricePublished` (null ⇒ say nothing), `weekdaySet` (false ⇒ **drop** the window), `reportedMultiplier` (`Number(null)` is 0 → "0× cheap window"), `weekdayOf` (NaN, because 0 is Monday).
- `droppedElos` must read the two bypass flags **before** placing `rejected`/`capped`: both bypasses retain diagnostics and restore the chain, so rendering them as "Dropped" printed the same elo twice in one viewport.
- `watchStatus()` deliberately does **not** use `call()`: a failed `call()` sets `state.unreachable`, and running every 60 s it would flip the header to "não consegui falar com o roteador" once a minute during a deploy's restart window.
- **The rule-level `status:` field is INERT.** `router/rules.py` never reads it — `match()` honours only `rule.get("enabled") is False` (`:226`), and `lint` does not validate it. It is nonetheless on all 8 shipped rows (`router.example.yaml:228,245,253,295,303,323,334,343`) and is the second key `RouterService.policy()` projects (`router/service.py:951`). So the read model served a DEAD field while dropping the LIVE one — the two halves of one defect, not two findings. (Both fixed on the branch; see §10.)
- **`policy()` drops `enabled` from the rule projection — VERIFIED** (`router/service.py:948-957`), while the console tests `r.enabled === false` for the off-state and shadow suppression. A disabled row round-trips through the write path but reads back as enabled, undercutting `router.example.yaml:222-224`'s promise that "the console's inspector offers the switch on each rule row".
- `--soft` is used three times in `console.html` (`:1614, 1624, 1673`) and **defined nowhere** — those borders/backgrounds resolve to nothing. Verified.
- The `badge.hidden` comment is at `webui_extension/hermes-smart-router/console.html:3597`, not in `router-nav.js` (which is 269 lines). And `router-nav.js:244`'s `!count.hasAttribute('hidden')` is always **TRUE**, not never — the `.count` badge is created without a `hidden` attribute at `console.html:3582-3585`, so the guard is VACUOUS rather than dead. Counts do propagate to the sidebar; the guard just never gates anything.
- `dashboard/dist/index.js` addresses `/api/plugins/delegate-profile` and registers `"delegate-profile"` while `dashboard/manifest.json` says `hermes-smart-router` — **verified**. It sends only `task` to `/explain` (no `prompt_text`, no `at`), which is the exact defect `dashboard/plugin_api.py:45-57` documents; and its `GET /log` serves the plugin's own preview `DecisionLog`, i.e. simulations rendered under "📜 Decision Log".
- `Annotated[…] = None/50` is used instead of `= Query(…)` because an in-process caller otherwise receives fastapi's `Query` sentinel — `DecisionLog.tail` died on `-Query(...)`.
- The relative-then-absolute import in `plugin_api.py:95-106` is pragma'd because resolving the absolute name first "worked" but imported a **second independent copy** of the router package, so the read path saw different module state than the write path.

### Vendors, policy and prices

- **Plan auto-routing is the defect the capability registry's docstring exists to prevent.** On a z.ai Coding Plan key, `glm-4.7` and `glm-5-turbo` are silently served by `glm-5.3-flash`, and `glm-5.2`/`glm-5.1` by `glm-5.3`. The request succeeds, the plan bills the substitute, and every trace, log and console row names the id nobody ran. `glm-4.7` sat in **four** places in the shipped policy while glm-5.3-flash answered (`router/capabilities.py:65-87`). This is recorded in `notes` and enforced only by `tests/test_shipped_policy_names_real_rails.py` — nothing in the routing code refuses it.
- **The price watcher missed that change.** The peak-hours clause did not change one character, so the watcher reported "confirmed" while four names in policy became aliases. That is why a second adapter now anchors the substitution sentence `"will automatically be routed to"` with a distinct key (`router/price_watch_runner.py:29-42`) — two adapters on one page need distinct keys or one overwrites the other's literal.
- **Two of five price-watch anchors are replacements for anchors that silently watched the wrong thing** (`c2dc9b4`): `Token Plan` matched an `og:title` meta tag, and `按量付费` never matched at all because the page says `按量计费`. Hence `_is_page_metadata`, and hence the anchors are pinned as literals so the pin breaks loudly instead of silently watching a title again.
- **deepseek's weekday gate was added after a silent vendor edit** (absent from the changelog; Wayback snapshots bracket it 21/08 vs 24/08). Without it the router priced 14 h/week at 2.0× that the vendor bills at 1.0× — which never overbills, it routes *away* from deepseek toward rivals that are not actually cheaper, and the money leaves through someone else's invoice.
- **The 168-hour arithmetic in `router.example.yaml` and `CONDITIONAL_ROUTING_DEPLOY.md` is stale by exactly that gate.** The YAML claims deepseek peaks "EVERY day" and that T3/T4 split 119h/29h/20h and T2's tail flips 49/168 (29.17%); the tests assert **15/20/133** and **35** (`tests/router/test_capabilities.py:3071, 3128`). `router/capabilities.py:625` still carries the comment "Peak is EVERY day, so no `weekdays` gate" three lines above the gate itself, and `:98-99` repeats it. An operator tuning time knobs from the YAML prose is reading pre-2026-08-22 figures.
- xiaomi's 0.8× night discount is deliberately **not** modelled: it is a prepaid Token Plan credit coefficient and this install bills pay-as-you-go. Carrying it told the router metered cost fell 20% for 8 h/day — real cost was 1.25× its own estimate there. If the install ever buys the plan it needs `billing_mode: plan` **and** the window, both halves. Consequently no shipped entry carries a discount window, which is why the mechanism is tested through a synthetic declared rail.
- glm-5.3-flash's registry price is the **list** price (0.15/0.50), not the 50%-off launch promo expiring 2026-09-09 — a chain ordered on a discount with an expiry silently doubles in cost that morning. Its 1M window is opt-in through a `[1m]` suffix nothing in this repo emits.
- Batch/Flex endpoints (50% off) are deliberately not windows — they are separate endpoints, and a window would claim a discount the router gets without changing how it calls. `us.anthropic.claude-opus-5` is deliberately unregistered: same weights, different rail, so registering it would assert a price that rail may not charge.
- `06:00-10:00 UTC` is peak on both primary rails at once, which is why `apply_time_policy` must demote two providers simultaneously without emptying a chain.
- **v1's motivating case is no longer enforced.** The whole "supporting" half of the 2026-07-21 spec was the `gpt-5.6-sol / openai-codex` accept-but-never-stream stall, encoded as a `manual_ban` plus a `block-codex-stall` deny row at the top of Table 1. The shipped policy has `manual_ban: []` and no deny row; the model is still in the registry, named by no tier. Conversely, v1's explicit YAGNI defer of the auto-breaker was **overturned** — it ships enabled, and the missing stall signal now exists as weighted failure kinds.

### Deployment and history

- **`node --test <missing path>` exits 0.** The webui CI job named three paths, two of which had not existed since that behaviour moved into `console.html`, while `test_router_nav_mount.js` — which exists and was **4/4 red** — was named nowhere. The job was green while running 135 of 139 tests. Both jobs now glob and pin node 22 explicitly.
- The coverage job needs node *and* a python interpreter (the console tests price a window with `router/capabilities.py` in a subprocess); without node the JS rot-detector skips inside the very job that owns the gate.
- The e2e sidecar tests document two measured harness traps: production `ThreadingHTTPServer` uses daemon threads so `server_close()` does not join handlers, and a client closing before draining the ~660 KB `/console` body makes the handler raise `ConnectionResetError` so `_serve` never returns — which flaked coverage 99.99% of the time.
- `_is_operator_unit_dir` compares by path **shape**, not against `Path.home()`, because during the 2026-08-26 incident the installer ran in an agent shell whose HOME was remapped and the home comparison answered False for the directory that mattered. `smoke-live-sidecar.sh` derives the production home from the live plugin path for the same reason (in a profile-scoped shell `$HERMES_HOME` points at the profile, TOK is empty, and every route 401s while the service reports "active").
- `_default_python()` is `sys.executable` because the template used to hardcode a venv path and systemd failed at ExecStart with no hint on installs that keep the venv under `HERMES_HOME`.
- `PYTHONDONTWRITEBYTECODE=1` in the unit exists because the retired `.path` unit watched `router/__pycache__` and a pyc write on boot caused a restart loop. The `.path`/inotify approach does not work on this WSL box (systemd 255 armed the watch but never opened an inotify fd, and a directory watch misses in-place edits) — hence the poller.
- `git stash pop` needs a reflog selector, so the updater returns the literal `stash@{0}`; safe only because the process-wide flock guarantees no other stash intervenes. `_restore_component` runs `reset --hard` **before** `checkout -f` because a failed merge leaves unmerged index entries that block the checkout. `update_component` deliberately does **not** `merge --abort` — the caller owns rollback from a byte-for-byte snapshot and Git's conflict diagnostics are kept.
- `REQUIRED_EXTENSION_IDS` is a bug fixed in place: the gate still named `hermes-panel` after phase A renamed it, so a healthy manifest failed validation.
- **The naming doc records evidence that overturned its own reasoning, then the operator overturning the evidence.** `Profile Router` was rejected by measurement (over 252 real decisions `profile` was `coder` in 230 while 7 distinct models were chosen; the console reads `model` 95 times vs `profile` 3), which argued for `capability-router`; Fase E then records the operator choosing `hermes-smart-router` and that "a convenção do pack e a precisão descritiva de 'capability' foram pesadas e rejeitadas; a escolha é do operador."
- Old-name residue after Fase E: `HERMES_KANBAN_BOARD` still defaults to `"capability-router"` (verified, pinned by a test, unmentioned in the naming doc); `scripts/install_hermes_one_router.py` and its test keep `hermes_one_router` filenames; both sidecar unit filenames are kept deliberately; `plugin.yaml:1` `name: delegate-profile` and the tool name are deliberate keeps. `PRODUCT.md:66` still claims the package is `hermes-delegate-profile` while `:17` and `pyproject.toml:6` say `hermes-smart-router` — the file disagrees with itself.
- README staleness (verified): badge says `tests-428 passed` against 1933 collected; `timeout` documented as 300; the T4 example says `claude-opus` while T4 is `gpt-5.5@openai-codex`; `cd hermes-delegate-profile` predates the rename; the layout tree omits `capabilities.py`, `durable_decision_log.py`, `price_watch*.py`, `threshold.py`, `fixtures/` and lists 4 CLI subcommands when there are 5; the failure envelope text and field list do not match `__init__.py:1545-1575`; and `fail_safe` is called "a trusted strong model" when it is the cheapest plan rail.
- `HERMES_CUSTOMIZATION_MANIFEST.md` is a dated snapshot (2026-07-27): it records "424 passed, 2 skipped" and a 10-modified/51-untracked/47-behind integration debt; the tree is clean today. Only one row self-annotates its own staleness. It also inventories one of the three systemd units and none of `scripts/sidecar_stale_check.py` or `scripts/cron/`.

---

## 9. Where things are unclear or risky

**Currently broken — highest priority.**

1. **CI is red on both gates, right now.** Verified on Python 3.11: `1 failed, 1929 passed, 3 skipped` out of 1933 collected; coverage `TOTAL 99.9%` with `router/adapter.py` at 99.4% (1 statement, 2 partial branches). The failure is `tests/router/test_adapter.py::TestTheVetoBindsWhatRuns::test_the_first_attempt_is_never_a_manually_banned_elo[glm-5.3]`, which errors with `blocklist_veto` / "the fallback chain offers no reachable replacement". **Root cause, verified:** commit `bdb92f6` ("z.ai sempre glm-5.3-flash") changed T3/T4's third hop from `glm-5.3` to `glm-5.3-flash` and therefore removed `glm-5.3` from `blocklist.fallback_chain` (the tier union). The test's `_vision_gap_config` (`tests/router/test_adapter.py:1357`) still *injects* `glm-5.3` as T2's primary to construct the declared-vs-attempted gap, so banning it leaves `_reachable_replacement` with no position to walk from and the turn denies. This is fixture staleness, not a routing regression — but the missed adapter branch is the substitution path, so the gate cannot go green until it is fixed. **Not skipped, not xfailed, not noted in any doc.**
2. **The suite cannot even collect on Python 3.10.** `scripts/update_hermes_stack.py:51` does `from datetime import UTC`, so `tests/test_update_hermes_stack.py` raises at import and pytest **aborts the whole run** rather than failing one file. `requires-python = ">=3.11"` is real, and this repo's default interpreter on this machine is 3.10.

**Behavioural questions the code cannot answer.**

3. **The probe-slot / planning-query interaction.** Is HALF_OPEN intended to have no expiry? Nothing re-arms the slot except a recorded success or failure, and `_veto_blocked`/`_vet_plan_chain`/`RouterService.liveness`/`router.cli blocklist` all *query* `is_blocked` for hops that may never be dispatched. A slot burned by a planning query for an undispatched hop leaves that rail blocked indefinitely. Commit `c3962a5` frames the fix purely as anti-stampede and never discusses re-arming. The adapter tests pin cooldowns far in the future explicitly so this cannot fire, so the whole interaction is untested. **The highest-risk unresolved behaviour in the repo.**
4. **The two disagreeing classifier defaults** (`glm-5.2` in the dispatching code, `glm-5.3-flash` in `classify.py`) — and `glm-5.2` is an id the vendor silently substitutes. Only reachable when `classifier.model` is absent, but there is no test asserting they agree.
5. **Which plugin id does the host key per-plugin config on?** `delegate-profile` (`plugin.yaml:1`, the docstrings, the `at_capacity` operator message, the llm-trust grant in `tests/test_classifier_trust.py`) or `hermes-smart-router` (the install directory, `pyproject.toml`, and the only code path plus its test)? Nothing in this repo resolves it, and the watchdog config is silently inert if the docs are right.
6. **Does the host validate `provides_hooks` against what `register()` subscribes?** If it does, the undeclared `pre_kanban_dispatch` could be rejected or unlogged.
7. ~~**Is `HERMES_DELEGATE_PROFILE_DISABLE` enforced anywhere?**~~ **ANSWERED, and it is now.** It was vacuous; `register()` reads it and withholds the tool. The decision behind the shape: withholding the tool is sufficient (a model cannot call a tool it was never offered) and `delegate_task` — in-process, no new session — stays available for a child that genuinely needs to sub-delegate. The hazard this closes is concrete: a nested `_spawn` creates its own session and pgid, so a depth-2 tree escapes both the outer `killpg` and the atexit registry, which is the orphaned-grandchildren failure mode the plugin exists to prevent.
8. **Where is `HERMES_ROUTE_ATTEMPTS_FILE` consumed?** `attempts_path()` reads `attempts.jsonl` from `routes_path()`, not from that variable, so the env publish (and its leak into later children via `os.environ.copy()`) only matters to a core-side writer outside this checkout.
9. **Should the `/explain` preview be vetted against the live blocklist?** Not the `blocked_model=False` question (that one is answered — see §8), but the chain: `explain()` builds no `Blocklist`, so `/explain` and the console's Simular tab can show an attempt order production would refuse. Fixing it means either reproducing `_veto_blocked`'s substitute/widen behaviour in the read model — which would be a second copy of the policy, and PRODUCT.md forbids that — or ADDITIVELY reporting which previewed hops are currently refused and letting the console label them. The second is cheap and honest; it is a product decision, not an implementation one, so it is written down rather than chosen here. If it is taken, the query must use `Blocklist.would_block`, never `is_blocked`: a preview consumes no capacity and must consume no probe slot.
10. **Is the `_load()` vs `_lint_merged` asymmetry deliberate?** A file with a malformed `fail_safe`/`compaction` reads `valid: True` and runs, but is unwritable.
11. **Is `policy()` dropping rule `enabled` intentional?** No test asserts either behaviour, and the console depends on the field.
12. **Is `_apply_session_floor`'s stale-provider asymmetry intentional?** It sets provider only when the pinned tier declares one, with no `else`-pop, unlike the two other paths.
13. **Is `breaker_cooldown` reserved or dead vocabulary?** It is in the closed cause set and has a console label, but no producer.
14. **`_run_watched` uses `readline()` on a `text=True, bufsize=1` pipe.** Whether a child emitting a very long line without a newline (a large single-shot answer) can defer the heartbeat past the idle threshold is untested.
15. **Teardown ownership is unstated.** `_close_pipes` runs inside `_kill_tree` and again at the end of `_run_watched`, and `_kill_tree` runs a third time in `_attempt`'s `finally`. All individually idempotent; no single owner is named. Similarly `_Pool.acquire(wait)` with `wait <= 0` blocks **forever**, which the handler never triggers but which is unguarded for other callers.
16. **Is the `.bak` intended to be exactly one level deep?** Two applies cannot both be undone; no depth policy is stated.
17. **What prunes `breaker-state.json` and `attempts.jsonl`?** Neither has any bound in this repo, and `attempts.jsonl`'s writer and schema evolution live in Hermes core (`route-attempts/2` behaviour is unspecified on this side).
18. **The advertised trace disk ceiling is knowingly violated** by an entry larger than the cap. Whether anything bounds entry size beyond `bound_chain_plan`'s `rejected` truncation is unstated.

**Spec/doc divergence that will mislead a reader.**

19. **The `min_context` → `context_window` alias the v2 spec promises does not exist.** `min_context` is absent from `_REGISTRY_FIELDS` *and* from `CAPABILITY_ASSERTION_KEYS`, so a chain hop written exactly as the spec's own example (`{model: deepseek-v4-pro, provider: deepseek, min_context: 128000}`) plans with that hop in `unknown` and its declared bound ignored. Whether it was dropped when `min_context` was redefined as an input figure, or never written, is unrecorded.
20. **The `derive_requirements` boolean floor — RESOLVED, at a different layer than the spec locates it.** `router/capabilities.py:1854` is a bare `result[key] = value` (verified: `derive_requirements({'needs_vision': True}, {'vision': False})` → `{'vision': False}`) and its own docstring still says the floor "wins on conflict". The defect *is* closed, one layer up, by `rules._tier_floor_of` (`router/rules.py:1527-1550`), which drops falsy booleans before assembling the floor — so the production path (`plan_chain`) is safe and a direct `capabilities` caller is not. Retrospective lesson 3 says the fix should have been positive enumeration where the defect lives; `_declared_capabilities` is still exclusion-based (`router/rules.py:942-948`), guarded one layer down. This is why a hop-declared `min_context` is harvested at the rules layer and silently dropped at the registry layer.
21. **Two typo'd hard-error strings the v2 spec lists do not exist** (`fallback[{i}] declares unknown capability key`, `'min_context' must be a positive integer` on a hop) — verified consequence: a hop carrying `vissssion: True` and `min_context: 5` lints clean. Two of the spec's eight advisory-warning rows also do not exist.
22. **Both v2 documents claim `unsatisfiable` reaches no human-facing surface. It does** — `console.html:7708` and `router/cli.py:926`. The deploy doc records the correction; the specs do not.
23. `docs/` is three specs behind the code: `shadow`, `compaction`, the injected `assignee` feature, the top-level `price_windows` overlay, `price_watch*`, `durable_decision_log`, `threshold` and the 609-line console `DESIGN.md` are outside every spec.
24. `HERMES_EXTENSION_NAMING_MIGRATION.md:3` still says "aguardando reconciliação do runtime `delegate-profile` antes da Fase A" while phases A–E are all marked concluded.

**Operational unknowns.**

25. **Nothing enables `hermes-router-sidecar-stale-check.timer`** — the installer writes the files, `update_hermes_stack.py` restarts only the sidecar, no doc runs `enable --now`. Whether the poller is armed on the box is unverifiable here. And `scripts/sidecar_stale_check.py` hardcodes `/home/rodrigo/.hermes/plugins/hermes-smart-router` and derives the token only from `HERMES_HOME`, while its unit template has **no `@WEBUI_STATE_DIR@` placeholder** — if the real token lives elsewhere, `process_started_at()` returns None and the poller no-ops **forever, silently** (it always exits 0) while the sidecar serves stale code.
26. **`router-nav.js:56-57` claims the sidecar sends `X-Frame-Options: DENY` and `frame-ancestors 'none'`.** `_write` (`router/one_sidecar.py:694-718`) sends only Content-Type, Vary, optional Content-Encoding, Content-Length and Cache-Control. Either the WebUI proxy adds them or the comment — and the srcdoc justification resting on it — is stale.
27. **`PrivateTmp=true` + `mkstemp` compaction candidate + a launcher that backgrounds via `systemd-run`.** Whether a detached unit can read a path inside the sidecar's private `/tmp` namespace cannot be checked without the out-of-repo `~/bin/hermes-safe-restart.sh`.
28. **CSRF enforcement lives entirely in the WebUI proxy** — the sidecar never inspects `X-Hermes-CSRF-Token`. Its exact rules (which methods, which paths) are outside this repo, and the whole write path depends on them.
29. **Has `scripts/collapse_profile_routing.py` ever been applied?** It is a one-shot with no caller, no marker file and no idempotency record beyond the profiles themselves; the deploy doc presents it as unexecuted. The runbook it encodes records having corrupted all 16 configs once.
30. `scripts/install_hermes_stack_updater.py:150`'s default `--python` (`/usr/local/lib/hermes-agent/venv/bin/python3`) disagrees with the deploy doc's recorded live interpreter (`/home/rodrigo/Workspace/hermes-agent/venv/bin/python`). The manifest names preserved local branches per component but the updater **never asserts them** — it merges into whatever branch is checked out.
31. `_extract_archive`'s traversal guard is effective for a real subdirectory but every resolved path is trivially "under" `/`, and the support archive extracts at `/`. Safe only because members come from `_archive_paths`.
32. **No lint, format, or type-check job exists** — no ruff/flake8/mypy/eslint config anywhere. `pytest-xdist` is a declared dev dependency but nothing uses `-n`, and given the autouse per-test canary and shared `tmp_path` isolation, parallel safety is untested. The Codecov upload is `if: false` with no explanation.
33. `scripts/*` is omitted from the 100% gate as "one-shot deployment helpers" even though four test files (63 tests) exercise them; whether they are near-100% by accident or intentionally un-gated is unstated. `dashboard/dist/index.js` has **no test and no CI step at all**, and whether it is generated from sources kept elsewhere is not determinable from this checkout.
34. The five `DEFAULT_ADAPTERS` anchors have never all been resolved against a live fetch inside the suite — the zai coverage test says so explicitly ("Both are silent for as long as nobody runs the cron"), and deepseek plus both xiaomi anchors have no captured-page test at all. The price watcher itself has no scheduler registration anywhere in the repo, no recorded `--state` path, and no loop back from a review card to `price_windows_verified`.
35. The `t_<hex8>` card ids that anchor most comments, tests and branch names live in an external Kanban DB, so `(t_1c6a002d, pedaço 2)` and the `CA1..CA9` acceptance numbers cannot be resolved from this checkout.
36. There is **no CONTRIBUTING.md, no commit-message linter, and no hook** enforcing any convention in §7 — they are entirely emergent from 222 commits of history.
---

## 10. What has already superseded this snapshot

Twenty-one commits landed after `c3962a5`, and most of them contradict something
above. They are listed here rather than edited into place, because §8 is a
catalogue of measured defects and deleting the measurement would defeat its
purpose — the convention this repo already applies to its specs.

**Read this section before trusting §8 or §9.**

### Correctness

| commit | supersedes | now |
|---|---|---|
| `e1021bc` | §9 #1 (CI red), §3 stage 13b, §8 "the substitution is capability-blind" | `_reachable_replacement` searches the **plan**, then the declared chain, then `blocklist.fallback_chain`; it denies only when all three offer no clean rail. `_dispatch_provider` lost a provably dead loop. `lint_warnings` reports a tier member missing from `fallback_chain`. |
| `41c64df` | §9 #3 (partly), §8 "`breaker_status()` mutates" | `BreakerState.would_block` / `Blocklist.would_block` — the same answer with no state change. **Five reporting surfaces** used the mutating form (`blocked_entries`, `/liveness`, the provider index, `router blocklist`, `router explain`/`chain`), so merely LOOKING at the breaker consumed an expired rail's single probe slot and, since HALF_OPEN is left only by a recorded outcome, excluded it permanently. |
| `f1a0c60` | §9 #3 (the rest) | `_VettedOnce` memoises `is_blocked` per DECISION. `_veto_blocked` and `_vet_plan_chain` asked about the same pair and got opposite answers, so the decision NAMED a refused elo with `blocked_model`/`cause` absent while `chain[0]` ran something else — and the granted probe was never spent. |
| `dc1102e` | not in the snapshot | `_ObservingBlocklist`: shadow mode measures without spending. The hook runs a full `route()` and only then checks the mode, so the SHIPPED DEFAULT mutated and persisted breaker state for cards it never dispatches. |
| `849d55f` | not in the snapshot | `blocklist:` shape is guarded in `Blocklist.__init__` AND hard-linted. `blocklist: off` linted clean — so the write gate accepted it — then raised out of the constructor, took all routing down, and left **every manual ban unenforced**. One malformed ROW did the same to the whole list. |
| `ee1f0fc` | §8 "an explicitly requested `model` is paired with the ROUTER's provider" | The rail now comes from the hop naming that model, or from nowhere. |
| `118e373` | §8 "`bad_args` for a routing decline" | Six declines carry their own `failure_kind` + `retryable`. `bad_args` had told the caller to name a profile — which skips routing, and therefore skips the blocklist that had just refused. Stage 0's deny now carries `blocked_model` and a `reason`. |
| `8a9180e` | §8 `_matching_clauses` any-vs-all, §8 `_detect_stacktrace`'s `"error:"` note | Chips use ALL, like the matcher. `" runtime error"` lost its leading space (it could not match at a line start). A non-hashable `run_id` no longer raises out of two "never raises" readers. |
| `a7f2068` | §8 `_load_router_config` returning `{}` silently | A non-mapping root is guarded and LOGGED once. It used to raise out of `register()` — before `_REGISTERED_CTX` was set, so a retry re-raised — so the plugin did not load at all. |
| `5b39117` | §9 #19 and #21 | A capability key the registry would drop is a hard lint error, in the wording the spec promised. The `min_context → context_window` alias promise is deleted from the spec rather than implemented. |

### The gate itself

| commit | what was wrong |
|---|---|
| `dd9ec2f` | A module-level `pytest.importorskip("fastapi")` in the MIDDLE of `tests/test_webui_extension.py` skipped the whole module, so **67 tests never ran in CI** — including every console-contract scan. And `dashboard/plugin_api.py` (185 statements) was outside the coverage denominator entirely while `--cov-fail-under=100` passed. Split, fastapi added to CI, `node --check` added for the dashboard bundle, and the one-inline-`<script>` count made attribute-tolerant and actually counted. |
| `21cd943` | `from datetime import UTC` aborted COLLECTION on 3.10 — zero tests, not one failure. |

### Operator-facing

| commit | what was wrong |
|---|---|
| `7da8ba5` | `MODEL_WINDOWS`/`SUMMARIZER_WINDOW` are GONE; the compaction curve reads the registry. Three of four entries disagreed with it, including the shipped `compaction.model` (272,000 vs 131,072), and the read path and the RESTART-class apply took DIFFERENT sources. |
| `fc9d15e` | `policy()` serves `enabled` (the field the engine honours) and stops inventing `status`. A rule the operator disabled read back as enabled, so the console's switch turned itself on again. |
| `9e17b94` | `RAIL_WINDOWS` matches the registry: the xiaomi 0.8× discount the registry publishes for NO elo is gone, and deepseek's Mon-Fri gate is present. The 168-hour arithmetic is re-swept: **133 quiet / 20 priced / 15 reordering**, Mon-Fri — four documents and a dead test citation said 119/29/49 "daily". |
| `d201683` | A successful save rebuilds the open inspector. It used to leave the panel bound to a nulled draft, so the second edit was silently discarded with zero POSTs. |
| `e7eb54e` | `var(--soft)` was undefined, so three declarations were dropped entirely — two panel dividers did not render and the marked chip had no fill. |
| `5925b15` | Four facts written twice, all four disagreeing: the watchdog config key the `at_capacity` error told the operator to edit, the classifier default (a vendor alias the plan silently substitutes), the `timeout` default the MODEL reads, and a review keyword the extractor could never produce. |
| `f898d63`, `e9213a0`, `0b2ad8f` | Documentation. The sidecar described itself as read-only while owning every write route; four places asserted framing headers that have never existed; the README's Requirements never named the LLM trust grant without which Stage 1 never runs; and `/apply/confirm`, `whenWords` and `known_models` are deleted. |

### Decisions taken, with their reasoning

The repo is pre-production, so questions that would otherwise wait for an operator
were DECIDED on the evidence rather than held. Each is recorded here with what
settled it, so a wrong call is visible and reversible.

| question | decision | what settled it |
|---|---|---|
| Which id does the dashboard key on? | **`hermes-smart-router`** | Three of four sites already said so; the BUNDLE was the lone outlier because `40f533d` renamed three and left it. `plugin.yaml:name` and the tool name stay `delegate-profile` — the migration doc keeps both deliberately. (`08e8d2f`) |
| Is recursive `delegate_profile` refused? | **Yes, and now enforced** | The README ×2, PRODUCT.md and this document all asserted the guarantee as fact while nothing read the variable. A nested `_spawn` gets its own session and pgid, so a depth-2 tree escapes both the outer `killpg` and the atexit registry. Only the TOOL is withheld; both hooks stay registered. (`19bff0e`) |
| Should HALF_OPEN self-heal, or need a manual reset? | **Self-heal after one `backoff_seconds`** | There IS no reset or unban command anywhere in this repo, so "require a reset" is not an option that exists — it is permanent exclusion with extra steps. (`b3cadf5`) |
| Keep or retire `dashboard/`? | **Keep and fix** | Documented in README and PRODUCT.md; its parity tests exist because the directory is deployed by file COPY and can land beside an older `router/`. The coverage argument for deleting it was refuted — `dd9ec2f` put its 185 statements inside the gate instead. (`08e8d2f`) |
| Should the classifier follow a hot edit? | **Yes** | `enabled` is in `_HOT_KEYS` and `router.example.yaml` promises "live with no restart". Only the process-stable half (does the host expose `ctx.llm`) is still decided at register. (`1a8d4ae`) |

### Still open, and deliberately not decided here

* **Does the deployed host's `VALID_HOOKS` contain `pre_kanban_dispatch`?** Declaring
  it in `plugin.yaml` would turn a `plugin doctor` WARNING into an ERROR on an older
  host. Unverifiable from this checkout, and the failure mode is worse than the
  inaccuracy, so the manifest still under-declares. Check the box, then declare it.
* **Should `/explain` be genuinely vetted or only labelled?** Labelling is safe now
  that probe-free reads exist; genuine vetting decides whether Simular may claim to
  be the plan that runs. A product question.
* **Should a vetoed explicit `model` RETRY on the `fallback_model` the router
  named?** `fallback_model` is computed and has zero consumers; `118e373` only
  reports it. Retrying is a behaviour change with no evidence behind it yet.
* From §9: the `_load()` vs write-gate validation asymmetry (#10),
  `breaker_cooldown` with no producer (#13), and the unenabled stale-check timer
  (#25 — nothing in the repo arms it).

**Observed once, cause unknown.** In one full run under `--cov`,
`tests/router/test_cli.py::TestCLIChainTime::test_the_clock_reaches_the_planner_and_the_feature_vector`
and `::test_a_time_blind_explain_does_not_answer_for_the_requested_hour` both
failed; every subsequent full run was green, and the class passes in isolation.
Both tests monkeypatch the module global `rules.plan_chain`, which
`router/adapter.py` has already bound by value at import and whose signature it
probed once into `_PLAN_CHAIN_ACCEPTS_WHEN` — the same import-time-flag hazard §8
records for `_ORDER_CHAIN_ACCEPTS_WHEN`. That is a hypothesis, not a diagnosis: it
is written down because an intermittent failure nobody recorded is one nobody can
reproduce.
