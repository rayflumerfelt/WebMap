"""The `adr/0010` schema: layers, basemaps, roles and the preference tiers.

`02-data-model.md` §3.7a and §3.8 specified these and nothing created them —
five of the twenty-one tables that document defines had never existed. This
covers the four properties of the migration that are easy to get wrong and
invisible when they are.

The tests are behavioural rather than structural where they can be. Asserting
that a constraint exists proves it was written; asserting that it *refuses*
proves it works.

Needs Postgres. Skips with instructions when absent.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


async def as_principal(conn: object, user: UUID) -> None:
    """Set the RLS context for this transaction.

    **`set_config(..., true)` — transaction-local.** Without the `true` the
    setting outlives the transaction on a pooled connection and leaks into the
    next principal's queries (`CLAUDE.md` §3.2).

    Needed even on the migration connection: these tables carry `FORCE ROW
    LEVEL SECURITY`, so the role that owns them is not exempt. The first run of
    this file failed with `unrecognized configuration parameter
    "webmap.user_id"`, which is the policy working rather than a fixture bug.
    """
    await conn.execute(  # type: ignore[attr-defined]
        text(
            "SELECT set_config('webmap.user_id', :u, true), "
            "set_config('webmap.team_ids', '{}', true)"
        ),
        {"u": str(user)},
    )


async def a_user(engine: AsyncEngine, name: str = "owner") -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO app_user (subject, email, display_name) "
                            "VALUES (:s, :e, :n) RETURNING id"
                        ),
                        {
                            "s": f"dev|{name}-{uuid4().hex[:8]}",
                            "e": f"{name}@x.test",
                            "n": name,
                        },
                    )
                ).scalar_one()
            )
        )


async def a_dataset(engine: AsyncEngine, owner: UUID) -> UUID:
    async with engine.begin() as conn:
        await as_principal(conn, owner)
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            # `vector_has_parquet`: a vector dataset with no
                            # object is a layer that 404s, so the schema
                            # refuses one. A real key is not needed here — only
                            # a non-null one.
                            "INSERT INTO dataset (name, kind, connector, storage_srid, "
                            "parquet_key, version, owner_user_id, visibility) "
                            "VALUES ('L', 'vector', 'upload', 2277, "
                            "'features/test/v1.parquet', 1, :o, 'private') RETURNING id"
                        ),
                        {"o": owner},
                    )
                ).scalar_one()
            )
        )


async def a_layer(engine: AsyncEngine, owner: UUID, dataset: UUID) -> UUID:
    async with engine.begin() as conn:
        await as_principal(conn, owner)
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO layer (name, dataset_id, presentation, "
                            "owner_user_id, visibility) VALUES ('Leases', :d, 'vector', "
                            ":o, 'private') RETURNING id"
                        ),
                        {"d": dataset, "o": owner},
                    )
                ).scalar_one()
            )
        )


async def a_basemap(engine: AsyncEngine, owner: UUID, name: str = "Base") -> UUID:
    async with engine.begin() as conn:
        await as_principal(conn, owner)
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO basemap (name, owner_user_id, visibility) "
                            "VALUES (:n, :o, 'private') RETURNING id"
                        ),
                        {"n": name, "o": owner},
                    )
                ).scalar_one()
            )
        )


# --- a shared layer cannot be deleted out from under a basemap -----------------


async def test_deleting_a_layer_a_basemap_uses_is_refused(
    migrator_engine: AsyncEngine, clean_database: None
) -> None:
    """**The point of the model.** Two basemaps sharing one layer is what
    `adr/0010` is for, and the sharing has to become visible at the moment it
    costs something — when someone tries to delete the layer — rather than
    afterwards, when another person's map has quietly lost it.

    `ON DELETE RESTRICT`, not CASCADE. With CASCADE the delete succeeds and the
    basemap silently loses a layer, which is the failure `07` §6.1 requires a
    named refusal for.
    """
    owner = await a_user(migrator_engine)
    layer = await a_layer(migrator_engine, owner, await a_dataset(migrator_engine, owner))
    first = await a_basemap(migrator_engine, owner, "Regional")
    second = await a_basemap(migrator_engine, owner, "Detail")

    async with migrator_engine.begin() as conn:
        await as_principal(conn, owner)
        for basemap in (first, second):
            await conn.execute(
                text("INSERT INTO basemap_layer (basemap_id, layer_id, z) VALUES (:b, :l, 0)"),
                {"b": basemap, "l": layer},
            )

    with pytest.raises(Exception) as excinfo:
        async with migrator_engine.begin() as conn:
            await as_principal(conn, owner)
            await conn.execute(text("DELETE FROM layer WHERE id = :id"), {"id": layer})

    assert "foreign key" in str(excinfo.value).lower()

    # And the basemaps still have it — the refusal left nothing half-done.
    async with migrator_engine.begin() as conn:
        await as_principal(conn, owner)
        remaining = (
            await conn.execute(
                text("SELECT count(*) FROM basemap_layer WHERE layer_id = :l"), {"l": layer}
            )
        ).scalar_one()
    assert remaining == 2


async def test_deleting_a_basemap_takes_its_layer_rows_but_not_its_layers(
    migrator_engine: AsyncEngine, clean_database: None
) -> None:
    """CASCADE on `basemap_id`, RESTRICT on `layer_id`, and the asymmetry is
    deliberate: a basemap owns its membership rows, and owns none of the
    layers. Deleting a basemap must not delete a layer another basemap uses,
    or that someone built independently."""
    owner = await a_user(migrator_engine)
    layer = await a_layer(migrator_engine, owner, await a_dataset(migrator_engine, owner))
    basemap = await a_basemap(migrator_engine, owner)

    async with migrator_engine.begin() as conn:
        await as_principal(conn, owner)
        await conn.execute(
            text("INSERT INTO basemap_layer (basemap_id, layer_id, z) VALUES (:b, :l, 0)"),
            {"b": basemap, "l": layer},
        )
        await conn.execute(text("DELETE FROM basemap WHERE id = :id"), {"id": basemap})

        memberships = (
            await conn.execute(text("SELECT count(*) FROM basemap_layer"))
        ).scalar_one()
        layers = (
            await conn.execute(text("SELECT count(*) FROM layer WHERE id = :l"), {"l": layer})
        ).scalar_one()

    assert memberships == 0, "membership rows go with the basemap"
    assert layers == 1, "the layer itself does not"


# --- the invariants the schema enforces ------------------------------------------


async def test_only_one_row_of_global_preferences_is_possible(
    migrator_engine: AsyncEngine, clean_database: None
) -> None:
    """A second would make "the global default" ambiguous with nothing to
    arbitrate it. `LIMIT 1` in application code is not a constraint — this is."""
    async with migrator_engine.begin() as conn:
        await conn.execute(text("INSERT INTO global_preferences DEFAULT VALUES"))

    with pytest.raises(Exception) as excinfo:
        async with migrator_engine.begin() as conn:
            await conn.execute(text("INSERT INTO global_preferences DEFAULT VALUES"))

    assert "duplicate key" in str(excinfo.value).lower()


async def test_a_layer_marked_team_visible_needs_a_team(
    migrator_engine: AsyncEngine, clean_database: None
) -> None:
    """The backstop revision 0003 added to every other ownable table, extended
    to the new ones. A row marked team-visible with no team matches nothing in
    the RLS clause, so it is **private in all but name** — and nothing errors
    or logs when it happens."""
    owner = await a_user(migrator_engine)
    dataset = await a_dataset(migrator_engine, owner)

    with pytest.raises(Exception) as excinfo:
        async with migrator_engine.begin() as conn:
            await as_principal(conn, owner)
            await conn.execute(
                text(
                    "INSERT INTO layer (name, dataset_id, presentation, owner_user_id, "
                    "visibility) VALUES ('Orphan', :d, 'vector', :o, 'team')"
                ),
                {"d": dataset, "o": owner},
            )

    assert "team_visibility_needs_a_team" in str(excinfo.value)


async def test_opacity_outside_zero_to_one_is_refused(
    migrator_engine: AsyncEngine, clean_database: None
) -> None:
    owner = await a_user(migrator_engine)
    dataset = await a_dataset(migrator_engine, owner)

    with pytest.raises(IntegrityError, match="default_opacity"):
        async with migrator_engine.begin() as conn:
            await as_principal(conn, owner)
            await conn.execute(
                text(
                    "INSERT INTO layer (name, dataset_id, presentation, owner_user_id, "
                    "default_opacity) VALUES ('Loud', :d, 'vector', :o, 1.5)"
                ),
                {"d": dataset, "o": owner},
            )


# --- row level security -----------------------------------------------------------


async def test_the_new_ownable_tables_have_all_four_policies_and_force_rls(
    migrator_engine: AsyncEngine,
) -> None:
    """**A new ownable table with three of the four policies has a hole in it,
    and the hole is invisible until someone finds it.**

    `FORCE` matters as much as `ENABLE`: policies do not apply to the table
    owner otherwise, and the migration role owns these tables — so a bug that
    ran application queries on the migration connection would bypass
    everything.
    """
    async with migrator_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tablename, count(*) FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename IN ('layer', 'basemap') "
                    "GROUP BY tablename"
                )
            )
        ).all()
        flags = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ('layer', 'basemap')"
                )
            )
        ).all()

    assert {str(name): int(count) for name, count in rows} == {"layer": 4, "basemap": 4}
    for _name, enabled, forced in flags:
        assert enabled and forced


async def test_the_join_table_is_deliberately_not_ownable(migrator_engine: AsyncEngine) -> None:
    """`basemap_layer` has no owner of its own; its visibility is entirely the
    basemap's. An `owner_user_id` on it would let whoever owns a *layer* attach
    it to someone else's basemap, which is the reverse of `adr/0010` §3."""
    async with migrator_engine.begin() as conn:
        columns = {
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'basemap_layer'"
                    )
                )
            ).all()
        }
        forced = (
            await conn.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE relname = 'basemap_layer'")
            )
        ).scalar_one()

    assert columns == {"basemap_id", "layer_id", "z"}
    assert not forced, "a join row is scoped by its parent, not by its own policy"


# --- capabilities --------------------------------------------------------------------


async def test_capability_columns_default_to_the_least_privilege(
    migrator_engine: AsyncEngine, clean_database: None
) -> None:
    """An existing deployment gains no administrators from this migration.
    Someone has to be promoted deliberately, which is the right direction to be
    wrong in for a column that grants org-wide publishing."""
    user = await a_user(migrator_engine)

    async with migrator_engine.begin() as conn:
        admin = (
            await conn.execute(
                text("SELECT is_global_admin FROM app_user WHERE id = :id"), {"id": user}
            )
        ).scalar_one()
        team = UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO team (slug, display_name) VALUES (:s, 'Geo') RETURNING id"
                        ),
                        {"s": f"geo-{uuid4().hex[:6]}"},
                    )
                ).scalar_one()
            )
        )
        await conn.execute(
            text("INSERT INTO team_member (team_id, user_id) VALUES (:t, :u)"),
            {"t": team, "u": user},
        )
        role = (
            await conn.execute(
                text("SELECT role FROM team_member WHERE team_id = :t AND user_id = :u"),
                {"t": team, "u": user},
            )
        ).scalar_one()

    assert admin is False
    assert role == "member"
