"""Authentication routes. `03-auth-security.md` §2.

Authorization code + PKCE against the corporate IdP, plus the development
sign-in that `adr/0009-offline-identity-seam.md` introduces.

**The OIDC half of this file is the one piece of Phase 1 that cannot be tested
before deployment.** Everything downstream of it is exercised offline through
the dev path, so the deployment-day risk is confined to: does the discovery
document resolve, does the code exchange succeed, and do the claim names match
what `Settings` expects. Those are the three things to check first when
connecting a real tenant.
"""

import base64
import hashlib
import secrets
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from webmap_api.db.session import unscoped_session
from webmap_api.dependencies import AppSettings, CurrentPrincipal, ScopedConn
from webmap_core.identity import AuthenticationFailed, Claims
from webmap_core.logging import get_logger
from webmap_core.permissions import Channel
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.directory import sync_user_from_claims
from webmap_core.settings import Environment, Settings

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

#: The PKCE verifier has to survive the round trip to the IdP. A signed,
#: short-lived cookie rather than server-side state: the API is stateless
#: (`01-architecture.md` §2.1), and parking login state in Redis would make
#: sign-in depend on the queue being up.
_FLOW_COOKIE = "webmap_login_flow"
_FLOW_TTL_SECONDS = 600


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        settings.session_secret.get_secret_value(), salt="webmap-login-flow"
    )


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    """Store the access token in an httpOnly cookie.

    httpOnly so no script can read it, `SameSite=Lax` so it does not ride
    along on cross-site requests, and `Secure` outside local development —
    local is plain HTTP, and a Secure cookie there is simply never sent.
    """
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.environment is Environment.PROD,
        path="/",
    )


async def _establish_session(
    request: Request, claims: Claims, token: str, settings: Settings
) -> JSONResponse:
    """Reconcile the directory, audit the login, and set the cookie."""
    async with unscoped_session(request.app.state.engine) as conn:
        synced = await sync_user_from_claims(conn, claims)
        await record(
            conn,
            action=AuditAction.LOGIN_SUCCEEDED,
            actor_user_id=synced.user_id,
            actor_channel=Channel.WEB.value,
            detail={"email": claims.email, "first_login": synced.created},
            ip_address=request.client.host if request.client else None,
        )
        # Membership changes are their own event: they silently alter what
        # this person can see, and a login record alone would not show it.
        if synced.teams_added or synced.teams_removed:
            await record(
                conn,
                action=AuditAction.TEAMS_CHANGED,
                actor_user_id=synced.user_id,
                actor_channel=Channel.WEB.value,
                detail={
                    "added": list(synced.teams_added),
                    "removed": list(synced.teams_removed),
                },
            )

    response = JSONResponse(
        {
            "user_id": str(synced.user_id),
            "display_name": synced.display_name,
            "email": claims.email,
            "teams": sorted(claims.groups),
            "first_login": synced.created,
        }
    )
    _set_session_cookie(response, settings, token)
    return response


# --- OIDC (production) ------------------------------------------------------


@router.get("/login")
async def login(
    request: Request,
    settings: AppSettings,
    redirect_uri: Annotated[str | None, Query()] = None,
) -> Response:
    """Begin sign-in.

    In `oidc` mode this redirects to the IdP with PKCE. In `dev` mode it
    returns the roster, because there is nowhere to redirect to.
    """
    if settings.auth_mode != "oidc":
        from webmap_api.auth.dev import DEV_USERS

        return JSONResponse(
            {
                "auth_mode": "dev",
                "detail": (
                    "Development sign-in. POST /auth/dev/login?user=<key> to "
                    "sign in as one of these, or POST /auth/dev/token for a "
                    "bearer token."
                ),
                "users": {
                    key: {"name": u.display_name, "email": u.email, "groups": list(u.groups)}
                    for key, u in DEV_USERS.items()
                },
            }
        )

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)
    callback = redirect_uri or str(request.url_for("auth_callback"))

    metadata = await _discovery(settings)
    params = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "redirect_uri": callback,
        # `groups` is requested explicitly: team membership comes from it, and
        # a tenant that does not emit it produces users with no teams and no
        # error (`03-auth-security.md` §2).
        "scope": "openid profile email groups",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(
        f"{metadata['authorization_endpoint']}?{urlencode(params)}", status_code=302
    )
    response.set_cookie(
        _FLOW_COOKIE,
        _serializer(settings).dumps({"v": verifier, "s": state, "r": callback}),
        max_age=_FLOW_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.environment is Environment.PROD,
        path="/auth",
    )
    return response


