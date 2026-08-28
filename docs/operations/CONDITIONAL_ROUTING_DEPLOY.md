# Deploying conditional routing — procedure and rollback

Covers everything on `feat/conditional-time-routing` on top of `origin/main` at `baae9e1` — i.e.
`git log --oneline baae9e1..feat/conditional-time-routing`. It was three commits (`1ab0c1a`, `51e4778`,
`dfc4f21`) when this was written and the branch has grown since, so read the range, not the three ids;
anything below that counts them ("all three commits are code") means the whole range. Companion documents:
the design spec and the time-window addendum under `docs/superpowers/specs/`. The general Hermes update
procedure this defers to is the operator's `hermes-update-RUNBOOK.md`; everything below is specific to this
change.

Every fact in this document was verified on the running box, not assumed. Where something is unverified
it says so.

**These claims go stale when the code moves, and this document has already proved it.** Two statements
here were false against the commit that shipped alongside them — §7 said no human-facing surface rendered
`unsatisfiable` or `peak_priced` (the console and the CLI both do) and §4 said the collapse script
re-parsed after writing (it did not, until it was made to). A document that opens by promising verified
facts is more dangerous when it rots than one that never claimed it, so: **re-run the checks in §6 and
re-read §4 and §7 against the code on every change to `router/capabilities.py`, `router/rules.py`,
`router/cli.py`, `scripts/collapse_profile_routing.py` or the console, and correct this file in the same
commit.** Prefer a claim a command can reproduce over a claim a reader has to trust.

## 0. What the runtime looks like right now

Observed on the box on 2026-08-18. Everything in this table is BOX STATE, not repository state, so it is
the part of this document a code change cannot keep true — re-run the right-hand column before relying on
any row.

| Fact | Value | How it was established |
|---|---|---|
| Plugin directory | `/home/rodrigo/.hermes/plugins/hermes-smart-router` | not a symlink; a real git checkout |
| Branch / HEAD | `main` at `baae9e1`, working tree clean | `git status --porcelain` empty |
| Tracking | in sync with `origin/main`, 0 ahead / 0 behind | `git rev-list --left-right --count HEAD...@{u}` |
| Interpreter | `/home/rodrigo/Workspace/hermes-agent/venv/bin/python`, 3.11.15 | `systemctl --user show hermes-gateway -p ExecStart` |
| `router.yaml` | **gitignored** (`.gitignore:44`), runtime state, not in any commit | `git check-ignore -v router.yaml` |
| `router.example.yaml` | tracked; this is what a fresh install reads | `git ls-files` |

Two consequences worth holding onto. First, the plugin being a clean checkout in sync with its remote
means git is the rollback for code — no file-copy deploy, no manual backup of source. Second,
`router.yaml` is NOT code: swapping the policy and deploying the code are two independent steps that can
be done in either order and rolled back independently.

The box's `~/Workspace/hermes-delegate-profile` is a **stale, abandoned checkout, 39 commits behind**.
It is not what runs. Do not deploy from it; the port in these commits was rebased off it onto `baae9e1`
precisely because it misleads.

## 1. What needs a restart and what does not

From the operator's runbook, confirmed against this change:

- **Code changes are inert until the gateway restarts.** Every commit in the range is code.
- **`router.yaml` is HOT.** `RouterService` re-reads it per request, and the runbook independently records
  that the fallback chain is re-read per turn. A policy swap takes effect with no restart.
- `config.yaml` and the 15 per-profile configs are RESTART-class for anything the agent binds at
  construction.

So: a policy swap alone is a live, restart-free, instantly revertible change. A code deploy is not.

## 2. Deploying the code — two options

Both are reversible. Neither has been executed; the branch has only been validated in an isolated
worktree at `/tmp/hdp-branch`, which left the runtime untouched.

### Option A — merge upstream, then pull (the project's normal flow)

    # off-box: merge feat/conditional-time-routing into main on the remote
    # then, on the box:
    cd ~/.hermes/plugins/hermes-smart-router
    git pull --ff-only

Leaves the runtime tracking `origin/main` exactly as it does today, so the next person to look at it
sees a normal checkout. Requires the merge to happen first, which is a decision and an action outside
the box.

