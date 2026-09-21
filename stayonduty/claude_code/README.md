# StayOnDuty × Claude Code

One command makes Claude Code use StayOnDuty automatically — the way videos
use YouTube. No per-session setup, no copy-pasted instructions.

```bash
# this project only (committed with the repo, team shares it)
python3 -m stayonduty.claude_code.install --project /path/to/project

# every project on this machine
python3 -m stayonduty.claude_code.install --user
```

## What it installs

1. **MCP server `stayonduty`** — 17 tools over stdio, zero dependencies.
   Project scope: `.mcp.json` at the project root. User scope: `~/.claude.json`
   under the top-level `mcpServers` key. (MCP servers in `settings.json` are
   silently ignored by Claude Code — the installer writes to the right files.)
2. **SessionStart hook** — injects the one-sentence standing instruction into
   every session (start / resume / clear / compact), so the agent reaches for
   `stayonduty_register_task` on its own for long tasks.
3. **Stop hook** — when the agent tries to stop with StayOnDuty tasks still
   in progress on live leases, the stop is blocked and the agent is told to
   finish or park them properly. Bounded: 3 blocks per session, then it stands
   down and the watchdog owns recovery. It never traps *you*: a missing DB,
   no live work, or an exhausted budget all let the stop through.

Existing settings are backed up to `.bak` before modification. Re-running
the installer changes nothing when already installed.

```bash
# MCP server only, no hooks
python3 -m stayonduty.claude_code.install --project . --no-hooks

# remove everything install added
python3 -m stayonduty.claude_code.install --project . --uninstall
```

## Verify it

```bash
claude mcp list            # stayonduty should show Connected
```

Then start a session in the project and check `/mcp` — the `stayonduty_*`
tools are there, and the session already knows the standing instruction
(the SessionStart hook injected it). Give it a long task and watch it
register, heartbeat, checkpoint, and verify acceptance on complete. Try to
end the session mid-task: the Stop hook makes the agent finish or park the
work first.

## How the agent uses it

```python
# the pattern the SessionStart hook teaches (via MCP tools):
# 1. stayonduty_register_task(title, acceptance=[...]) -> task_id
# 2. stayonduty_claim_task(task_id, owner) + heartbeat loop
# 3. stayonduty_step / stayonduty_remember as work progresses
# 4. stayonduty_complete -> acceptance verified against the real artifact
```

The DB lives at `<project>/.stayonduty/work.db` (project scope) or
`~/.stayonduty/work.db` (user scope); override with `--db`. Point the
dashboard at it for the passive, pull-only view:

```bash
python3 -m stayonduty.dashboard /path/to/work.db
```
