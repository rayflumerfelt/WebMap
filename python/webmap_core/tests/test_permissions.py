"""Exhaustive permission tests.

`CLAUDE.md` §6.1 sets the bar here: every visibility x grant x role
combination, not a sample. Permission bugs are invisible in review and the
failure mode is a colleague reading data they were never meant to see, so the
combinatorial sweep in `test_every_combination` is the point of this file and
the named cases below it are documentation of the interesting corners.
"""

import itertools
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from webmap_core.exceptions import PermissionDenied
from webmap_core.permissions import (
    Channel,
    Grant,
    GrantRole,
    Ownable,
    Permission,
    Principal,
    Visibility,
    require,
    require_owner,
    resolve_permission,
)

OWNER_ID = UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_ID = UUID("00000000-0000-0000-0000-0000000000b2")
OWNER_TEAM = UUID("00000000-0000-0000-0000-0000000000c3")
OTHER_TEAM = UUID("00000000-0000-0000-0000-0000000000d4")


@dataclass
class FakeDataset:
    id: UUID
    owner_user_id: UUID
    owner_team_id: UUID | None
    visibility: str
    name: str = "Wolfcamp A Porosity"


class FakeGrantStore:
    def __init__(self, grants: list[Grant]) -> None:
        self._grants = grants

    async def grants_for(self, obj: Ownable, principal: Principal) -> list[Grant]:
        return self._grants

    async def owner_display_name(self, obj: Ownable) -> str:
        return "Dana Reyes"


def principal(*, user: UUID = OTHER_ID, teams: frozenset[UUID] = frozenset()) -> Principal:
    return Principal(user_id=user, team_ids=teams, channel=Channel.WEB)


def dataset(visibility: Visibility, team: UUID | None = OWNER_TEAM) -> FakeDataset:
    return FakeDataset(
        id=uuid4(), owner_user_id=OWNER_ID, owner_team_id=team, visibility=visibility
    )


# --- The sweep --------------------------------------------------------------

GRANT_SHAPES: dict[str, Grant | None] = {
    "none": None,
    "user_viewer": Grant(GrantRole.VIEWER, grantee_user_id=OTHER_ID),
    "user_editor": Grant(GrantRole.EDITOR, grantee_user_id=OTHER_ID),
    "team_viewer": Grant(GrantRole.VIEWER, grantee_team_id=OTHER_TEAM),
    "team_editor": Grant(GrantRole.EDITOR, grantee_team_id=OTHER_TEAM),
    "irrelevant_user_editor": Grant(GrantRole.EDITOR, grantee_user_id=uuid4()),
    "irrelevant_team_editor": Grant(GrantRole.EDITOR, grantee_team_id=uuid4()),
}

MEMBERSHIPS: dict[str, frozenset[UUID]] = {
    "no_teams": frozenset(),
    "owner_team": frozenset({OWNER_TEAM}),
    "other_team": frozenset({OTHER_TEAM}),
    "both_teams": frozenset({OWNER_TEAM, OTHER_TEAM}),
}


def expected(visibility: Visibility, grant_name: str, membership_name: str) -> Permission:
    """The rule from `03-auth-security.md` §3.2, restated independently.

    Written out longhand rather than by calling the implementation, so the
    sweep compares two expressions of the rule rather than one against itself.
    """
    teams = MEMBERSHIPS[membership_name]
    grant = GRANT_SHAPES[grant_name]

    granted: GrantRole | None = None
    if grant is not None and (
        grant.grantee_user_id == OTHER_ID or grant.grantee_team_id in teams
    ):
        granted = grant.role

    if granted == GrantRole.EDITOR:
        return Permission.EDITOR

    floor = Permission.NONE
    if visibility == Visibility.ORG or (visibility == Visibility.TEAM and OWNER_TEAM in teams):
        floor = Permission.VIEWER

    if granted == GrantRole.VIEWER:
        floor = max(floor, Permission.VIEWER)
    return floor


@pytest.mark.parametrize(
    ("visibility", "grant_name", "membership_name"),
    list(itertools.product(list(Visibility), GRANT_SHAPES, MEMBERSHIPS)),
)
def test_every_combination(
    visibility: Visibility, grant_name: str, membership_name: str
) -> None:
    grant = GRANT_SHAPES[grant_name]
    actual = resolve_permission(
        principal(teams=MEMBERSHIPS[membership_name]),
        dataset(visibility),
        [] if grant is None else [grant],
    )

    assert actual == expected(visibility, grant_name, membership_name)


@pytest.mark.parametrize("visibility", list(Visibility))
@pytest.mark.parametrize("grant_name", list(GRANT_SHAPES))
def test_owner_always_wins(visibility: Visibility, grant_name: str) -> None:
    """Ownership short-circuits everything, including a viewer grant to self.

    An owner who was also granted 'viewer' by an earlier sharing action must
    not be demoted by it — grants widen, never narrow (`03` §3.3).
    """
    grant = GRANT_SHAPES[grant_name]

    actual = resolve_permission(
        principal(user=OWNER_ID),
        dataset(visibility),
        [] if grant is None else [grant],
    )

    assert actual == Permission.OWNER