@router.get("/callback", name="auth_callback")
async def callback(
    request: Request,
    settings: AppSettings,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
) -> Response:
    """Complete sign-in: exchange the code, verify the token, set the cookie."""
    if error:
        # The IdP's own words. Paraphrasing them loses the detail an
        # administrator needs — consent required, user blocked, and so on.
        raise AuthenticationFailed(
            f"The identity provider refused the sign-in: {error}"
            f"{f' — {error_description}' if error_description else ''}."
        )
    if not code or not state:
        raise AuthenticationFailed(
            "The sign-in callback arrived without an authorization code. "
            "Start again at /auth/login."
        )

    raw_flow = request.cookies.get(_FLOW_COOKIE)
    if not raw_flow:
        raise AuthenticationFailed(
            "The sign-in flow expired or the browser dropped its cookie. "
            "Start again at /auth/login."
        )
    try:
        flow: dict[str, str] = _serializer(settings).loads(raw_flow, max_age=_FLOW_TTL_SECONDS)
    except BadSignature as exc:
        raise AuthenticationFailed(
            "The sign-in flow could not be verified. Start again at /auth/login."
        ) from exc

    # CSRF: the state we issued must come back. Compared in constant time
    # because it is a secret for the length of the flow.
    if not secrets.compare_digest(flow["s"], state):
        raise AuthenticationFailed(
            "The sign-in state did not match. This can happen if two sign-ins "
            "were started in the same browser; start again at /auth/login."
        )

    metadata = await _discovery(settings)
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_response = await client.post(
            str(metadata["token_endpoint"]),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": flow["r"],
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret.get_secret_value(),
                "code_verifier": flow["v"],
            },
        )
    if token_response.status_code != 200:
        log.warning("token_exchange_failed", status=token_response.status_code)
        raise AuthenticationFailed(
            "The identity provider rejected the authorization code. This is "
            "usually a redirect-URI or client-secret mismatch in the app "
            "registration rather than anything the user did."
        )

    payload = token_response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise AuthenticationFailed(
            "The identity provider returned no access token. Check that the "
            "app registration requests the WebMap API scope."
        )

    claims = await request.app.state.verifier.verify(access_token)
    response = await _establish_session(request, claims, access_token, settings)
    response.delete_cookie(_FLOW_COOKIE, path="/auth")
    return response


async def _discovery(settings: Settings) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(settings.oidc_discovery_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AuthenticationFailed(
                f"Could not reach the identity provider at "
                f"{settings.oidc_discovery_url}. This is an outage or a "
                f"network policy problem, not a credential problem."
            ) from exc
    return dict(response.json())


# --- Development sign-in ----------------------------------------------------


def _dev_only(settings: Settings) -> None:
    if settings.auth_mode != "dev":
        raise AuthenticationFailed(
            "Development sign-in is disabled: this deployment verifies tokens "
            "against the corporate directory. Use /auth/login."
        )


@router.post("/dev/login")
async def dev_login(
    request: Request,
    settings: AppSettings,
    user: Annotated[str, Query(description="Roster key: ada, grace, or alan")],
) -> Response:
    """Sign in as a fictional user and set the session cookie.

    Takes the same path a real sign-in does from `_establish_session` onward —
    directory reconcile, audit event, cookie — so what is exercised here is
    the production code.
    """
    _dev_only(settings)
    from webmap_api.auth.dev import DEV_USERS

    dev_user = DEV_USERS.get(user)
    if dev_user is None:
        raise AuthenticationFailed(
            f"No development user '{user}'. Available: {', '.join(sorted(DEV_USERS))}."
        )

    token = request.app.state.verifier.issue(dev_user)
    claims = await request.app.state.verifier.verify(token)
    return await _establish_session(request, claims, token, settings)


@router.post("/dev/token")
async def dev_token(
    request: Request,
    settings: AppSettings,
    user: Annotated[str, Query(description="Roster key: ada, grace, or alan")],
) -> dict[str, Any]:
    """Mint a bearer token for a fictional user.

    For tests and for pointing the local MCP server at the local API without
    a credential broker. The token is signed by this process and dies with it.
    """
    _dev_only(settings)
    from webmap_api.auth.dev import DEV_USERS

    dev_user = DEV_USERS.get(user)
    if dev_user is None:
        raise AuthenticationFailed(
            f"No development user '{user}'. Available: {', '.join(sorted(DEV_USERS))}."
        )
    return {
        "access_token": request.app.state.verifier.issue(dev_user),
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_seconds,
        "user": {"email": dev_user.email, "groups": list(dev_user.groups)},
    }


# --- Session ----------------------------------------------------------------


@router.get("/me")
async def me(principal: CurrentPrincipal, conn: ScopedConn) -> dict[str, Any]:
    """Who am I, and what teams am I in.

    The SPA calls this on load. It runs through the full dependency chain, so
    a 200 here means identity, directory sync, and the RLS context all work —
    which makes it the fastest way to confirm a new deployment's IdP wiring.
    """
    from sqlalchemy import text

    result = await conn.execute(
        text(
            """
            SELECT u.id, u.email, u.display_name,
                   coalesce(
                     array_agg(t.slug ORDER BY t.slug)
                       FILTER (WHERE t.slug IS NOT NULL),
                     '{}') AS teams
            FROM app_user u
            LEFT JOIN team_member m ON m.user_id = u.id
            LEFT JOIN team t ON t.id = m.team_id
            WHERE u.id = :id
            GROUP BY u.id, u.email, u.display_name
            """
        ),
        {"id": principal.user_id},
    )
    row = result.one()
    return {
        "user_id": str(row.id),
        "email": row.email,
        "display_name": row.display_name,
        "teams": list(row.teams),
        # From the token, so it reflects the live grant of membership rather
        # than the reconciled cache. They should agree; when they do not, the
        # token is authoritative and the cache is stale.
        "effective_team_ids": sorted(str(t) for t in principal.team_ids),
        "channel": principal.channel.value,
    }


@router.post("/logout")
async def logout(settings: AppSettings) -> Response:
    response = JSONResponse({"status": "signed out"})
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response


__all__ = ["router"]
