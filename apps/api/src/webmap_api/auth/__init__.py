"""Authentication: verifying who is calling.

Authorization lives in `webmap_core.permissions` and the RLS policies, and
nothing in this package decides what anyone may do. The split matters: the
verifier is swappable per environment (`adr/0009-offline-identity-seam.md`),
and the permission model is not.
"""

from webmap_core.identity import TokenVerifier
from webmap_core.logging import get_logger
from webmap_core.settings import Settings

log = get_logger(__name__)


def build_verifier(settings: Settings) -> TokenVerifier:
    """Select the token verifier for this deployment.

    **Guard 3 of 4** (`adr/0009`): a non-production verifier announces itself
    at WARNING, naming the mode and environment. The other guards refuse the
    bad combination; this one makes an unexpected-but-permitted combination —
    a `local` deployment someone is treating as production, say — visible in
    the first ten lines of the log rather than never.
    """
    verifier: TokenVerifier
    if settings.auth_mode == "oidc":
        from webmap_api.auth.oidc import OidcTokenVerifier

        verifier = OidcTokenVerifier(settings)
        log.info("auth_mode", mode=verifier.mode, audience=settings.oidc_audience)
        return verifier

    from webmap_api.auth.dev import DevTokenVerifier

    verifier = DevTokenVerifier(settings)
    log.warning(
        "auth_mode_not_production",
        mode=verifier.mode,
        environment=settings.environment.value,
        detail=(
            "Tokens are verified against a key this process generated. Valid "
            "only for development; see adr/0009-offline-identity-seam.md."
        ),
    )
    return verifier


__all__ = ["TokenVerifier", "build_verifier"]
