"""Request dependencies: identity in, scoped connection out.

The chain `03-auth-security.md` §5 calls the single most important control in
the document, expressed as FastAPI dependencies so a route cannot skip a link
by forgetting one — a route that wants data asks for `ScopedConn`, and there
is no way to obtain one without a verified principal.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_api.db.session import principal_session, unscoped_session
from webmap_core.identity import AuthenticationFailed, Claims, TokenVerifier
from webmap_core.logging import bind_request
from webmap_core.permissions import Channel, Principal
from webmap_core.services.directory import resolve_principal
from webmap_core.settings import Settings


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_verifier(request: Request) -> TokenVerifier:
    return request.app.state.verifier  # type: ignore[no-any-return]


def get_channel(
    x_webmap_channel: Annotated[str | None, Header()] = None,
) -> Channel:
    """Where the request came from. A *label*, never a credential.

    The local MCP server sets `X-WebMap-Channel: claude`, and it runs on the
    user's own machine — so anyone can set it to anything. It drives
    `audit_event.actor_channel` and nothing else. Authorization must never
    read it (`03-auth-security.md` §4.2).
    """
    return Channel.CLAUDE if x_webmap_channel == "claude" else Channel.WEB


async def get_claims(
    request: Request,
    verifier: Annotated[TokenVerifier, Depends(get_verifier)],
    authorization: Annotated[str | None, Header()] = None,
) -> Claims:
    """Verify the bearer token.

    The browser session cookie carries the same token, so an SPA request and
    an MCP request converge here rather than having two verification paths —
    one of which would eventually diverge.
    """
    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    else:
        settings: Settings = request.app.state.settings
        token = request.cookies.get(settings.session_cookie_name)

    if not token:
        raise AuthenticationFailed(
            "This endpoint requires authentication. Sign in at /auth/login, "
            "or send an access token as 'Authorization: Bearer <token>'."
        )
    return await verifier.verify(token)


async def get_principal(
    request: Request,
    claims: Annotated[Claims, Depends(get_claims)],
    channel: Annotated[Channel, Depends(get_channel)],
) -> Principal:
    """The authenticated actor, reconciled against the directory.

    Runs on an unscoped connection because it *establishes* the identity the
    scope is built from — `app_user` and `team` carry no RLS policies, and
    nothing here touches an ownable table.
    """
    async with unscoped_session(request.app.state.engine) as conn:
        principal = await resolve_principal(conn, claims, channel)

    bind_request(
        request_id=request.headers.get("X-Request-ID", ""),
        channel=channel.value,
        user_id=principal.user_id,
    )
    return principal


async def get_scoped_conn(
    request: Request, principal: Annotated[Principal, Depends(get_principal)]
) -> AsyncIterator[AsyncConnection]:
    """A connection carrying this principal's RLS context.

    Every route that reads or writes an ownable table takes this. Because the
    only way to get one is through `get_principal`, a route cannot
    accidentally query without a context — it would have to reach for the
    engine directly, which the `no-raw-db-session` pre-commit hook rejects.
    """
    async with principal_session(request.app.state.engine, principal) as conn:
        yield conn


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
ScopedConn = Annotated[AsyncConnection, Depends(get_scoped_conn)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]
