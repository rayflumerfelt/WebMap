"""Domain services. Behind the API, never in an MCP tool handler.

`04-mcp-server.md` §1: "If you find yourself writing domain logic in a tool
handler, it belongs in webmap_core.services, behind the API."

Every function here takes a `Principal` and an `AsyncConnection` that already
carries the matching RLS context. The two are not interchangeable: RLS is the
backstop, the explicit `require()` call is where the good error messages come
from, and `03-auth-security.md` §3.1 asks for both.
"""