### Option B — check the branch out in place (no upstream change)

    # transfer once (a 412K bundle of just these three commits):
    #   local:  git bundle create /tmp/branch.bundle baae9e1..feat/conditional-time-routing
    #   scp it to the box, then:
    cd ~/.hermes/plugins/hermes-smart-router
    git fetch /tmp/branch.bundle feat/conditional-time-routing:feat/conditional-time-routing
    git checkout feat/conditional-time-routing

Entirely local to the box and instantly revertible with `git checkout main`. The cost is that the
runtime is then on a branch that is not `origin/main`, so it will read as diverged until the merge
happens. Prefer this to test in place before committing to the merge; prefer A once the merge is done.

### Then, either way

    systemctl --user daemon-reload
    systemctl --user restart hermes-gateway
    systemctl --user restart hermes-webui hermes-dashboard hermes-office-web \
                             hermes-memory-sidecar hermes-router-sidecar

A stop logs `exited, code=exited, status=1` for the OLD pid. That is the old process leaving, not the new
one failing — the runbook is emphatic about not chasing it. Judge health only by these:

    for u in hermes-gateway hermes-webui hermes-dashboard hermes-office-web \
             hermes-memory-sidecar hermes-router-sidecar; do
      printf '%-26s %s\n' "$u" "$(systemctl --user is-active $u)"
    done
    systemctl --user show hermes-gateway -p MainPID,NRestarts,ExecMainStartTimestamp

All six must be `active`, `NRestarts` should be `0`, and the gateway's start timestamp must be AFTER the
checkout — if it started before, it is still running the old code.

**Before state, captured for comparison:** all six `active`, all `NRestarts=0`; gateway, webui and
dashboard started 2026-08-18 12:14, the three older units 2026-08-15 16:52. The box is in active use —
this restart interrupts live work, so pick the moment.

## 3. Swapping the policy

Independent of the code deploy, and safe to defer. The new policy is a working file in the porting tree
at `/tmp/hdp2/router.yaml`; the tracked template `router.example.yaml` carries the same shape for fresh
installs.

    cp ~/.hermes/plugins/hermes-smart-router/router.yaml \
       ~/.hermes/backups/pre-conditional-routing-20260818T0000Z/router.yaml.pre-swap
    # copy the new policy into place, then verify BEFORE any traffic depends on it:
    cd ~/.hermes/plugins/hermes-smart-router
    /home/rodrigo/Workspace/hermes-agent/venv/bin/python -m router.cli --config router.yaml lint

Rollback is a `cp` back from the snapshot; the change is live per request either way.

**The new code runs correctly against the OLD policy.** That is what makes the two steps independent, and
it is the scenario validated in the isolated worktree rather than assumed: the pre-change
`router.example.yaml` lints clean under the substantially stricter gate, every tier resolves identically,
and no phase-2 key is materialised. So deploying code without swapping policy changes no routing
behaviour — the feature simply sits dormant until a tier declares a strategy, a requirement or a clock
knob.

## 4. Collapsing the per-profile routing blocks — the risky step

This is what makes "one canonical chain with rare overrides" real instead of theoretical. Today the
routing order exists in 18 places: the global `config.yaml`, `router.yaml`'s `tiers`, its
`blocklist.fallback_chain`, and 15 per-profile `config.yaml` copies that each redeclare `model`,
`fallback_providers` and `auxiliary.vision`. A single canonical chain is unusable while 15 copies shadow
it.

It is also the one step the operator's runbook records having already gone wrong: *"All 16 configs
corrupted at once — a regex/sed edit across `config.yaml` + `profiles/*/config.yaml`. Restore from
`config-snapshot/`. Only ever edit with Python + PyYAML, then re-parse all 16."* The script honours both
halves of that constraint:

| Constraint | How the script honours it | How you can see it |
|---|---|---|
| PyYAML only, never sed | `yaml.safe_load` / `yaml.safe_dump`, no regex over the file body | `scripts/collapse_profile_routing.py` imports `yaml` and nothing else that touches content |
| Re-parse all 16 after the write | `verify_after_write()` re-reads **every** discovered `profiles/*/config.yaml`, and additionally requires each rewritten one to re-parse **equal to the document that was planned** | a clean `--apply` prints `re-parsed all N profile config(s) after the write …`; a failure prints `POST-WRITE RE-PARSE FAILED`, names the file, points at the backup, and exits `3` |
| Nothing silently changes but comments | permissions are carried across the atomic replace | `tests/test_collapse_profile_routing.py::test_every_rewritten_file_keeps_the_mode_it_had` asserts mode-after == mode-before per file |

