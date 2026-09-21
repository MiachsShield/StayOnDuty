"""StayOnDuty integration for Claude Code.

One command wires everything:

    python3 -m stayonduty.claude_code.install --project /path/to/project

That registers the MCP server (14 tools, stdio, zero dependencies),
installs a SessionStart hook that teaches every session the one-sentence
standing instruction, and installs a Stop hook that refuses to let the
agent quit while StayOnDuty tasks are still in progress with live leases.
See README.md in this directory.
"""
