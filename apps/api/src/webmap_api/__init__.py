"""The WebMap control plane.

Owns the database, registers datasets, manages styles and sessions, enqueues
jobs, and proxies tile requests. Stateless.

Does *not* do heavy computation — any operation that can exceed 2 seconds is
enqueued (`01-architecture.md` §2.1) — and contains no geoprocessing
algorithms; it orchestrates `webmap_geo` (§3.1).

**This is the sole enforcement point for authorization.** The MCP server runs
on the user's workstation and is trusted with nothing
(`adr/0008-local-stdio-mcp.md`).
"""

__version__ = "0.1.0"