The re-parse was added because this document previously asserted it and the script did not do it —
`tempfile.mkstemp` + `os.replace` also meant every rewritten file dropped from 0664 to 0600, which the
same fix closed. On top of that: dry-run by default, write-access pre-flight before the first mutation,
and a mandatory timestamped backup.

Both behaviours are covered by tests, so the claim above is checkable without the box:

    python3.11 -m pytest tests/test_collapse_profile_routing.py -q

    V=/home/rodrigo/Workspace/hermes-agent/venv/bin/python
    cd ~/.hermes/plugins/hermes-smart-router
    $V scripts/collapse_profile_routing.py --hermes-home ~/.hermes --stamp 20260818T0000Z   # dry-run
    # read the per-file report, then:
    $V scripts/collapse_profile_routing.py --hermes-home ~/.hermes --stamp 20260818T0000Z --apply

`--dry-run` is the default; writing needs an explicit `--apply`. **It must be run with that venv python** —
no system interpreter on the box has PyYAML, so `python3` would fail on import.

Read the exit status, do not just read the report: `0` succeeded (dry-run included), `2` a usage error or a
target that would not parse — **nothing was written**, `3` the write was refused up front, failed mid-run,
**or the post-write re-parse failed**. On a `3` do not restart anything until you have read the named files
and, if the re-parse is what failed, restored them from the backup directory the diagnostic prints.

Rollback: the script's own timestamped backup, plus the pre-existing snapshot at
`~/.hermes/backups/pre-conditional-routing-20260818T0000Z/routing-config/`, which holds `config.yaml`,
`auth.json`, `router.yaml.live` and all 15 profile `config.yaml`/`profile.yaml` pairs — 33 files, 328K,
every one verified to re-parse under PyYAML 6.0.3.

Why the profiles inherit at all, since the whole step depends on it: a profile that omits a key resolves
to the root. `trama-engineer` proves it — it omits `reasoning_effort` and inherits the root's `max`.

## 5. Rolling the whole thing back

    cd ~/.hermes/plugins/hermes-smart-router
    git checkout main                     # or: git reset --hard baae9e1 after a pull
    cp ~/.hermes/backups/pre-conditional-routing-20260818T0000Z/routing-config/router.yaml.live \
       router.yaml
    systemctl --user daemon-reload
    systemctl --user restart hermes-gateway
    systemctl --user restart hermes-webui hermes-dashboard hermes-office-web \
                             hermes-memory-sidecar hermes-router-sidecar

If the profile collapse was applied, also restore the 15 profile configs from
`.../routing-config/profiles/` before restarting.

## 6. Verifying the deploy did something

Unit tests passing is not evidence the feature is live. This change spent an entire development phase
fully implemented and completely inert in production, because `explain()` displayed a filtered chain
while the executor attempted the declared one. Check the running path, not the reporting surface:

    V=/home/rodrigo/Workspace/hermes-agent/venv/bin/python
    cd ~/.hermes/plugins/hermes-smart-router

    # 1. The planner is actually wired in. This is the guard that phase's defect earned.
    $V -m pytest tests/router/test_adapter.py -k the_installed_planner_is_wired -q

    # 2. A vision task's chain contains only elos that can see.
    $V -m router.cli --config router.yaml chain \
       "Look at this screenshot and fix the chart in the attached image"

    # 3. The clock reaches the decision. THREE hours, because two is what made the
    #    policy comments wrong: 07:00Z and 15:00Z between them miss every hour in
    #    which T3's avoid_peak actually reorders. Use a task that reaches T3.
    T="refactor the authentication module and migrate its callers"
    $V -m router.cli --config router.yaml chain --at 2026-08-17T02:00:00Z "$T"
    $V -m router.cli --config router.yaml chain --at 2026-08-17T07:00:00Z "$T"
    $V -m router.cli --config router.yaml chain --at 2026-08-17T15:00:00Z "$T"

    # 4. The policy passes the fail-closed gate.
    $V -m router.cli --config router.yaml lint

Step 3 must produce **three different plans**, and each one checks a different thing:

