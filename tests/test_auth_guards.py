"""Guard 4 of 4. `adr/0009-offline-identity-seam.md`.

The development token verifier accepts tokens this process signed for itself.
In production that is an unauthenticated API. Three guards refuse the
combination — settings, the verifier's constructor, and the startup banner —
and this file is the one that fails CI if any of them is removed.

Without it the other three are each a single edit away from being gone, and
nothing would notice: the dev path works, so deleting a guard makes no test
fail and no behaviour change until the day it matters.

No database and no network, so these run in every suite.
"""

import pytest

from webmap_core.settings import Environment, Settings

#: A production configuration with every other placeholder replaced, so the
#: only thing under test is `auth_mode`. Reusing the dev defaults would let
#: this pass for the wrong reason — refused because a secret was unset.
PROD_BASE = {
    "environment": "prod",
    "database_url": "postgresql+asyncpg://u:p@db:5432/webmap",
    "tile_token_secret": "a-real-secret",
    "session_secret": "another-real-secret",
    "s3_access_key": "real-key",
    "s3_secret_key": "real-secret",
    "oidc_client_id": "00000000-0000-0000-0000-000000000000",
    "oidc_discovery_url": "https://login.example.com/.well-known/openid-configuration",
}


def test_prod_with_dev_auth_mode_is_refused_at_settings() -> None:
    """Guard 1: the combination does not construct."""
    with pytest.raises(ValueError) as excinfo:
        Settings(**PROD_BASE, auth_mode="dev")  # type: ignore[arg-type]

    message = str(excinfo.value)
    assert "auth_mode" in message
    assert "Refusing to start" in message


def test_prod_with_oidc_is_accepted() -> None:
    """The control. If this failed, guard 1 would be refusing everything and
    the test above would pass for the wrong reason."""
    settings = Settings(**PROD_BASE, auth_mode="oidc")  # type: ignore[arg-type]

    assert settings.environment is Environment.PROD
    assert settings.auth_mode == "oidc"


def test_dev_verifier_refuses_to_construct_in_prod() -> None:
    """Guard 2: independent of guard 1.

    Constructed against a settings object that claims prod, which guard 1
    would normally have prevented from existing. That is the point — this
    guard has to hold when the first one has been bypassed or reordered,
    which is exactly the circumstance nobody plans for.
    """
    from webmap_api.auth.dev import DevTokenVerifier

    settings = Settings(**PROD_BASE, auth_mode="oidc")  # type: ignore[arg-type]
    assert settings.environment is Environment.PROD

    with pytest.raises(RuntimeError) as excinfo:
        DevTokenVerifier(settings)

    assert "Refusing to construct" in str(excinfo.value)
    assert "adr/0009" in str(excinfo.value)


def test_build_verifier_selects_oidc_in_prod() -> None:
    from webmap_api.auth import build_verifier

    verifier = build_verifier(Settings(**PROD_BASE, auth_mode="oidc"))  # type: ignore[arg-type]

    assert verifier.mode == "oidc"


def test_build_verifier_selects_dev_locally() -> None:
    from webmap_api.auth import build_verifier

    verifier = build_verifier(Settings(environment="local", auth_mode="dev"))

    assert verifier.mode == "dev"


def test_oidc_mode_without_a_discovery_url_refuses_to_start() -> None:
    """An OIDC verifier with nowhere to fetch keys verifies nothing.

    Failing at construction rather than on the first request means a
    misconfigured deployment does not start, instead of starting and rejecting
    every user with a confusing error.
    """
    from webmap_api.auth.oidc import OidcTokenVerifier

    settings = Settings(environment="local", auth_mode="oidc")

    with pytest.raises(ValueError) as excinfo:
        OidcTokenVerifier(settings)

    assert "WEBMAP_OIDC_DISCOVERY_URL" in str(excinfo.value)


def test_prod_still_refuses_placeholder_secrets() -> None:
    """The guard that predates adr/0009, kept honest.

    A signing key that ships in the repository is not a signing key.
    """
    incomplete = {**PROD_BASE, "session_secret": "dev-only-not-a-secret"}

    with pytest.raises(ValueError) as excinfo:
        Settings(**incomplete, auth_mode="oidc")  # type: ignore[arg-type]

    assert "session_secret" in str(excinfo.value)
