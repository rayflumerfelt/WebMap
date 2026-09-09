"""MCP server skeleton. `04-mcp-server.md` §3.

Transport is **stdio** — the Claude client launches this as a subprocess on
the user's workstation (`adr/0008-local-stdio-mcp.md`). Tool names carry the
`webmap_` prefix because this server runs alongside others and generic names
collide.

The twenty tools land in Phases 1-4 as the API endpoints behind them appear.
What is here now is the transport, the instructions block, and the
authenticated client — the parts every tool depends on.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import MCPServer

from webmap_mcp import __version__
from webmap_mcp.settings import McpSettings

INSTRUCTIONS = (
    "WebMap is a geospatial mapping system for subsurface geology. Use it to "
    "find spatial datasets, interpolate scattered point data into gridded "
    "surfaces (honoring geological faults), derive contours, and render maps "
    "for presentations.\n\n"
    "Typical flow: find a dataset with webmap_search_datasets, inspect it with "
    "webmap_describe_dataset, grid it with webmap_interpolate, then render with "
    "webmap_render_map. Renders return structured metadata — use it to write "
    "accurate figure captions rather than describing the image.\n\n"
    "When the user wants to edit data rather than view it, use "
    "webmap_open_session and give them the link."
)

# `04-mcp-server.md` §3 names this class FastMCP, which is what the Python
# SDK called it through 1.x. It is `MCPServer` from 2.0 onward — the same
# framework, renamed. Pinning mcp<2 to match the spelling in the spec would
# hold the server on a superseded major for a rename.
mcp = MCPServer(name="webmap", version=__version__, instructions=INSTRUCTIONS)
settings = McpSettings()


async def acquire_token(scopes: list[str]) -> str:
    """The logged-in user's own token, from the OS credential broker.

    With Entra ID, MSAL's Windows broker (WAM) obtains it silently for the
    logged-in Windows account. With on-prem AD, `httpx-gssapi` over SSPI
    obtains a Kerberos ticket for the API's SPN. Either way there is no
    prompt, no stored password, and nothing here that the user does not
    already have.

    **There is no fallback and no service account.** If acquisition fails,
    the tool call fails (`03-auth-security.md` §4).
    """
    raise NotImplementedError(
        "Token acquisition is Phase 1 (12-roadmap.md). Which broker to wire "
        "depends on whether the directory is Entra ID or pure on-prem AD — "
        "confirm in week 1; see adr/0007-multi-user-directory-sso.md."
    )


@asynccontextmanager
async def api() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client authenticated as the logged-in Windows user.

    Every permission decision happens on the other end of this client. Do not
    add one here — this process runs where the user can edit it, so a check
    here is theatre, and worse, it invites someone to assume the API is
    already protected.
    """
    token = await acquire_token([settings.api_scope])
    async with httpx.AsyncClient(
        base_url=settings.api_base_url,
        timeout=settings.request_timeout_seconds,
        headers={
            "Authorization": f"Bearer {token}",
            # Drives actor_channel in the audit log. A label, not a
            # credential — the API must never authorize on it.
            "X-WebMap-Channel": "claude",
        },
    ) as client:
        yield client


def main() -> None:
    """Console entry point. Registered as `webmap-mcp` in pyproject."""
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
