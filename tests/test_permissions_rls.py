"""Row-level security, tested against a real Postgres.

`12-roadmap.md` Phase 1: *"User A cannot read User B's private dataset —
verified at both application and RLS layer."* Two layers, and the point of
having two is that either one alone would be a single point of failure
(`03-auth-security.md` §3.1).

So every case here is asserted twice:

- **`raw_visible_dataset_ids`** issues `SELECT id FROM dataset WHERE id = ...`
  directly under the principal's RLS context, with no service function
  anywhere in the call. If the application check were the only thing working,
  this half would return the row and the test would fail.
- **the service call** goes through `load_and_require`, which is where the
  message that names the owner comes from.

`pytest.mark.integration` because these need the stack up; they skip with
instructions when it is not.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import insert_dataset, raw_visible_dataset_ids
from webmap_api.db.session import principal_session
from webmap_core.exceptions import NotFound, PermissionDenied
from webmap_core.permissions import GrantRole, Permission, Principal
from webmap_core.services import datasets as service
from webmap_core.services.grants import create_grant, revoke_grant
from webmap_core.services.ownable import load_and_require, load_ownable

pytestmark = pytest.mark.integration


async def _make_dataset(
    engine: AsyncEngine,
    owner: Principal,
    people: dict[str, UUID],
    visibility: str,
    name: str = "Wolfcamp A Porosity Picks",
) -> UUID:
    async with principal_session(engine, owner) as conn:
        return await insert_dataset(
            conn,
            owner_id=people["owner"],
            team_id=people["owner_team"],
            visibility=visibility,
            name=name,
        )


# --- The criterion ----------------------------------------------------------


async def test_private_dataset_is_invisible_to_another_user_at_both_layers(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """The Phase 1 acceptance criterion, stated as directly as possible."""
    dataset_id = await _make_dataset(engine, principals["owner"], people, "private")

    # Database layer: a bare SELECT under the outsider's context.
    assert await raw_visible_dataset_ids(engine, principals["outsider"], dataset_id) == []

    # Application layer: the service refuses, and says something useful.
    async with principal_session(engine, principals["outsider"]) as conn:
        with pytest.raises(NotFound) as excinfo:
            await service.get_dataset(conn, principals["outsider"], dataset_id)
    assert "you can access" in str(excinfo.value)

    # And the owner is unaffected — a test that passes because nothing works
    # is not a test.
    assert await raw_visible_dataset_ids(engine, principals["owner"], dataset_id) == [
        dataset_id
    ]


async def test_private_dataset_is_invisible_even_to_a_teammate(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """`private` means owner-only, not team-only.

    The teammate shares the owning team, so this separates the two mechanisms:
    if the read policy were checking team membership regardless of visibility,
    everything else here would still pass.
    """
    dataset_id = await _make_dataset(engine, principals["owner"], people, "private")

    assert await raw_visible_dataset_ids(engine, principals["teammate"], dataset_id) == []


async def test_team_visibility_admits_the_team_and_nobody_else(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    dataset_id = await _make_dataset(engine, principals["owner"], people, "team")

    assert await raw_visible_dataset_ids(engine, principals["teammate"], dataset_id) == [
        dataset_id
    ]
    assert await raw_visible_dataset_ids(engine, principals["outsider"], dataset_id) == []
    # On no team at all — a different code path through the policy than being
    # on the wrong team, because `owner_team_id = ANY('{}')` is not NULL logic.
    assert await raw_visible_dataset_ids(engine, principals["stranger"], dataset_id) == []


async def test_org_visibility_admits_everyone_authenticated(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    dataset_id = await _make_dataset(engine, principals["owner"], people, "org")

    for key in ("teammate", "outsider", "stranger"):
        assert await raw_visible_dataset_ids(engine, principals[key], dataset_id) == [
            dataset_id
        ], f"{key} should see an org-visible dataset"


# --- Grants -----------------------------------------------------------------


async def test_a_grant_makes_a_private_dataset_visible_at_both_layers(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """Grants widen access past the visibility scope (`03` §3.3)."""
    dataset_id = await _make_dataset(engine, principals["owner"], people, "private")
    assert await raw_visible_dataset_ids(engine, principals["outsider"], dataset_id) == []

    async with principal_session(engine, principals["owner"]) as conn:
        obj = await load_ownable(conn, "dataset", dataset_id)
        await create_grant(
            conn,
            principals["owner"],
            obj,
            "dataset",
            GrantRole.VIEWER,
            grantee_user_id=people["outsider"],
        )

    assert await raw_visible_dataset_ids(engine, principals["outsider"], dataset_id) == [
        dataset_id
    ]
    async with principal_session(engine, principals["outsider"]) as conn:
        detail = await service.get_dataset(conn, principals["outsider"], dataset_id)
    assert detail["id"] == dataset_id


async def test_revoking_a_grant_takes_effect_immediately(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """Permissions resolve per request against the live model (`03` §4.4).

    Nothing is cached in the token, so revocation is visible on the next query
    rather than at token expiry.
    """
    dataset_id = await _make_dataset(engine, principals["owner"], people, "private")

    async with principal_session(engine, principals["owner"]) as conn:
        obj = await load_ownable(conn, "dataset", dataset_id)
        grant_id = await create_grant(
            conn,
            principals["owner"],
            obj,
            "dataset",
            GrantRole.VIEWER,
            grantee_user_id=people["outsider"],
        )
    assert await raw_visible_dataset_ids(engine, principals["outsider"], dataset_id) != []

    async with principal_session(engine, principals["owner"]) as conn:
        obj = await load_ownable(conn, "dataset", dataset_id)
        await revoke_grant(conn, principals["owner"], obj, "dataset", grant_id)

    assert await raw_visible_dataset_ids(engine, principals["outsider"], dataset_id) == []


async def test_a_viewer_grant_does_not_confer_edit(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """Visible but not editable is the case that produces the good message.

    RLS lets the outsider see the row, so the application layer is what
    refuses — and it names the owner, which is the whole reason `03` §3.2
    insists on two layers rather than relying on RLS alone.
    """
    dataset_id = await _make_dataset(engine, principals["owner"], people, "private")
    async with principal_session(engine, principals["owner"]) as conn:
        obj = await load_ownable(conn, "dataset", dataset_id)
        await create_grant(
            conn,
            principals["owner"],
            obj,
            "dataset",
            GrantRole.VIEWER,
            grantee_user_id=people["outsider"],
        )

    async with principal_session(engine, principals["outsider"]) as conn:
        with pytest.raises(PermissionDenied) as excinfo:
            await load_and_require(
                conn, "dataset", dataset_id, principals["outsider"], Permission.EDITOR
            )

    message = str(excinfo.value)
    assert "Olive Owner" in message, "the message must name who to ask"
    assert "viewer access" in message and "editor is required" in message


async def test_an_editor_grant_does_not_confer_delete(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """Delete is owner-only even for editors (`03` §3.3)."""
    dataset_id = await _make_dataset(engine, principals["owner"], people, "team")
    async with principal_session(engine, principals["owner"]) as conn:
        obj = await load_ownable(conn, "dataset", dataset_id)
        await create_grant(
            conn,
            principals["owner"],
            obj,
            "dataset",
            GrantRole.EDITOR,
            grantee_user_id=people["teammate"],
        )

    async with principal_session(engine, principals["teammate"]) as conn:
        with pytest.raises(PermissionDenied) as excinfo:
            await service.soft_delete_dataset(conn, principals["teammate"], dataset_id)

    assert "owner-only, even for editors" in str(excinfo.value)


async def test_only_the_owner_may_grant(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """The escalation this rule exists to stop: a viewer granting themselves
    editor (`03-auth-security.md` §1, privilege escalation via grants)."""
    dataset_id = await _make_dataset(engine, principals["owner"], people, "team")

    async with principal_session(engine, principals["teammate"]) as conn:
        obj = await load_ownable(conn, "dataset", dataset_id)
        with pytest.raises(PermissionDenied):
            await create_grant(
                conn,
                principals["teammate"],
                obj,
                "dataset",
                GrantRole.EDITOR,
                grantee_user_id=people["teammate"],
            )

    assert await raw_visible_dataset_ids(engine, principals["outsider"], dataset_id) == []


# --- Write policies ---------------------------------------------------------


async def test_a_user_cannot_create_a_dataset_owned_by_someone_else(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """The INSERT policy's WITH CHECK, which `02` §4 originally omitted.

    Without it there is no INSERT policy at all and every insert is denied;
    with it but without the owner check, a request could plant a row owned by
    a third party.
    """
    from sqlalchemy.exc import DBAPIError

    async with principal_session(engine, principals["outsider"]) as conn:
        with pytest.raises(DBAPIError):
            await insert_dataset(
                conn,
                owner_id=people["owner"],  # not the acting principal
                team_id=people["owner_team"],
                visibility="team",
            )


async def test_an_editor_cannot_reassign_ownership(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """The UPDATE policy's WITH CHECK.

    Without it Postgres reuses the USING clause for the new row, and an
    editor can rewrite `owner_user_id` to a third party while keeping the row
    visible — quietly transferring ownership of someone else's data.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    dataset_id = await _make_dataset(engine, principals["owner"], people, "team")
    async with principal_session(engine, principals["owner"]) as conn:
        obj = await load_ownable(conn, "dataset", dataset_id)
        await create_grant(
            conn,
            principals["owner"],
            obj,
            "dataset",
            GrantRole.EDITOR,
            grantee_user_id=people["teammate"],
        )

    async with principal_session(engine, principals["teammate"]) as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(
                text("UPDATE dataset SET owner_user_id = :new WHERE id = :id"),
                {"new": people["outsider"], "id": dataset_id},
            )


