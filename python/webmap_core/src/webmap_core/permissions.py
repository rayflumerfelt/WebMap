"""Permission resolution. `03-auth-security.md` §3.

Two layers, both required (§3.1). This module is the *application* layer —
where the good error messages come from. The RLS policies in the migration
are the backstop that makes the failure mode "no rows" rather than "another
user's rows" when someone forgets to call `require`.

The resolution rule is deliberately a pure function over already-loaded
grants. `CLAUDE.md` §6.1 asks for exhaustive tests of every
visibility x grant x role combination, and that is only affordable if the
rule can be exercised without a database.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from webmap_core.exceptions import PermissionDenied


class Permission(IntEnum):
    """Ordered so comparisons work: OWNER > EDITOR > VIEWER > NONE."""

    NONE = 0
    VIEWER = 1
    EDITOR = 2
    OWNER = 3


class Visibility(StrEnum):
    PRIVATE = "private"
    TEAM = "team"
    ORG = "org"


class GrantRole(StrEnum):
    VIEWER = "viewer"
    EDITOR = "editor"


class Channel(StrEnum):
    """Where an action came from. Drives `audit_event.actor_channel`.

    'claude' is what makes "what did Claude do on my behalf" answerable —
    and with several users, whose Claude.
    """

    WEB = "web"
    CLAUDE = "claude"
    WORKER = "worker"


@dataclass(frozen=True)
class Principal:
    """The authenticated actor. Constructed once per request, never mutated."""

    user_id: UUID
    team_ids: frozenset[UUID]
    channel: Channel

    @property
    def team_ids_literal(self) -> str:
        """Postgres array literal for the RLS `webmap.team_ids` setting."""
        return "{" + ",".join(str(t) for t in sorted(self.team_ids, key=str)) + "}"


@runtime_checkable
class Ownable(Protocol):
    """The ownership block every ownable table repeats (`02` §3.3).

    Read-only members, so a frozen row satisfies it. That is not only a typing
    convenience: a permission check must never mutate the object it is
    deciding about, and declaring the protocol this way says so.
    """

    @property
    def id(self) -> UUID: ...

    @property
    def owner_user_id(self) -> UUID: ...

    @property
    def owner_team_id(self) -> UUID | None: ...

    @property
    def visibility(self) -> str: ...


@dataclass(frozen=True)
class Grant:
    """An explicit grant. Exactly one grantee, per the table CHECK constraint."""

    role: GrantRole
    grantee_user_id: UUID | None = None
    grantee_team_id: UUID | None = None

    def __post_init__(self) -> None:
        if (self.grantee_user_id is None) == (self.grantee_team_id is None):
            raise ValueError(
                "A grant names exactly one grantee — a user or a team, not both "
                "and not neither. This mirrors the num_nonnulls CHECK on "
                "access_grant."
            )

    def applies_to(self, principal: Principal) -> bool:
        if self.grantee_user_id is not None:
            return self.grantee_user_id == principal.user_id
        return self.grantee_team_id in principal.team_ids


class GrantStore(Protocol):
    """Loads grants and owner names. Implemented over the database by the API."""

    async def grants_for(self, obj: Ownable, principal: Principal) -> Sequence[Grant]: ...

    async def owner_display_name(self, obj: Ownable) -> str: ...


def resolve_permission(
    principal: Principal, obj: Ownable, grants: Iterable[Grant]
) -> Permission:
    """Effective permission for `principal` on `obj`. Pure.

    Grants widen access and never narrow it (`03` §3.3): there is no deny
    grant, so the result is the maximum of the visibility floor and anything
    granted explicitly. Ownership short-circuits both.
    """
    if obj.owner_user_id == principal.user_id:
        return Permission.OWNER

    applicable = [g.role for g in grants if g.applies_to(principal)]
    if GrantRole.EDITOR in applicable:
        return Permission.EDITOR

    base = Permission.NONE
    if obj.visibility == Visibility.ORG or (
        obj.visibility == Visibility.TEAM and obj.owner_team_id in principal.team_ids
    ):
        base = Permission.VIEWER

    if GrantRole.VIEWER in applicable:
        base = max(base, Permission.VIEWER)
    return base


async def effective_permission(
    store: GrantStore, principal: Principal, obj: Ownable
) -> Permission:
    return resolve_permission(principal, obj, await store.grants_for(obj, principal))


async def require(
    store: GrantStore, principal: Principal, obj: Ownable, level: Permission
) -> None:
    """Raise unless `principal` has at least `level` on `obj`.

    The message names the owner so the conversation can continue. Claude can
    then tell the geologist exactly who to ask, which turns a dead end into a
    next step (`03` §3.2).
    """
    actual = await effective_permission(store, principal, obj)
    if actual >= level:
        return

    name = getattr(obj, "name", None) or str(obj.id)
    raise PermissionDenied(
        f"You have {actual.name.lower()} access to '{name}' but "
        f"{level.name.lower()} is required. Ask "
        f"{await store.owner_display_name(obj)} to grant access."
    )


async def require_owner(store: GrantStore, principal: Principal, obj: Ownable) -> None:
    """Owner-only operations: delete, grant management, ownership transfer.

    Separate from `require(..., Permission.OWNER)` only for the message.
    'editor is not enough' is the confusing part for a user who was told they
    could edit the object, so say why explicitly (`03` §3.3).
    """
    if obj.owner_user_id == principal.user_id:
        return

    name = getattr(obj, "name", None) or str(obj.id)
    raise PermissionDenied(
        f"Only the owner of '{name}' can do this — deleting, sharing, and "
        f"transferring ownership are owner-only, even for editors. Ask "
        f"{await store.owner_display_name(obj)}."
    )
