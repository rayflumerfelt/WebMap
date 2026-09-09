"""The development token verifier. `adr/0009-offline-identity-seam.md`.

Verifies tokens signed by a key generated in this process and held only in
memory. That is the whole security model: a token minted here is worthless in
any other process, cannot outlive the one that issued it, and cannot be
replayed against a real deployment.

It exists so the identity chain — claims, directory sync, `Principal`, RLS
context, `require()` — can be exercised without a reachable directory. It is
**not** an authorization bypass: everything downstream of `verify()` is the
same code production runs. What changes is who you are proved to be, never
what you may do.

**Guard 2 of 4.** `Settings` refuses `environment=prod` with `auth_mode=dev`
before the app starts; this module refuses again at construction, in case that
check is ever bypassed or reordered. Guard 3 is the startup banner, guard 4 is
`tests/test_auth_guards.py`.
"""

import secrets
import time
from dataclasses import dataclass

import jwt

from webmap_core.identity import AuthenticationFailed, Claims, claims_from_mapping
from webmap_core.logging import get_logger
from webmap_core.settings import Environment, Settings

log = get_logger(__name__)

#: Matches the production audience check, so a token that would be rejected in
#: prod for the wrong audience is rejected here too. The dev path should fail
#: the same way the real one does, or it teaches the wrong lessons.
_ALGORITHM = "HS256"


@dataclass(frozen=True)
class DevUser:
    """A fictional user the dev login can sign in as."""

    subject: str
    email: str
    display_name: str
    groups: tuple[str, ...]


#: A roster with the shape the Phase 1 acceptance criteria need: two users on
#: the same team, one on another, so "User A cannot read User B's private
#: dataset" and "team visibility works" are both demonstrable by hand against
#: the local stack, not only in tests.
DEV_USERS: dict[str, DevUser] = {
    "ada": DevUser(
        subject="dev|ada",
        email="ada.lovelace@webmap.local",
        display_name="Ada Lovelace",
        groups=("seed-group-permian",),
    ),
    "grace": DevUser(
        subject="dev|grace",
        email="grace.hopper@webmap.local",
        display_name="Grace Hopper",
        groups=("seed-group-permian",),
    ),
    "alan": DevUser(
        subject="dev|alan",
        email="alan.turing@webmap.local",
        display_name="Alan Turing",
        groups=("seed-group-exploration",),
    ),
}


class DevTokenVerifier:
    """Issues and verifies tokens signed with a per-process key."""

    mode = "dev"

    def __init__(self, settings: Settings) -> None:
        if settings.environment is Environment.PROD:
            raise RuntimeError(
                "Refusing to construct the development token verifier with "
                "environment=prod. It accepts tokens this process signed for "
                "itself, which in production is an unauthenticated API. This "
                "is the second of four guards in "
                "adr/0009-offline-identity-seam.md; if you reached it, the "
                "first one was bypassed and that is worth understanding "
                "before going further."
            )
        self._settings = settings
        # Never written to disk, never configurable. Making this settable
        # would let a deployment pin a known key, which is the failure this
        # design exists to make impossible.
        self._key = secrets.token_bytes(32)
        log.warning(
            "dev_token_verifier_active",
            environment=settings.environment.value,
            detail="Tokens are signed by this process and accepted only by it.",
        )

    def issue(self, user: DevUser, *, ttl_seconds: int | None = None) -> str:
        """Mint a token for a fictional user.

        Carries identity and nothing else — no role, no scope, no permission
        (`03-auth-security.md` §4.4). If a privilege claim ever appears here,
        the dev path has stopped mirroring production.
        """
        now = int(time.time())
        ttl = (
            ttl_seconds if ttl_seconds is not None else self._settings.access_token_ttl_seconds
        )
        payload = {
            "sub": user.subject,
            self._settings.oidc_email_claim: user.email,
            self._settings.oidc_name_claim: user.display_name,
            self._settings.oidc_groups_claim: list(user.groups),
            "aud": self._settings.oidc_audience,
            "iss": "webmap-dev",
            "iat": now,
            "exp": now + ttl,
        }
        return jwt.encode(payload, self._key, algorithm=_ALGORITHM)

    async def verify(self, token: str) -> Claims:
        try:
            payload = jwt.decode(
                token,
                self._key,
                algorithms=[_ALGORITHM],
                audience=self._settings.oidc_audience,
                issuer="webmap-dev",
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            log.warning("dev_token_rejected", reason=type(exc).__name__)
            raise AuthenticationFailed(
                "The access token was rejected. In development this usually "
                "means the API restarted — the signing key lives only in "
                "memory, so tokens do not survive a reload. Sign in again."
            ) from exc

        return claims_from_mapping(
            payload,
            groups_claim=self._settings.oidc_groups_claim,
            name_claim=self._settings.oidc_name_claim,
            email_claim=self._settings.oidc_email_claim,
        )
