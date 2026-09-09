"""The WebMap MCP server. Runs locally, over stdio, trusted with nothing.

Launched as a subprocess by Claude Code or Claude Desktop on the geologist's
own workstation. It is a thin HTTP client of `webmap-api`, authenticating as
the logged-in Windows user through the OS credential broker.

It holds **no database connection, no service credential, and no
authorization logic**. A local MCP process that enforces a permission check
is a bug, not a defence — the user owns that process
(`03-auth-security.md` §4.2).
"""

__version__ = "0.1.0"
