# delegate-profile

Hermes Agent plugin: extends `delegate_task` with **profile selection** — spawn subagents under different Hermes profiles.

## What it does

The built-in `delegate_task` always runs children under the **same** Hermes profile as the parent. This plugin adds a `delegate_profile` tool that lets you pick a **different** profile per subagent:

```
delegate_profile(goal="Review this code", profile="reviewer")
delegate_profile(goal="Implement feature X", profile="coder")
```

## How it works

- **Cross-profile**: spawns `hermes -p <profile> chat -q "<goal>"` as a one-shot subprocess. The child inherits the target profile's config, skills, memories, and model.
- **Same-profile**: falls back to inline `delegate_task` for efficiency (no subprocess overhead).
- **Hook**: registers a `post_tool_call` hook that warns when `delegate_task` is called with a `profile` param (nudge toward using the right tool).

## Install

```bash
hermes plugins install rodrigogs/hermes-delegate-profile
hermes plugins enable delegate-profile
```

Or manually:

```bash
git clone https://github.com/rodrigogs/hermes-delegate-profile.git \
  ~/.hermes/plugins/delegate-profile
hermes plugins enable delegate-profile
```

## Usage

```
delegate_profile(
  goal="What the subagent should accomplish",
  profile="reviewer",    # required — Hermes profile name
  context="Optional background info",
  model="Optional model override",
  timeout=300            # max seconds (default 300)
)
```

The target profile must exist. Use `hermes profile list` to see available profiles.

## Requirements

- Hermes Agent (any recent version)
- One or more additional Hermes profiles (`hermes profile create <name>`)
- The `delegation` toolset must be enabled

## How it differs from delegate_task

| | `delegate_task` | `delegate_profile` |
|---|---|---|
| Profile | Same as parent | Any profile (required param) |
| Mechanism | ThreadPoolExecutor (in-process) | `hermes -p` subprocess |
| Speed | Faster | Slightly slower (process spawn) |
| Isolation | Shared process, isolated context | Full process + profile isolation |
| Use case | Parallel work in same session | Specialized agent with different config |

## Development

```bash
git clone https://github.com/rodrigogs/hermes-delegate-profile.git
cd hermes-delegate-profile
# Plugin structure:
#   plugin.yaml     — manifest
#   __init__.py     — register() + tool handler
#   README.md       — this file
```

## License

MIT
