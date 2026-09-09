"""Acquiring the user's own token. `03-auth-security.md` §4.3.

The production path is the OS credential broker: MSAL's Windows broker (WAM)
against Entra ID, or SSPI/Kerberos against on-prem AD. Either way the
geologist sees nothing — they open Claude and the tools work — and
`webmap-api` receives a verifiable assertion of who is calling.

**There is no fallback and no service account.** If acquisition fails, the
tool call fails. This process runs on the user's own machine, where they can
read and modify it, so anything it held would be theirs already; the only
credential it ever touches is the user's own, and only for as long as a call
takes.

The broker is unreachable from a development machine, so the same seam
`adr/0009-offline-identity-seam.md` puts in the API appears here: a protocol
with a broker implementation and a development one. The development source
asks the API for a token, and the API only issues one when *it* is running in
development mode — so the guard that matters lives at the API, where it is
enforced against everyone, rather than here on a machine the user controls.
"""

from typing import Any, Protocol

import httpx

from webmap_mcp.settings import McpSettings


class TokenAcquisitionFailed(Exception):
    """No token could be obtained. The tool call fails; nothing falls back."""


class TokenSource(Protocol):
    """Produces a bearer token for the logged-in user."""

    @property
    def mode(self) -> str: ...

    async def token(self) -> str: ...


class BrokerTokenSource:
    """MSAL's Windows broker. Silent, for the logged-in Windows account.

    Not exercised in development — there is no directory to reach — so this is
    the one part of the MCP server whose first real run is on a workstation.
    What that costs is bounded: everything downstream of `token()` is shared
    with the development path and is tested, so the untested surface is the
    acquisition call itself.
    """

    mode = "broker"

    def __init__(self, settings: McpSettings) -> None:
        self._settings = settings
        # Untyped: msal ships no stubs, and the alternative is a hand-written
        # Protocol for three methods we call once each.
        self._app: Any = None

    async def token(self) -> str:
        try:
            import msal
        except ImportError as exc:
            raise TokenAcquisitionFailed(
                "The Windows credential broker is not available: msal is not "
                "installed. Install the MCP server with its 'broker' extra "
                "(uv tool install 'webmap-mcp[broker]'), which is what the "
                "packaged installer does."
            ) from exc

        if self._app is None:
            self._app = msal.PublicClientApplication(
                self._settings.client_id,
                authority=self._settings.authority,
                enable_broker_on_windows=True,
            )

        app = self._app
        scopes = [self._settings.api_scope]
        accounts = app.get_accounts()
        result = None
        if accounts:
            result = app.acquire_token_silent(scopes, account=accounts[0])
        if not result:
            # Interactive, but through the broker, so for a domain-joined
            # machine it resolves against the logged-in account without a
            # prompt. `03` §4.3: no prompt, no stored password.
            result = app.acquire_token_interactive(
                scopes,
                parent_window_handle=app.CONSOLE_WINDOW_HANDLE,
            )

        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description", "no detail given")
            raise TokenAcquisitionFailed(
                f"Could not obtain a token for {self._settings.api_scope} from "
                f"the Windows credential broker: {detail}. This is a sign-in "
                f"problem on this workstation, not a WebMap permission "
                f"problem — check that you are signed in to Windows with your "
                f"corporate account."
            )
        return str(result["access_token"])


class DevTokenSource:
    """Asks the API for a development token. Offline-capable.

    Deliberately thin, and deliberately not self-authorising: it calls
    `/auth/dev/token`, which the API serves only when *it* is in development
    mode. The decision about whether development tokens exist belongs to the
    API, which enforces it for every caller, rather than to a process running
    on a machine the user controls (`03-auth-security.md` §4.2).
    """

    mode = "dev"

    def __init__(self, settings: McpSettings) -> None:
        self._settings = settings

    async def token(self) -> str:
        user = self._settings.dev_user
        async with httpx.AsyncClient(
            base_url=self._settings.api_base_url,
            timeout=self._settings.request_timeout_seconds,
        ) as client:
            try:
                response = await client.post("/auth/dev/token", params={"user": user})
            except httpx.HTTPError as exc:
                raise TokenAcquisitionFailed(
                    f"Could not reach WebMap at {self._settings.api_base_url}. "
                    f"Start the local stack with "
                    f"`docker compose -f infra/compose.yaml up -d`."
                ) from exc

        if response.status_code != 200:
            raise TokenAcquisitionFailed(
                f"WebMap refused a development token for '{user}' "
                f"(HTTP {response.status_code}). If this deployment "
                f"authenticates against the corporate directory, development "
                f"tokens are disabled and this MCP server should be installed "
                f"with WEBMAP_MCP_AUTH_MODE=broker."
            )
        return str(response.json()["access_token"])


def build_token_source(settings: McpSettings) -> TokenSource:
    """Select the token source for this install.

    Fixed at install time along with the API base URL (`03` §4.5). A tool call
    that deletes a dataset does not care which environment it landed in, and
    this is the one component sitting on a machine where both configurations
    are plausible.
    """
    if settings.auth_mode == "broker":
        return BrokerTokenSource(settings)
    return DevTokenSource(settings)
