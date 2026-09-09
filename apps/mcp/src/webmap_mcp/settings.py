"""Configuration for the local MCP server.

Its own module rather than `webmap_core.settings`: importing webmap_core would
drag SQLAlchemy and the whole domain layer onto the user's workstation, which
is exactly what `adr/0008-local-stdio-mcp.md` says this process must not
carry. An import-linter contract enforces it.
"""

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    # Environment only — deliberately no `env_file`. This process is launched
    # by the Claude client, so its working directory is whatever that client
    # happened to be in; reading a `.env` from there means the server picks
    # up a stray file on one machine and not another. Configuration is fixed
    # at install time (`03-auth-security.md` §4.5), which in practice is the
    # env block of the Claude client's MCP config.
    model_config = SettingsConfigDict(env_prefix="WEBMAP_MCP_", extra="forbid")

    # Fixed at install time, never switchable at runtime
    # (`03-auth-security.md` §4.5). A development install points at localhost;
    # a production install points at the internal server. A tool call that
    # deletes a dataset does not care which environment it landed in, and this
    # is the one component sitting on a machine where both configurations are
    # plausible.
    api_base_url: str = "http://localhost:8000"
    api_scope: str = "api://webmap/.default"
    request_timeout_seconds: float = Field(60.0, gt=0)

    # `broker` is the production path: MSAL/WAM against Entra, or SSPI against
    # on-prem AD. `dev` asks the API for a token and only works when the API
    # is itself in development mode, so the decision is enforced there rather
    # than here (`03` §4.2).
    auth_mode: Literal["broker", "dev"] = "dev"

    # Broker configuration. Supplied by the packaged installer.
    client_id: str = ""
    authority: str = ""

    #: Which fictional user the development token source signs in as. Only
    #: meaningful with auth_mode=dev.
    dev_user: str = "ada"

    @model_validator(mode="after")
    def _broker_needs_its_configuration(self) -> "McpSettings":
        """Fail at construction rather than on the first tool call.

        A misconfigured install that starts and then fails every call looks
        like WebMap being broken. One that refuses to start names the missing
        setting.
        """
        if self.auth_mode != "broker":
            return self
        missing = [
            name
            for name, value in (
                ("WEBMAP_MCP_CLIENT_ID", self.client_id),
                ("WEBMAP_MCP_AUTHORITY", self.authority),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"auth_mode=broker needs {' and '.join(missing)}. These come "
                f"from the WebMap app registration in the corporate directory "
                f"and are set by the packaged installer. Refusing to start "
                f"with a credential broker that cannot reach a tenant."
            )
        return self

    @model_validator(mode="after")
    def _https_outside_localhost(self) -> "McpSettings":
        """A token must not travel in clear text to anything but localhost.

        The broker hands this process the user's own bearer token; sending it
        over plain HTTP to an internal hostname puts it on the wire for
        anything on the network segment to read.
        """
        url = self.api_base_url
        if url.startswith("https://"):
            return self
        host = url.removeprefix("http://").split("/")[0].split(":")[0]
        if host in {"localhost", "127.0.0.1", "::1"}:
            return self
        raise ValueError(
            f"api_base_url is {url!r}. A token acquired for this user would be "
            f"sent in clear text to a remote host. Use https:// for anything "
            f"other than localhost."
        )
