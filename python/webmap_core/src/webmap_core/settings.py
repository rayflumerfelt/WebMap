"""Deployment configuration.

Secrets come from the environment, which in production is populated by the
corporate secret manager rather than by a file baked into the image
(`03-auth-security.md` §11). `.env` is for `local` only and is gitignored.

Two environments, not four (`01-architecture.md` §6): `local` is Docker
Compose on a workstation, `prod` is the internal server.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WEBMAP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    environment: Environment = Environment.LOCAL

    # --- Control plane ------------------------------------------------------
    # The application role. It must not have BYPASSRLS; `assert_rls_enforced`
    # checks that at startup and refuses to boot (`03` §3.5).
    database_url: str = "postgresql+asyncpg://webmap_app:webmap@localhost:5432/webmap"
    # Migrations run as a separate privileged role (`02` §4).
    migration_database_url: str = (
        "postgresql+psycopg://webmap_migrator:webmap@localhost:5432/webmap"
    )
    redis_url: str = "redis://localhost:6379/0"

    # --- Data plane ---------------------------------------------------------
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "webmap"
    s3_access_key: SecretStr = SecretStr("minioadmin")
    s3_secret_key: SecretStr = SecretStr("minioadmin")
    s3_region: str = "us-east-1"
    s3_use_ssl: bool = False

    # --- Public addressing --------------------------------------------------
    # Where a user's browser reaches the app. Session links are built from this
    # rather than from a request's Host header: the API sits behind a proxy,
    # and a Host-derived link hands out an internal hostname that resolves
    # nowhere useful once it has been pasted into a chat.
    public_base_url: str = "http://localhost:5173"

    # --- Internal services --------------------------------------------------
    titiler_url: str = "http://localhost:8001"
    render_url: str = "http://localhost:8002"
    # Glyphs and sprites for the render shell. Must be an allowlisted host in
    # `apps/render/security.py` or every label renders as a box.
    internal_static: str = "http://localhost:8000/static"

    # --- Identity -----------------------------------------------------------
    #
    # `oidc` is the production path. `dev` verifies tokens signed by a key
    # generated in-process, so the whole identity chain — claims, directory
    # sync, Principal, RLS context, require() — is exercisable without a
    # reachable IdP. See adr/0009-offline-identity-seam.md; the combination
    # `environment=prod` with `auth_mode=dev` is refused below.
    auth_mode: Literal["oidc", "dev"] = "dev"

    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    # Tokens must name this audience. A token issued for another service is
    # rejected rather than replayed here (`03` §4.4).
    oidc_audience: str = "api://webmap"
    api_scope: str = "api://webmap/.default"

    # Claim names differ by directory and by tenant configuration: Entra may
    # emit groups as `groups` or `roles`, and email as `email`,
    # `preferred_username`, or `upn`. These are the deployment-day unknowns
    # adr/0009 names, so they are configuration rather than constants.
    oidc_groups_claim: str = "groups"
    oidc_name_claim: str = "name"
    oidc_email_claim: str = "email"

    # Signs the browser session cookie. Rotating it logs everyone out, which
    # is the intended effect of a suspected compromise.
    session_secret: SecretStr = SecretStr("dev-only-not-a-secret")
    session_cookie_name: str = "webmap_session"
    # 03 §2: access token 15 minutes, refresh in an httpOnly cookie.
    access_token_ttl_seconds: int = Field(900, gt=0, le=3600)
    session_ttl_seconds: int = Field(28_800, gt=0)

    # --- Signing ------------------------------------------------------------
    # HMAC key for scoped tile tokens (`03` §6). Rotating it invalidates
    # outstanding tile URLs, which is a 15-minute inconvenience by design.
    tile_token_secret: SecretStr = SecretStr("dev-only-not-a-secret")
    tile_token_ttl_seconds: int = Field(900, gt=0, le=3600)
    render_token_ttl_seconds: int = Field(300, gt=0, le=900)

    # --- Retention ----------------------------------------------------------
    # 30 days, matching the soft-delete window in `03` §8 and the feature
    # version retention in `02` §3.5.1. They are the same number on purpose.
    soft_delete_days: int = 30
    feature_version_retention_days: int = 30

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    otel_endpoint: str | None = None

    @field_validator("database_url")
    @classmethod
    def _must_be_async(cls, value: str) -> str:
        """The API and worker are asyncio end to end.

        A sync driver here does not fail — it blocks the event loop under
        load, which shows up as unrelated timeouts weeks later.
        """
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                f"database_url must use the asyncpg driver "
                f"('postgresql+asyncpg://...'); got '{value.split('://')[0]}://'. "
                f"The sync driver belongs on migration_database_url."
            )
        return value

    @model_validator(mode="after")
    def _prod_requires_real_secrets(self) -> "Settings":
        """Refuse to start prod with the development defaults.

        A signing key that ships in the repository is not a signing key. This
        is the check that stops a copied `.env` from reaching the internal
        server unnoticed.
        """
        if self.environment is not Environment.PROD:
            return self

        # Guard 1 of 4 from adr/0009. The development verifier accepts tokens
        # this process signed for itself; in production that is an open door,
        # so the combination does not start. The other three guards are in
        # apps/api/auth/dev.py, the startup banner, and a test.
        if self.auth_mode != "oidc":
            raise ValueError(
                f"environment=prod with auth_mode={self.auth_mode!r}. The "
                f"development token verifier accepts tokens this process "
                f"signed for itself and must never run in production. Set "
                f"WEBMAP_AUTH_MODE=oidc. Refusing to start."
            )

        placeholders = {
            "tile_token_secret": self.tile_token_secret.get_secret_value()
            == "dev-only-not-a-secret",
            "session_secret": self.session_secret.get_secret_value() == "dev-only-not-a-secret",
            "s3_access_key": self.s3_access_key.get_secret_value() == "minioadmin",
            "s3_secret_key": self.s3_secret_key.get_secret_value() == "minioadmin",
            "oidc_client_id": not self.oidc_client_id,
            "oidc_discovery_url": not self.oidc_discovery_url,
        }
        unset = sorted(name for name, is_default in placeholders.items() if is_default)
        if unset:
            raise ValueError(
                f"environment=prod but these still hold development defaults: "
                f"{', '.join(unset)}. Set WEBMAP_{unset[0].upper()} and the rest "
                f"from the secret manager. Refusing to start."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so `.env` is read once.

    Call this rather than constructing `Settings()` — tests clear the cache
    via `get_settings.cache_clear()`.
    """
    return Settings()