# --- The corners worth naming ----------------------------------------------


def test_private_with_no_grant_is_invisible() -> None:
    """The Phase 1 acceptance criterion: A cannot read B's private dataset."""
    actual = resolve_permission(
        principal(teams=frozenset({OWNER_TEAM})), dataset(Visibility.PRIVATE), []
    )

    assert actual == Permission.NONE


def test_team_visibility_needs_membership_of_the_owning_team() -> None:
    """Being on *a* team is not being on *the* team."""
    actual = resolve_permission(
        principal(teams=frozenset({OTHER_TEAM})), dataset(Visibility.TEAM), []
    )

    assert actual == Permission.NONE


def test_team_visible_object_with_no_owning_team_is_private_in_effect() -> None:
    """owner_team_id is nullable. NULL must not match a principal's teams."""
    actual = resolve_permission(
        principal(teams=frozenset({OWNER_TEAM})),
        dataset(Visibility.TEAM, team=None),
        [],
    )

    assert actual == Permission.NONE


def test_grant_widens_beyond_visibility() -> None:
    """An editor grant on a private object reaches past the scope entirely."""
    actual = resolve_permission(
        principal(),
        dataset(Visibility.PRIVATE),
        [Grant(GrantRole.EDITOR, grantee_user_id=OTHER_ID)],
    )

    assert actual == Permission.EDITOR


def test_viewer_grant_does_not_narrow_org_visibility() -> None:
    """There is no deny grant. A viewer grant on an org object changes nothing."""
    actual = resolve_permission(
        principal(),
        dataset(Visibility.ORG),
        [Grant(GrantRole.VIEWER, grantee_user_id=OTHER_ID)],
    )

    assert actual == Permission.VIEWER


def test_editor_grant_beats_a_conflicting_viewer_grant() -> None:
    """Two grants can both apply. The wider one wins, per §3.3."""
    actual = resolve_permission(
        principal(teams=frozenset({OTHER_TEAM})),
        dataset(Visibility.PRIVATE),
        [
            Grant(GrantRole.VIEWER, grantee_user_id=OTHER_ID),
            Grant(GrantRole.EDITOR, grantee_team_id=OTHER_TEAM),
        ],
    )

    assert actual == Permission.EDITOR


def test_grant_must_name_exactly_one_grantee() -> None:
    with pytest.raises(ValueError, match="exactly one grantee"):
        Grant(GrantRole.VIEWER, grantee_user_id=OTHER_ID, grantee_team_id=OTHER_TEAM)

    with pytest.raises(ValueError, match="exactly one grantee"):
        Grant(GrantRole.VIEWER)


# --- Messages are the interface (CLAUDE.md §8) ------------------------------


async def test_denial_names_the_owner_and_the_gap() -> None:
    with pytest.raises(PermissionDenied) as excinfo:
        await require(
            FakeGrantStore([]),
            principal(),
            dataset(Visibility.PRIVATE),
            Permission.VIEWER,
        )

    message = str(excinfo.value)
    assert "Wolfcamp A Porosity" in message
    assert "Dana Reyes" in message
    assert "none access" in message
    assert "viewer is required" in message


async def test_editor_is_told_why_owner_only_means_owner_only() -> None:
    """Granting editor does not grant delete (`03` §3.3).

    An editor hitting a delete refusal needs to know it is the rule and not a
    bug, or they file a ticket.
    """
    store = FakeGrantStore([Grant(GrantRole.EDITOR, grantee_user_id=OTHER_ID)])

    with pytest.raises(PermissionDenied) as excinfo:
        await require_owner(store, principal(), dataset(Visibility.PRIVATE))

    assert "owner-only, even for editors" in str(excinfo.value)


async def test_require_passes_silently_when_permitted() -> None:
    await require(
        FakeGrantStore([]),
        principal(user=OWNER_ID),
        dataset(Visibility.PRIVATE),
        Permission.OWNER,
    )


def test_team_ids_literal_is_a_postgres_array() -> None:
    """Feeds `set_config('webmap.team_ids', ...)` — the RLS context (`03` §3.4)."""
    p = Principal(user_id=OTHER_ID, team_ids=frozenset({OWNER_TEAM}), channel=Channel.CLAUDE)

    assert p.team_ids_literal == "{" + str(OWNER_TEAM) + "}"


def test_empty_team_membership_yields_an_empty_array_not_a_broken_literal() -> None:
    """`{}` is a valid empty uuid[]. A bare `` would make every RLS check error."""
    assert principal().team_ids_literal == "{}"