| Hour | Expected | What it proves |
|---|---|---|
| `02:00Z` | chain `gpt-5.6-terra, glm-5.3, deepseek-v4-pro`; `demoted: deepseek-v4-pro`; `peak_priced: deepseek-v4-pro` | deepseek peaks and zai does not, so `avoid_peak` really reorders — the second hop changes rail |
| `07:00Z` | chain `gpt-5.6-terra, deepseek-v4-pro, glm-5.3`; `demoted` **empty**; `peak_priced: deepseek-v4-pro, glm-5.3` | both peak, so the permutation is the identity and the trace says "nothing moved" instead of claiming a reorder |
| `15:00Z` | declared order, both lists empty, every multiplier `x1.0` | outside every window the clock changes nothing |

If all three are identical, the clock is not reaching the decision. Against the **old** policy they will be
identical and that is correct, because it declares no clock knob — check which policy is in place before
reading this as a failure. Do not substitute a T1 task (`"add a docstring to the helper"`, which the earlier
version of this step used): T1 is byte-identical at all 168 hours by design, so it cannot distinguish a live
clock from a dead one.

## 6b. What a claim about the clock has to be verified over

Every time-dependent claim in `router.yaml`, `router.example.yaml` and this file is stated over **the whole
168-hour week**, not over sample hours, because sampling is how three of them ended up wrong. The
partitions the shipped policy actually produces, measured by running `rules.plan_chain` at all 168 hours:

| Tier | Behaviour | Hours (of 168) |
|---|---|---|
| `T1` | `capped: []`, order unchanged | 168 (100%) — the cap removes nothing at any hour |
| `T2` | tail `deepseek-v4-flash, gpt-5.6-luna` | 119 (70.8%) |
| `T2` | tail flips to `gpt-5.6-luna, deepseek-v4-flash` | 49 (29.2%) — deepseek's own two windows, 01:00-04:00 and 06:00-10:00 UTC **daily** |
| `T3`/`T4` | nothing matches, declared order | 119 (70.8%) |
| `T3`/`T4` | reorders — `demoted: deepseek-v4-pro` | 29 (17.3%) — 01:00-04:00 UTC daily + 06:00-10:00 UTC at the weekend |
| `T3`/`T4` | identity, `peak_priced` names both | 20 (11.9%) — 06:00-10:00 UTC Mon-Fri |

`tests/router/test_capabilities.py::test_the_shipped_t3_policy_reorders_for_29_hours_of_the_week` and
`::test_the_shipped_t2_tail_flips_on_deepseeks_window_alone` pin these numbers, so a registry or window
change that invalidates the table fails the suite instead of quietly outdating this page.

## 7. Known and deliberate, at time of writing

- `T1`'s `time_cap` removes nothing at any hour, and that one is genuinely inert: `max_multiplier` is a
  DOLLAR ceiling, and T1's only peaking elo is plan-covered `glm-5.3-flash`, whose 2.0x doubles a credit draw
  and no invoice. Verified over all 168 hours, `capped: []` at every one. Changing a chain shape changes
  which model serves real traffic, so it was left to the operator.
  **`T3`/`T4`'s `avoid_peak` is NOT inert** — an earlier version of this bullet said both knobs "earn
  nothing … neither tier holds a dollar-billed elo that peaks", and both halves are wrong: metered
  `deepseek-v4-pro` is dollar-billed and peaks 2.0x at 01:00-04:00 and 06:00-10:00 UTC daily, and the
  policy reorders the chain for 29 of the week's 168 hours (see §6b). It changes which model serves the
  second hop of every hard and adversarial task, from metered `deepseek-v4-pro` to plan-billed `glm-5.3`.
  Treat it as live behaviour, not as a dormant knob.
- `unsatisfiable` and `peak_priced` **do** render on human-facing surfaces — an earlier version of this
  bullet said nothing did. Both are rendered by the router console
  (`webui_extension/hermes-smart-router/console.html`: `unsatisfiableWords()` emits the amber
  "Requirement no model can meet." line, `peakPriceWords()` the peak-pricing line) and by the CLI
  (`router/cli.py`: `_unsatisfiable_lines()`, and `peak_priced` in the time-flag block, including the
  extra "nothing moved" line when `demoted` is empty). They also reach the plan, the trace and the JSON
  output. `cap_exempt` is the field that genuinely does not surface: `capabilities.apply_time_cap`
  returns it and `rules.plan_chain` drops it, so an exemption is visible only as `multipliers` and its
  reason (plan credits, not dollars) is nowhere in the plan.
