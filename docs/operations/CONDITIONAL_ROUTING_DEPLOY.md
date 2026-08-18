# Deploying conditional routing — procedure and rollback

Covers the three commits on `feat/conditional-time-routing` (`1ab0c1a`, `51e4778`, `dfc4f21`) on top of
`origin/main` at `baae9e1`. Companion documents: the design spec and the time-window addendum under
`docs/superpowers/specs/`. The general Hermes update procedure this defers to is the operator's
`hermes-update-RUNBOOK.md`; everything below is specific to this change.

Every fact in this document was verified on the running box, not assumed. Where something is unverified
it says so.

## 0. What the runtime looks like right now

| Fact | Value | How it was established |
|---|---|---|
| Plugin directory | `/home/rodrigo/.hermes/plugins/delegate-profile` | not a symlink; a real git checkout |
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

- **Code changes are inert until the gateway restarts.** All three commits are code.
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
    cd ~/.hermes/plugins/delegate-profile
    git pull --ff-only

Leaves the runtime tracking `origin/main` exactly as it does today, so the next person to look at it
sees a normal checkout. Requires the merge to happen first, which is a decision and an action outside
the box.

### Option B — check the branch out in place (no upstream change)

    # transfer once (a 412K bundle of just these three commits):
    #   local:  git bundle create /tmp/branch.bundle baae9e1..feat/conditional-time-routing
    #   scp it to the box, then:
    cd ~/.hermes/plugins/delegate-profile
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

    cp ~/.hermes/plugins/delegate-profile/router.yaml \
       ~/.hermes/backups/pre-conditional-routing-20260818T0000Z/router.yaml.pre-swap
    # copy the new policy into place, then verify BEFORE any traffic depends on it:
    cd ~/.hermes/plugins/delegate-profile
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
routing order exists in 18 places: the global config, `blocklist.fallback_chain`, and 15 per-profile
`config.yaml` copies that each redeclare `model`, `fallback_providers` and `auxiliary.vision`. A single
canonical chain is unusable while 15 copies shadow it.

It is also the one step the operator's runbook records having already gone wrong: *"All 16 configs
corrupted at once — a regex/sed edit across `config.yaml` + `profiles/*/config.yaml`. Restore from
`config-snapshot/`. Only ever edit with Python + PyYAML, then re-parse all 16."* The script honours that
constraint — PyYAML only, re-parse after write, dry-run by default, write-access pre-flight before the
first mutation, and a mandatory timestamped backup.

    V=/home/rodrigo/Workspace/hermes-agent/venv/bin/python
    cd ~/.hermes/plugins/delegate-profile
    $V scripts/collapse_profile_routing.py --hermes-home ~/.hermes --stamp 20260818T0000Z   # dry-run
    # read the per-file report, then:
    $V scripts/collapse_profile_routing.py --hermes-home ~/.hermes --stamp 20260818T0000Z --apply

`--dry-run` is the default; writing needs an explicit `--apply`. **It must be run with that venv python** —
no system interpreter on the box has PyYAML, so `python3` would fail on import.

Rollback: the script's own timestamped backup, plus the pre-existing snapshot at
`~/.hermes/backups/pre-conditional-routing-20260818T0000Z/routing-config/`, which holds `config.yaml`,
`auth.json`, `router.yaml.live` and all 15 profile `config.yaml`/`profile.yaml` pairs — 33 files, 328K,
every one verified to re-parse under PyYAML 6.0.3.

Why the profiles inherit at all, since the whole step depends on it: a profile that omits a key resolves
to the root. `trama-engineer` proves it — it omits `reasoning_effort` and inherits the root's `max`.

## 5. Rolling the whole thing back

    cd ~/.hermes/plugins/delegate-profile
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
    cd ~/.hermes/plugins/delegate-profile

    # 1. The planner is actually wired in. This is the guard that phase's defect earned.
    $V -m pytest tests/router/test_adapter.py -k the_installed_planner_is_wired -q

    # 2. A vision task's chain contains only elos that can see.
    $V -m router.cli --config router.yaml chain \
       "Look at this screenshot and fix the chart in the attached image"

    # 3. The clock reaches the decision: the same task at two hours, two answers.
    $V -m router.cli --config router.yaml chain --at 2026-08-17T07:00:00Z "add a docstring to the helper"
    $V -m router.cli --config router.yaml chain --at 2026-08-17T15:00:00Z "add a docstring to the helper"

    # 4. The policy passes the fail-closed gate.
    $V -m router.cli --config router.yaml lint

Step 3 only differs once the policy declares a clock knob, so against the old policy expect identical
output — that is correct, not a failure.

## 7. Known and deliberate, at time of writing

- `T1`'s `time_cap` and `T3`/`T4`'s `avoid_peak` earn nothing for their current chain shapes: neither
  tier holds a dollar-billed elo that peaks. The stages are correct; the shapes cannot exercise them.
  Changing a chain shape changes which model serves real traffic, so it was left to the operator.
- Nothing renders `unsatisfiable` or `peak_priced` on a human-facing surface yet. Both reach the plan,
  the trace and the JSON output.
- The policy is designed around a z.ai Coding Plan that expires in roughly two months. When it does,
  `glm-5.3` may not be purchasable per token at all — its metered API is not live, it carries no
  published price, and the metered catalogue starts at GLM-5.2. The metered successor is `glm-4.7` at
  $0.60/$2.20, which is already `T1`'s primary. Plan the switch before the expiry, not after.
