"""StayOnDuty — the agent stays on duty.

External memory + task persistence for AI agents, so long-running work
never stalls silently.

Agent-facing surface (the YouTube play: agents use this automatically):
    from stayonduty import Client
    client = Client("work.db")
    with client.lease("my long task") as job:
        job.step("did a thing")
        job.remember("progress", 42)

Or as an MCP server any MCP-capable agent can discover on its own:
    python3 -m stayonduty.mcp_server --db work.db
"""
from .sdk import Client, Lease, QuotaWait

__version__ = "1.0.0"

__all__ = ["Client", "Lease", "QuotaWait", "__version__"]
