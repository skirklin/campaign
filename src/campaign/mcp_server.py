"""Expose the campaign tools over MCP, so Claude Code or Claude Desktop can be the assistant.

    campaign mcp --campaign curse-of-strahd

The MCP client's own model does the reasoning; this server only reads and
writes the campaign database.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .agent import ASK_SYSTEM, _role_parts, overview
from .db import Store
from .tools import ToolContext, build_tools


def serve(store: Store, campaign_slug: str, read_only: bool = False) -> None:
    campaign = store.campaign(campaign_slug)
    server = MCPServer(
        name=f"campaign-{campaign.slug}",
        instructions=ASK_SYSTEM.format(
            name=campaign.name, overview=overview(store, campaign), **_role_parts(campaign)
        ),
    )
    for fn in build_tools(ToolContext(store, campaign), writable=not read_only):
        server.add_tool(fn)
    server.run("stdio")
