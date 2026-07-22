# delegate-profile

[![CI](https://github.com/rodrigogs/hermes-delegate-profile/actions/workflows/ci.yml/badge.svg)](https://github.com/rodrigogs/hermes-delegate-profile/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-77%25-yellow)](https://github.com/rodrigogs/hermes-delegate-profile)
[![Tests](https://img.shields.io/badge/tests-198%20passed-brightgreen)](https://github.com/rodrigogs/hermes-delegate-profile)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.3.0-informational)](https://github.com/rodrigogs/hermes-delegate-profile)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/rodrigogs/hermes-delegate-profile/blob/main/LICENSE)

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds a
`delegate_profile` tool for spawning subagents under a **different** Hermes
profile as a **fully isolated subprocess** — with an optional **capability router**
that auto-selects the best profile + model based on task difficulty.

## Why this exists (read this first)

The built-in `delegate_task` **already supports** `profile=` for *in-process*
cross-profile delegation — it swaps the child's config, secret scope, SOUL, and
toolsets within the same process. That path is fast.

This plugin is for the **subprocess-isolation** case, where you want a hard
process boundary around the child:

|                                     | `delegate_task(profile=...)` | `delegate_profile` (this plugin) |
|-------------------------------------|------------------------------|----------------------------------|
| Isolation                           | In-process (shared process)  | Separate OS process              |
| Child crashes parent?               | Yes (same process)           | No (process boundary)            |
| Child toolset                       | Can only **narrow** parent's | Target profile's **full** toolset|
| Different Hermes version in child?  | No                           | Yes                              |
| Overhead                            | Low                          | Process spawn + cold start       |
| Best for                            | Fast parallel subtasks       | Crash isolation / full toolset   |

Rule of thumb: if you'd be happy running the subagent in the current process,
use `delegate_task(profile=...)`. If the subprocess boundary itself is the
point — crash safety, the target profile's full toolset, a different Hermes
version — use `delegate_profile`.

```text
delegate_profile(goal="Run the firmware flash suite",     profile="firmware-engineer")
delegate_profile(goal="Review this PR for security",      profile="reviewer")
delegate_profile(goal="Summarize these papers",           profile="researcher", model="anthropic/claude-sonnet-4")
```

## What it does

- Adds a **`delegate_profile`** tool to the `delegation` toolset.
- For **cross-profile** calls, spawns a one-shot
  `hermes -p <profile> chat -q "<goal>"` subprocess. The child runs fully
  isolated under the target profile — its own process, session, skills,
  memory, and model.
- For **same-profile** calls (profile omitted or matching the active profile),
  transparently routes to the built-in `delegate_task` via `ctx.dispatch_tool`
  (which wires up `parent_agent`) so you pay no subprocess overhead.
- **Validates the target profile exists before spawning** — a typo produces an
  instant, actionable error listing available profiles, not a confusing
  subprocess failure.
- Registers a **`post_tool_call` hook** that logs an advisory warning if
  `delegate_task` is invoked with a `profile` argument.

## Install

From the plugin registry:

```bash
hermes plugins install rodrigogs/hermes-delegate-profile
hermes plugins enable delegate-profile
```

Or manually by cloning into your plugins directory:

```bash
git clone https://github.com/rodrigogs/hermes-delegate-profile.git \
  ~/.hermes/plugins/delegate-profile
hermes plugins enable delegate-profile
```

Restart your session (or run `/reset` in the CLI / `/restart` in the gateway)
after enabling — plugin loads happen at startup.

### Requirements

- Hermes Agent (any recent version)
- The `delegation` toolset enabled (`hermes tools enable delegation`)
- One or more additional Hermes profiles. Create one with
  `hermes profile create <name>` and list them with `hermes profile list`.

## Usage

### Minimal

```python
delegate_profile(
    goal="Refactor the auth module to use async/await",
    profile="coder",
)
```

### With context and model override

```python
delegate_profile(
    goal="Review the diff in PR #142 for SQL injection and auth flaws",
    profile="reviewer",
    context="Repo: ~/projects/myapp. Stack: FastAPI + SQLAlchemy. See git diff origin/main.",
    model="anthropic/claude-sonnet-4",
    timeout=600,
)
```

### Parameters

| Parameter  | Type    | Required | Default | Notes |
|------------|---------|----------|---------|-------|
| `goal`     | string  | yes      | —       | What the subagent should accomplish. Be self-contained — the child has no context from your session. |
| `profile`  | string  | no       | `auto`  | Target Hermes profile name. Must exist (`hermes profile list`). If omitted or `auto`, the capability router picks the best profile + model based on task difficulty. If it matches the active profile, routes to in-process `delegate_task`. |
| `context`  | string  | no       | —       | Background info: file paths, error messages, project structure, constraints. |
| `model`    | string  | no       | profile default | Model override passed as `-m` to the child. |
| `timeout`  | integer | no       | `300`   | Max seconds to wait for the subprocess. Override globally via `HERMES_DELEGATE_PROFILE_TIMEOUT`. |

### Result format

Returns a JSON string. On success:

```json
{
  "success": true,
  "subagent_id": "dp_a1b2c3d4e5f6",
  "profile": "reviewer",
  "result": "<stdout from the child hermes process, last 8000 chars>",
  "elapsed_s": 42.3
}
```

On failure (non-zero exit, timeout, or missing binary):

```json
{
  "success": false,
  "subagent_id": "dp_a1b2c3d4e5f6",
  "profile": "reviewer",
  "error": "Subprocess exited with code 1",
  "stderr": "<last 2000 chars of stderr>",
  "elapsed_s": 12.1
}
```

For a missing or non-existent profile:

```json
{
  "success": false,
  "error": "Profile 'reviwer' does not exist. Create it with: hermes profile create reviwer",
  "profile": "reviwer",
  "available_profiles": ["coder", "reviewer", "tester"],
  "hint": "Available profiles: coder, reviewer, tester"
}
```

For missing required args, returns `{"error": "goal is required"}` or
`{"error": "profile is required"}`.

## How it works

1. Resolves the active profile name via Hermes's own `get_active_profile_name()`
   (falls back to `HERMES_PROFILE` env, then `default`).
2. Validates the target profile exists (`hermes_cli.profiles.profile_exists`).
3. If the target **equals** the active profile, routes to `delegate_task`
   through `ctx.dispatch_tool` (in-process, no spawn).
4. Otherwise resolves the `hermes` binary (prefers the one in the current
   venv, falls back to `PATH`), builds the prompt as
   `Context: <context>\n\nTask: <goal>` when `context` is provided, and
   spawns `hermes -p <profile> chat -q "<prompt>"` (plus `-m <model>` when set)
   with `capture_output=True` and the resolved `timeout`.
5. Forwards `HERMES_HOME` so the child resolves profiles from the same place
   as the parent, and sets `HERMES_DELEGATE_PROFILE_DISABLE=1` to prevent
   recursive delegation inside the child.
6. Returns a JSON envelope (see [Result format](#result-format)).

### Capability Router

When `profile` is omitted or set to `auto`, the plugin runs a **capability
router** that picks the best profile + model for the task:

- **Stage 0 — Deterministic rules:** matches task signals (verb class,
  code presence, keyword patterns) against user-defined rules in
  `router.yaml`. Fast, cheap, no LLM call.
- **Stage 1 — LLM classifier:** fires only when rules can't decide (rules
  with `action: classify`). Uses a pinned model (glm-5.2/zai, temp=0,
  token-capped) to classify task difficulty.
- **Fail-safe:** if the classifier is unavailable, falls back to a trusted
  strong model from `router.yaml`.
- **Blocklist:** manual bans + auto-breaker with exponential backoff
  cooldowns for models that stall repeatedly.
- **Decision log:** every routing decision is recorded for observability.

Example routing behavior with the default config:

| Task | Router picks |
|------|-------------|
| `"Rename getCwd in utils.py"` | `coder` + `glm-5.2-fast` (T1) |
| `"Debug race condition in pool"` | `coder` + `claude-opus` (T4) |
| `"Review PR for security"` | `reviewer` + classify (→ fail_safe if no LLM) |

Configure via `router.yaml` — rules, tiers, blocklist, classifier model,
and fail-safe are all user-editable. Run `python -m router.cli explain "task"`
to see routing decisions from the CLI.

The `post_tool_call` hook (`_on_post_tool_call`) is a no-op for every tool
except `delegate_task`. When `delegate_task` is invoked with a `profile`
parameter, it emits a `logger.warning`. The hook never blocks or modifies the
call — it's purely advisory (the built-in `delegate_task(profile=...)` is a
legitimate in-process path; the warning is a nudge for callers who actually
want subprocess isolation).

## Configuration

No config file is required. Behavior can be influenced via environment
variables:

| Variable                          | Default      | Effect |
|-----------------------------------|--------------|--------|
| `HERMES_PROFILE`                  | `default`    | Fallback for active-profile detection when Hermes's resolver is unavailable. |
| `HERMES_HOME`                     | `~/.hermes`  | Forwarded to the child so profiles resolve consistently. |
| `HERMES_DELEGATE_PROFILE_TIMEOUT` | unset        | Global default timeout (seconds) when no `timeout` arg is passed. |
| `HERMES_DELEGATE_PROFILE_DISABLE` | unset        | Set to `1` inside spawned children to prevent recursive delegation. |

## Development

```bash
git clone https://github.com/rodrigogs/hermes-delegate-profile.git
cd hermes-delegate-profile
pip install -e ".[dev]"
```

Project layout:

```
hermes-delegate-profile/
├── plugin.yaml          # manifest (name, version, provides_tools, provides_hooks)
├── __init__.py          # register() — registers the tool + post_tool_call hook
├── router.yaml          # capability router config (rules, tiers, blocklist)
├── pyproject.toml       # project metadata + pytest/coverage config
├── README.md            # this file
├── LICENSE              # MIT
├── router/              # capability router library
│   ├── adapter.py       # Stage 0 → Stage 1 → delegate_profile bridge
│   ├── signals.py       # task signal extraction
│   ├── rules.py         # rule matching engine
│   ├── classify.py      # LLM difficulty classifier
│   ├── blocklist.py     # manual ban + auto-breaker state machine
│   ├── breaker.py       # circuit breaker state
│   ├── cache.py         # exact-hash classifier cache
│   ├── decision_log.py  # JSONL decision recorder
│   └── cli.py           # CLI: explain, lint, blocklist, log
├── dashboard/           # WebUI panel (React + Plugin SDK)
│   ├── manifest.json
│   ├── plugin_api.py    # 6 FastAPI routes
│   └── dist/index.js    # bundled panel code
└── tests/
    ├── test_delegate_profile.py
    ├── test_router_integration.py
    └── router/          # per-module unit tests
```

### Tests

```bash
# All tests:
pytest tests/ -v

# Router tests only:
pytest tests/ -v --ignore=tests/test_delegate_profile.py

# With coverage:
pytest --cov --cov-report=term tests/ -v
```

`register(ctx)` is called by Hermes at startup. It resolves the active profile
once, builds the handler via `_make_handler(current_profile, dispatch_delegate)`
(which captures a `dispatch_delegate` closure over `ctx.dispatch_tool`), and
registers the tool schema + `post_tool_call` hook.

## License

MIT
