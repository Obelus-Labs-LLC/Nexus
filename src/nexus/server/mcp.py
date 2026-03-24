"""MCP stdio server for Nexus — thin orchestrator.

Tools are split across modules:
  tools_index.py   — nexus_scan, nexus_read, nexus_symbols, nexus_register_edit
  tools_query.py   — nexus_start, nexus_retrieve, nexus_stats, nexus_analytics
  tools_refactor.py — nexus_rename, nexus_enrich, nexus_cross_project, nexus_remember

Shared state lives in state.py.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nexus.server")

mcp = FastMCP("nexus")

# Load parser plugins at module import time
from nexus.index.plugins import load_all_plugins

_plugins_loaded = load_all_plugins()

# Register all tools
from nexus.server import tools_index, tools_query, tools_refactor

tools_index.register(mcp)
tools_query.register(mcp)
tools_refactor.register(mcp)

logger.info("Nexus MCP server initialized with %d plugins", _plugins_loaded)


async def run_stdio():
    """Run the MCP server over stdio."""
    await mcp.run_stdio_async()
