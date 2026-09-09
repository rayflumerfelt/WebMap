"""Configuration for the local MCP server.

Its own module rather than `webmap_core.settings`: importing webmap_core
would drag SQLAlchemy and the whole domain layer onto the user's workstation,
which is exactly what `adr/0008-local-stdio-mcp.md` says this process must
not carry. An import-linter contract enforces it.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEBMAP_MCP_", env_file=".env", extra="forbid")

    # Fixed at install time, never switchable at runtime
    # (`03-auth-security.md` §4.5). A development install points at
    # localhost; a production install points at the internal server. A tool
    # call that deletes a dataset does not care which environment it landed
    # in, and this is the one component sitting on a machine where both
    # configurations are plausible.
    api_base_url: str = "http://localhost:8000"
    api_scope: str = "api://webmap/.default"
    request_timeout_seconds: float = Field(60.0, gt=0)