- `requirements.min_context` means **input** tokens, not window size, and is now enforced against
  `max_input_tokens` where a vendor publishes one. Five elos do — `gpt-5.6-sol`/`terra`/`luna` accept
  922_000 of prompt inside a 1_050_000 window, `gpt-5.3-codex`/`gpt-5.4-mini` 272_000 inside 400_000 — so
  a request whose estimate exceeds that bound now routes past them instead of to them. This TIGHTENS the
  filter only, it never loosens it, and `filter_chain` still bypasses rather than emptying a chain. An elo
  that publishes no separate input bound is taken at its window (`gpt-5.5` is the pointed example: same
  provider and window as `gpt-5.6-sol`, but no published input limit, and the registry will not invent
  one). Nothing in the shipped policy changes — `T3`'s floor is 200_000 — but a floor above 272_000 on a
  tier holding a codex-mini rail now behaves differently than it did.
- The policy is designed around a z.ai Coding Plan that expires in roughly two months, and the exit is
  now PRICED. An earlier version of this bullet said `glm-5.3` "may not be purchasable per token at all —
  its metered API is not live, it carries no published price", and named `glm-4.7` at $0.60/$2.20 as the
  metered successor. Both halves went stale on 2026-08-27: `glm-5.3` is metered at **$1.40/$4.40**, and
  `glm-4.7` is no longer a model on a plan key at all — the plan dropped it and auto-routes the id to
  `glm-5.3-flash`. The exit today is `glm-5.3-flash` metered at **$0.15/$0.50** list (50% promo to
  2026-09-09 16:00 UTC; budget the list) for T1 and T2 — both now run on it — and `glm-5.3` at
  $1.40/$4.40 where the flagship is wanted, which is T3/T4's third hop. The blast radius of the expiry
  is therefore ~9x cheaper per token than it was under the old T2 primary.
  Nothing needs a chain edit at expiry — both plan ids keep working, the bill just starts
  arriving in dollars, which is what `cheapest_now`'s billing_mode bucketing is there to notice. Re-read
  the vendor's credit table before the switch: this bullet has been wrong once already.
- **`T2` — the general-purpose rail — runs `glm-5.3-flash`, not the flagship.** Whatever the classifier
  cannot place elsewhere lands on T2, so this is the most consequential name in the policy. The trade,
  measured rather than assumed: 8 output credits against 24 (3x the work off the same allowance; the
  vendor's own weekly estimate is 146–292M tokens against 48–97M on Lite), and 9.3x/8.8x cheaper in
  dollars. Against that, the vendor's own self-reported tables put Flash under four points behind on
  Terminal-Bench 2.1 (84.3 vs 88.2) and DeepSWE v1.1 (63.4 vs 66.9), further behind on HLE-with-tools
  (55.3 vs 62.5), and AHEAD on Toolathlon Verified (78.4 vs 73.0), AutomationBench and GDPval-AA.
  Single-file standard-pattern work is the tool-use end of that, and the flagship's clear win — long
  trajectories and hard reasoning — is T3/T4 work, where `glm-5.3` is still the third hop.
  Two consequences worth knowing: **T1 and T2 now share a primary** (there is exactly one plan rail below
  the flagship), so the tiers are separated by their fallback economics and time policy rather than by the
  primary — T1 refuses a peak-rate dollar and falls to `mimo-v2.5` at $0.28, T2 reorders by the hour and
  falls to `deepseek-v4-flash` at $0.66. And **`vision-required` finally stops costing money**: Flash is
  natively multimodal, so an image turn stays on the plan rail instead of being filtered onto the
  subscription seat. If the vendor ever gives the plan a second rung below the flagship, T1 is where to
  spend it.
- **The plan's model coverage changed under the config and nothing caught it.** `router/price_watch.py`
  watches z.ai's devpack page, but its anchor is the peak-hours clause ("Singapore Standard Time"), which
  did NOT change; the *supported models* and *credit multiplier* sections did. So the watcher reported no
  change while four names in policy became vendor aliases. A window is not the only fact a config leans
  on: coverage is the other, and it deserves its own anchor.
