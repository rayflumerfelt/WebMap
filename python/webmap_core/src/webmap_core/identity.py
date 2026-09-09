"""Identity: claims, the token-verification seam, and directory sync.

`03-auth-security.md` §2 and §5. The chain this module sits in the middle of:

    token -> Claims -> app_user row + team_member reconcile -> Principal
          -> principal_session (RLS context) -> require() in a service

Verification is behind a protocol so it can be satisfied offline during
development (`adr/0009-offline-identity-seam.md`). Everything after
`verify()` is identical in both modes — there is one permission model, one set
of RLS policies, one `require()`. Which verifier is active changes who you are
proved to be, never what you may do.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from webmap_core.exceptions import WebMapError


class AuthenticationFailed(WebMapError):
    """A token was absent, malformed, expired, or not for us.

    Deliberately one exception for every cause. Distinguishing "expired" from
    "bad signature" in a response tells an attacker which half of a forged
    token to fix; the detail belongs in the log, not the body.
    """


@dataclass(frozen=True)
class Claims:
    """The verified assertion about who is calling.

    Only what this system uses. A token carries far more, and copying it all
    into the domain invites someone to authorize on a field the IdP does not
    guarantee.
    """

    subject: str
    """OIDC `sub`. The stable identifier; `app_user.subject` is unique on it.

    Not the email: people are renamed and re-married, and an email-keyed user
    becomes a second user with none of their datasets.
    """

    email: str
    display_name: str
    groups: tuple[str, ...] = ()
    """Directory group identifiers, matched against `team.idp_group_id`.

    Whether these arrive as object ids or display names is IdP configuration
    and differs between Entra and on-prem AD. `team.idp_group_id` is TEXT for
    exactly that reason — see `adr/0007-multi-user-directory-sso.md`.
    """

    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    """The full verified claim set, for audit and debugging. Never authorize
    on this — anything load-bearing gets a field above."""


class TokenVerifier(Protocol):
    """Verifies a bearer token and returns claims, or raises.

    The single seam between "a string arrived in a header" and "we know who
    this is". Implementations must verify signature, issuer, expiry, and
    audience; a verifier that skips audience lets a token minted for another
    service be replayed here (`03-auth-security.md` §4.4).
    """

    @property
    def mode(self) -> str:
        """Short name for logs and the startup banner: 'oidc', 'dev', ..."""
        ...

    async def verify(self, token: str) -> Claims: ...


@dataclass(frozen=True)
class SyncedIdentity:
    """The result of reconciling claims against the directory tables."""

    user_id: UUID
    team_ids: frozenset[UUID]
    display_name: str
    created: bool
    """True when this login created the `app_user` row. Drives the audit event
    and lets the caller distinguish a first login from a returning one."""

    teams_added: tuple[str, ...] = ()
    teams_removed: tuple[str, ...] = ()
    """Group slugs whose membership changed on this login. The directory is
    authoritative (`03` §2), so a user removed from a group upstream loses
    team access here without manual cleanup — and that is worth an audit trail
    because it silently changes what they can see."""


def normalise_groups(groups: Sequence[str] | None) -> tuple[str, ...]:
    """Trim, drop empties, de-duplicate, and order group identifiers.

    Ordering matters only so that logs and audit records compare cleanly
    between logins; the set semantics are what authorization uses.
    """
    if not groups:
        return ()
    seen = {g.strip() for g in groups if g and g.strip()}
    return tuple(sorted(seen))


def claims_from_mapping(
    payload: dict[str, Any],
    *,
    groups_claim: str = "groups",
    name_claim: str = "name",
    email_claim: str = "email",
) -> Claims:
    """Build `Claims` from a verified token payload.

    The claim *names* are configurable because they genuinely differ: Entra ID
    may emit `groups` or `roles` depending on tenant configuration, and email
    may arrive as `email`, `preferred_username`, or `upn`. Getting this wrong
    is a deployment-day configuration problem, so it is configuration.

    A missing `sub` is fatal — it is the only claim this system keys on.
    """
    subject = payload.get("sub")
    if not subject:
        raise AuthenticationFailed(
            "The identity token carries no 'sub' claim, so there is no stable "
            "identifier to key this user on. Check the IdP's token "
            "configuration; WebMap keys users on 'sub' rather than email "
            "because email changes and 'sub' does not."
        )

    email = payload.get(email_claim) or payload.get("preferred_username") or payload.get("upn")
    if not email:
        raise AuthenticationFailed(
            f"The identity token carries no '{email_claim}' claim (nor "
            f"'preferred_username' or 'upn'). WebMap needs an email to "
            f"identify the user in the UI and in permission messages. Add the "
            f"claim to the application's token configuration in the IdP."
        )

    raw_groups = payload.get(groups_claim)
    if isinstance(raw_groups, str):
        # Some IdPs emit a single group as a bare string rather than a list.
        raw_groups = [raw_groups]

    return Claims(
        subject=str(subject),
        email=str(email),
        display_name=str(payload.get(name_claim) or email),
        groups=normalise_groups(raw_groups),
        raw=payload,
    )