async def test_delete_policy_is_owner_only_at_the_database(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """Even with an editor grant, a hard DELETE matches no rows.

    The service uses soft delete, so this asserts the policy directly — the
    backstop for a future code path that issues a real DELETE.
    """
    from sqlalchemy import text

    dataset_id = await _make_dataset(engine, principals["owner"], people, "team")
    async with principal_session(engine, principals["owner"]) as conn:
        obj = await load_ownable(conn, "dataset", dataset_id)
        await create_grant(
            conn,
            principals["owner"],
            obj,
            "dataset",
            GrantRole.EDITOR,
            grantee_user_id=people["teammate"],
        )

    async with principal_session(engine, principals["teammate"]) as conn:
        result = await conn.execute(
            text("DELETE FROM dataset WHERE id = :id"), {"id": dataset_id}
        )
        assert result.rowcount == 0

    assert await raw_visible_dataset_ids(engine, principals["owner"], dataset_id) == [
        dataset_id
    ]


# --- The context itself -----------------------------------------------------


async def test_a_query_without_an_rls_context_is_refused_loudly(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """A query with no principal fails; it never returns rows.

    `02` §4 calls `current_setting` without the missing_ok flag so that a
    missing context raises rather than quietly matching nothing — the
    difference between a loud bug and an empty list that reads as "you have no
    datasets".

    Which error you get depends on the connection, and that is worth stating
    because it looks like an inconsistency:

    - On a **fresh** connection the parameter has never been set, and Postgres
      raises `unrecognized configuration parameter`.
    - On a **pooled** connection that previously served a request, the
      transaction-local `set_config` has reverted the value to empty rather
      than undefining the parameter, so the cast to uuid fails instead.

    Both are errors and neither returns a row, which is the property that
    matters. Asserting on one specific message would make this test pass or
    fail depending on pool reuse, and the "fix" for that flake would be
    missing_ok — which would replace both errors with silent zero rows.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    dataset_id = await _make_dataset(engine, principals["owner"], people, "org")

    async with engine.begin() as conn:  # deliberately not principal_session
        with pytest.raises(DBAPIError):
            await conn.execute(text("SELECT id FROM dataset"))

    # The dataset is org-visible, so it is not the row being unreadable —
    # it is the missing context.
    assert await raw_visible_dataset_ids(engine, principals["stranger"], dataset_id) == [
        dataset_id
    ]


async def test_rls_context_does_not_leak_between_pooled_connections(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """`set_config(..., true)` is transaction-local. This is why.

    With the flag omitted the setting persists on the pooled connection, and
    the next request to borrow it inherits the previous user's identity. The
    pool is small enough here that the same connection is very likely reused,
    which is exactly the condition that would expose the bug.
    """
    dataset_id = await _make_dataset(engine, principals["owner"], people, "private")

    for _ in range(5):
        assert await raw_visible_dataset_ids(engine, principals["owner"], dataset_id) == [
            dataset_id
        ]
        assert await raw_visible_dataset_ids(engine, principals["outsider"], dataset_id) == []


async def test_soft_deleted_datasets_disappear_from_reads_but_survive(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """Delete is soft, and lineage must still resolve it (`03` §8).

    The row stays and stays visible to RLS — the filtering is the service's
    job, which is what lets a lineage record still name it.
    """
    dataset_id = await _make_dataset(engine, principals["owner"], people, "team")
    async with principal_session(engine, principals["owner"]) as conn:
        await service.soft_delete_dataset(conn, principals["owner"], dataset_id)

    async with principal_session(engine, principals["owner"]) as conn:
        page = await service.list_datasets(conn, principals["owner"])
        assert dataset_id not in [item["id"] for item in page.items]
        with pytest.raises(NotFound):
            await service.get_dataset(conn, principals["owner"], dataset_id)

    # Still there, and still reachable when deleted rows are asked for.
    assert await raw_visible_dataset_ids(engine, principals["owner"], dataset_id) == [
        dataset_id
    ]
    async with principal_session(engine, principals["owner"]) as conn:
        row = await load_ownable(conn, "dataset", dataset_id, include_deleted=True)
    assert row.deleted_at is not None


async def test_unknown_dataset_id_reports_not_found(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """An id that does not exist and one hidden by RLS report identically.

    Deliberate: distinguishing them would confirm the existence of objects the
    caller has no access to.
    """
    async with principal_session(engine, principals["owner"]) as conn:
        with pytest.raises(NotFound):
            await service.get_dataset(conn, principals["owner"], uuid4())


async def test_the_ownership_block_names_the_sanctioned_path(
    engine: AsyncEngine, people: dict[str, UUID], principals: dict[str, Principal]
) -> None:
    """CLAUDE.md §8: the message says what to do next.

    Someone hitting this is implementing transfer. The error has to point at
    the door rather than reading as an unexplained database refusal.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    dataset_id = await _make_dataset(engine, principals["owner"], people, "team")

    async with principal_session(engine, principals["owner"]) as conn:
        with pytest.raises(DBAPIError) as excinfo:
            await conn.execute(
                text("UPDATE dataset SET owner_user_id = :new WHERE id = :id"),
                {"new": people["teammate"], "id": dataset_id},
            )

    message = str(excinfo.value)
    assert "explicit, audited operation" in message
    assert "webmap.allow_ownership_transfer" in message
