# delegate-profile

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds a
`delegate_profile` tool for spawning subagents under a **different** Hermes profile.

The built-in `delegate_task` always runs children under the *same* profile as the
parent. This plugin lets you pick any profile per subagent so the child inherits
that profile's config, skills, memories, and model.

```text
delegate_profile(goal="Review this PR for security issues", profile="reviewer")
delegate_profile(goal="Implement feature X",              profile="coder")
delegate_profile(goal="Summarize these papers",            profile="researcher-a", model="anthropic/claude-sonnet-4")
```

## What it does

- Adds a **`delegate_profile`** tool to the `delegation` toolset.
- For **cross-profile** calls, spawns a one-shot
  `hermes -p <profile> chat -q "<goal>"` subprocess. The child runs fully
  isolated under the target profile — its own session, skills, memory, and model.
- For **same-profile** calls (profile omitted or matching the current profile),
  transparently falls back to the in-process `delegate_task` so you pay no
  subprocess overhead.
- Registers a **`post_tool_call` hook** that logs a warning if `delegate_task`
  is ever called with a `profile` argument — a nudge to use `delegate_profile`
  instead, since the built-in tool would silently ignore the parameter.

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

### Parallel batch under different profiles

Call `delegate_profile` multiple times — each runs as an independent
subprocess under its own profile:

```python
delegate_profile(goal="Draft Q3 investor update",   profile="writer")
delegate_profile(goal="Audit last week's deployments", profile="sre")
delegate_profile(goal="Summarize support tickets",   profile="ops")
```

### Parameters

| Parameter  | Type    | Required | Default | Notes |
|------------|---------|----------|---------|-------|
| `goal`     | string  | yes      | —       | What the subagent should accomplish. Be self-contained — the child has no context from your session. |
| `profile`  | string  | yes      | —       | Target Hermes profile name. Must exist (`hermes profile list`). If it matches the current profile, falls back to inline `delegate_task`. |
| `context`  | string  | no       | —       | Background info: file paths, error messages, project structure, constraints. |
| `model`    | string  | no       | profile default | Model override passed as `-m` to the child. |
| `timeout`  | integer | no       | `300`   | Max seconds to wait for the subprocess. |

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

For missing required args, returns `{"error": "goal is required"}` or
`{"error": "profile is required"}`.

## How it differs from `delegate_task`

|                         | `delegate_task`                         | `delegate_profile`                          |
|-------------------------|-----------------------------------------|---------------------------------------------|
| Profile                 | Same as parent (always)                 | Any profile (`profile` is required)         |
| Mechanism               | In-process `ThreadPoolExecutor`         | `hermes -p <profile>` subprocess            |
| Overhead                | Lower                                   | Slightly higher (process spawn + cold start) |
| Isolation               | Separate conversation, shared process   | Fully isolated process + profile            |
| Child capabilities      | Inherits parent's config/skills/model   | Inherits **target** profile's config/skills/memories/model |
| Best for                | Parallel subtasks in the same session   | Specialized work needing a different persona or model |

Rule of thumb: if you'd be happy running the subagent with the *current*
profile's settings, use `delegate_task` (faster). If you need the subagent to
behave like a *different* profile — different SOUL, skills, memory, or default
model — use `delegate_profile`.

## How it works

1. Resolves the `hermes` binary (prefers the one in the current venv, falls
   back to `PATH`).
2. Builds the prompt as `Context: <context>\n\nTask: <goal>` when `context`
   is provided, otherwise just `goal`.
3. Spawns `hermes -p <profile> chat -q "<prompt>"` (plus `-m <model>` when set)
   with `capture_output=True` and the given `timeout`.
4. Forwards `HERMES_HOME` so the child resolves profiles from the same place
   as the parent, and sets `HERMES_DELEGATE_PROFILE_DISABLE=1` to prevent
   recursive delegation inside the child.
5. Returns a JSON envelope (see [Result format](#result-format)).

The `post_tool_call` hook (`_on_post_tool_call`) is a no-op for every tool
except `delegate_task`. When `delegate_task` is invoked with a `profile`
parameter, it emits a `logger.warning` pointing you at `delegate_profile`.
The hook never blocks or modifies the call — it's purely advisory.

## Configuration

No config file is required. Behavior can be influenced via environment
variables:

| Variable                          | Default      | Effect |
|-----------------------------------|--------------|--------|
| `HERMES_PROFILE`                  | `default`    | Used to detect same-profile calls (inline fallback). |
| `HERMES_HOME`                     | `~/.hermes`  | Forwarded to the child so profiles resolve consistently. |
| `HERMES_DELEGATE_PROFILE_DISABLE` | unset        | Set to `1` inside spawned children to prevent recursive delegation. |

## Development

```bash
git clone https://github.com/rodrigogs/hermes-delegate-profile.git
cd hermes-delegate-profile
```

Plugin layout:

```
hermes-delegate-profile/
├── plugin.yaml     # manifest (name, version, provides_tools, provides_hooks)
├── __init__.py     # register() — registers the tool + post_tool_call hook
└── README.md       # this file
```

`register(ctx)` is called by Hermes at startup. It calls
`ctx.register_tool(...)` with the `delegate_profile` schema and handler, and
`ctx.register_hook("post_tool_call", _on_post_tool_call)`.

## License

MIT
