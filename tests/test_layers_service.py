"""Layers, basemaps and default resolution as services. `adr/0010`, `07` §6.1.

`test_layers_basemaps_schema.py` covers what the *database* enforces. This
covers what only the service layer can: the refusal that names the basemaps,
the permission check on a layer's dataset, duplication by reference, and the
tier-exhausting default resolution — none of which a constraint can express.

Needs Postgres. Skips with instructions when absent.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from webmap_core.db.session import principal_session
from webmap_core.exceptions import NotFound, PermissionDenied
from webmap_core.permissions import Principal, Visibility
from webmap_core.services import layers, preferences

pytestmark = pytest.mark.integration


async def a_dataset(
    engine: AsyncEngine,
    principal: Principal,
    *,
    name: str = "Leases",
    visibility: str = "private",
) -> UUID:
    async with principal_session(engine, principal) as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO dataset (name, kind, connector, storage_srid, "
                            "parquet_key, version, owner_user_id, visibility) "
                            "VALUES (:n, 'vector', 'upload', 2277, "
                            "'features/test/v1.parquet', 1, :o, "
                            "CAST(:v AS visibility_t)) RETURNING id"
                        ),
                        {"n": name, "o": principal.user_id, "v": visibility},
                    )
                ).scalar_one()
            )
        )


async def a_layer(
    engine: AsyncEngine, principal: Principal, dataset_id: UUID, *, name: str = "Leases"
) -> UUID:
    async with principal_session(engine, principal) as conn:
        return await layers.create_layer(
            conn,
            principal,
            name=name,
            dataset_id=dataset_id,
            presentation="vector",
        )


# --- the dataset behind a layer is permission-checked ---------------------------


async def test_a_layer_cannot_be_wrapped_around_a_dataset_you_cannot_see(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """**The hole this check closes.** The foreign key would accept the id, and
    RLS on `layer` would then say the layer belongs to whoever made it, because
    it does — so the private dataset would be readable through it.
    """
    owner, stranger = principals["owner"], principals["stranger"]
    dataset_id = await a_dataset(engine, owner)

    async with principal_session(engine, stranger) as conn:
        with pytest.raises(NotFound):
            await layers.create_layer(
                conn,
                stranger,
                name="Not mine",
                dataset_id=dataset_id,
                presentation="vector",
            )


async def test_presentation_is_not_the_dataset_kind(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """'vector' is a presentation; 'grid' is a dataset kind and not one.

    The message has to say which axis it is on, or the obvious fix is to pass
    the other wrong value.
    """
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)

    async with principal_session(engine, owner) as conn:
        with pytest.raises(ValueError, match="how a layer is drawn"):
            await layers.create_layer(
                conn, owner, name="X", dataset_id=dataset_id, presentation="grid"
            )


# --- duplication is by reference -----------------------------------------------


async def test_duplicating_a_layer_shares_the_dataset(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """`07` §6.1: the same shapefile styled two ways is two layers, one dataset.

    Asserted on `dataset_id` rather than on storage size, which is the roadmap's
    acceptance criterion — same claim, and this one runs in a millisecond.
    """
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)
    original = await a_layer(engine, owner, dataset_id)

    async with principal_session(engine, owner) as conn:
        copy_id = await layers.duplicate_layer(conn, owner, original, name="Leases (red)")
        copy = await layers.get_layer(conn, owner, copy_id)

    assert copy_id != original
    assert copy.dataset_id == dataset_id
    assert copy.name == "Leases (red)"


async def test_duplicating_someone_elses_layer_makes_it_private_and_yours(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """Inheriting org visibility would republish someone else's work under a
    new name without their involvement."""
    owner, teammate = principals["owner"], principals["teammate"]
    dataset_id = await a_dataset(engine, owner, visibility="org")

    async with principal_session(engine, owner) as conn:
        shared = await layers.create_layer(
            conn,
            owner,
            name="Shared",
            dataset_id=dataset_id,
            presentation="vector",
            visibility=Visibility.TEAM,
        )

    async with principal_session(engine, teammate) as conn:
        copy_id = await layers.duplicate_layer(conn, teammate, shared)
        copy = await layers.get_layer(conn, teammate, copy_id)
    assert copy.visibility == "private"

    # And the original owner cannot see the copy at all.
    async with principal_session(engine, owner) as conn:
        with pytest.raises(NotFound):
            await layers.get_layer(conn, owner, copy_id)


# --- the delete refusal names the basemaps --------------------------------------


async def test_deleting_a_layer_a_basemap_uses_names_the_basemaps(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """`07` §6.1. The foreign key produces the right outcome and an unusable
    message; the person reading it needs to know *whose maps* they are about to
    break, because the next step is a conversation.
    """
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)
    layer_id = await a_layer(engine, owner, dataset_id)

    async with principal_session(engine, owner) as conn:
        await layers.create_basemap(conn, owner, name="Regional", layer_ids=[layer_id])
        await layers.create_basemap(conn, owner, name="Field", layer_ids=[layer_id])

    async with principal_session(engine, owner) as conn:
        with pytest.raises(PermissionDenied) as raised:
            await layers.delete_layer(conn, owner, layer_id)

    message = str(raised.value)
    assert "'Field'" in message and "'Regional'" in message
    assert "2 basemap" in message


async def test_a_layer_no_basemap_uses_deletes(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)
    layer_id = await a_layer(engine, owner, dataset_id)

    async with principal_session(engine, owner) as conn:
        await layers.delete_layer(conn, owner, layer_id)
        with pytest.raises(NotFound):
            await layers.get_layer(conn, owner, layer_id)


async def test_deleting_a_basemap_leaves_its_layers(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """The asymmetry is the model: a basemap is a collection, not an owner."""
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)
    layer_id = await a_layer(engine, owner, dataset_id)

    async with principal_session(engine, owner) as conn:
        basemap_id = await layers.create_basemap(
            conn, owner, name="Regional", layer_ids=[layer_id]
        )
        await layers.delete_basemap(conn, owner, basemap_id)
        # The layer survives, and is now deletable because the basemap is gone.
        assert await layers.basemaps_using(conn, layer_id) == []
        await layers.delete_layer(conn, owner, layer_id)


# --- basemap membership ---------------------------------------------------------


async def test_a_basemap_cannot_reference_a_layer_you_cannot_see(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """Otherwise opening the basemap is a read of the layer."""
    owner, stranger = principals["owner"], principals["stranger"]
    dataset_id = await a_dataset(engine, owner)
    layer_id = await a_layer(engine, owner, dataset_id)

    async with principal_session(engine, stranger) as conn:
        with pytest.raises(NotFound):
            await layers.create_basemap(conn, stranger, name="Borrowed", layer_ids=[layer_id])


async def test_draw_order_is_position_not_creation_time(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """Reordering rewrites `z`; a query that ignored it would draw yesterday's
    order, which is exactly the bug nobody reports because the map still looks
    like a map."""
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)
    first = await a_layer(engine, owner, dataset_id, name="Roads")
    second = await a_layer(engine, owner, dataset_id, name="Leases")

    async with principal_session(engine, owner) as conn:
        basemap_id = await layers.create_basemap(
            conn, owner, name="Regional", layer_ids=[first, second]
        )
        assert [
            row["name"] for row in await layers.basemap_layers(conn, owner, basemap_id)
        ] == [
            "Roads",
            "Leases",
        ]

        await layers.set_basemap_layers(conn, owner, basemap_id, [second, first])
        assert [
            row["name"] for row in await layers.basemap_layers(conn, owner, basemap_id)
        ] == [
            "Leases",
            "Roads",
        ]


async def test_a_repeated_layer_is_refused(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """`z` is a position, so a repeat is ambiguous rather than a second copy —
    and the composite primary key would raise something far less helpful."""
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)
    layer_id = await a_layer(engine, owner, dataset_id)

    async with principal_session(engine, owner) as conn:
        with pytest.raises(ValueError, match="appears twice"):
            await layers.create_basemap(
                conn, owner, name="Twice", layer_ids=[layer_id, layer_id]
            )


async def test_an_empty_basemap_is_refused(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    owner = principals["owner"]
    async with principal_session(engine, owner) as conn:
        with pytest.raises(ValueError, match="at least one layer"):
            await layers.create_basemap(conn, owner, name="Nothing", layer_ids=[])


# --- save any map as a basemap ---------------------------------------------------


async def test_saving_a_map_as_a_basemap_drops_the_active_layer(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """`07` §6.1, and the default matters: saving the active layer means every
    map later built on this basemap opens with last week's working grid
    underneath it."""
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)
    backdrop = await a_layer(engine, owner, dataset_id, name="Roads")
    working = await a_layer(engine, owner, dataset_id, name="Top Wolfcamp")

    async with principal_session(engine, owner) as conn:
        basemap_id = await layers.save_map_as_basemap(
            conn,
            owner,
            name="Regional",
            layer_ids=[backdrop, working],
            active_layer_id=working,
        )
        names = [row["name"] for row in await layers.basemap_layers(conn, owner, basemap_id)]
    assert names == ["Roads"]


async def test_saving_a_map_of_only_the_active_layer_says_so(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    owner = principals["owner"]
    dataset_id = await a_dataset(engine, owner)
    working = await a_layer(engine, owner, dataset_id)

    async with principal_session(engine, owner) as conn:
        with pytest.raises(ValueError, match="include_active"):
            await layers.save_map_as_basemap(
                conn,
                owner,
                name="Regional",
                layer_ids=[working],
                active_layer_id=working,
            )


# --- default basemap resolution ---------------------------------------------------


async def _basemap_named(
    engine: AsyncEngine, principal: Principal, name: str, *, visibility: Visibility
) -> UUID:
    dataset_id = await a_dataset(engine, principal, name=f"ds-{name}", visibility="org")
    async with principal_session(engine, principal) as conn:
        layer_id = await layers.create_layer(
            conn,
            principal,
            name=f"layer-{name}",
            dataset_id=dataset_id,
            presentation="vector",
            visibility=visibility,
            owner_team_id=(
                None if visibility is not Visibility.TEAM else sorted(principal.team_ids)[0]
            ),
        )
        return await layers.create_basemap(
            conn,
            principal,
            name=name,
            layer_ids=[layer_id],
            visibility=visibility,
            owner_team_id=(
                None if visibility is not Visibility.TEAM else sorted(principal.team_ids)[0]
            ),
        )


async def test_no_default_anywhere_resolves_to_none(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """A legitimate state, not an error: a map with no basemap is a map on a
    blank background, and a fresh deployment has no global default."""
    owner = principals["owner"]
    async with principal_session(engine, owner) as conn:
        assert await preferences.resolve_default_basemap(conn, owner) is None


async def test_a_users_general_default_beats_their_teams_specific_one(
    engine: AsyncEngine, principals: dict[str, Any], people: dict[str, UUID]
) -> None:
    """**The rule that is easy to get backwards.** `adr/0010` §4 exhausts a tier
    before descending: matching presentation across all three tiers first would
    let a team's contour default beat a user's own general default, and a user
    who set a general default meant it to beat their team's.
    """
    owner = principals["owner"]
    mine = await _basemap_named(engine, owner, "Mine", visibility=Visibility.PRIVATE)
    theirs = await _basemap_named(engine, owner, "Theirs", visibility=Visibility.TEAM)

    async with principal_session(engine, owner) as conn:
        await preferences.set_user_default_basemap(conn, owner, basemap_id=mine)

    async with principal_session(engine, owner) as conn:
        # A team default is a team-administrator write; set it directly so this
        # test is about resolution rather than about capabilities.
        await conn.execute(
            text(
                "INSERT INTO team_preferences (team_id, default_basemaps) "
                "VALUES (:t, CAST(:d AS JSONB))"
            ),
            {"t": people["owner_team"], "d": f'{{"contour": "{theirs}"}}'},
        )

    async with principal_session(engine, owner) as conn:
        resolved = await preferences.resolve_default_basemap(
            conn, owner, presentation="contour"
        )
    assert resolved is not None
    assert resolved.basemap_id == mine
    assert resolved.tier == "user"
    assert resolved.presentation_key == "*"


async def test_a_presentation_specific_default_beats_the_general_one_in_its_tier(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    owner = principals["owner"]
    general = await _basemap_named(engine, owner, "General", visibility=Visibility.PRIVATE)
    contours = await _basemap_named(engine, owner, "Contours", visibility=Visibility.PRIVATE)

    async with principal_session(engine, owner) as conn:
        await preferences.set_user_default_basemap(conn, owner, basemap_id=general)
        await preferences.set_user_default_basemap(
            conn, owner, basemap_id=contours, presentation="contour"
        )

    async with principal_session(engine, owner) as conn:
        for presentation, expected in (("contour", contours), ("vector", general)):
            resolved = await preferences.resolve_default_basemap(
                conn, owner, presentation=presentation
            )
            assert resolved is not None and resolved.basemap_id == expected


async def test_a_default_naming_a_deleted_basemap_falls_through(
    engine: AsyncEngine, principals: dict[str, Any], people: dict[str, UUID]
) -> None:
    """Not in the ADR, and it follows from it: the tiers exist so a default
    always lands somewhere. Stopping at a dangling one would defeat that, and
    the user whose default broke is rarely the one who broke it.
    """
    owner = principals["owner"]
    doomed = await _basemap_named(engine, owner, "Doomed", visibility=Visibility.PRIVATE)
    survivor = await _basemap_named(engine, owner, "Survivor", visibility=Visibility.TEAM)

    async with principal_session(engine, owner) as conn:
        await preferences.set_user_default_basemap(conn, owner, basemap_id=doomed)
        await conn.execute(
            text(
                "INSERT INTO team_preferences (team_id, default_basemaps) "
                "VALUES (:t, CAST(:d AS JSONB))"
            ),
            {"t": people["owner_team"], "d": f'{{"*": "{survivor}"}}'},
        )
        await layers.delete_basemap(conn, owner, doomed)

    async with principal_session(engine, owner) as conn:
        resolved = await preferences.resolve_default_basemap(conn, owner)
    assert resolved is not None
    assert resolved.basemap_id == survivor
    assert resolved.tier == "team"
    assert resolved.team_slug == "owner-team"


async def test_clearing_a_default_removes_only_that_key(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    owner = principals["owner"]
    general = await _basemap_named(engine, owner, "General", visibility=Visibility.PRIVATE)
    contours = await _basemap_named(engine, owner, "Contours", visibility=Visibility.PRIVATE)

    async with principal_session(engine, owner) as conn:
        await preferences.set_user_default_basemap(conn, owner, basemap_id=general)
        await preferences.set_user_default_basemap(
            conn, owner, basemap_id=contours, presentation="contour"
        )
        await preferences.set_user_default_basemap(
            conn, owner, basemap_id=None, presentation="contour"
        )
        stored = (await preferences.get_preferences(conn, owner))["user"]

    assert stored == {"*": str(general)}


async def test_a_default_must_name_a_basemap_the_setter_can_see(
    engine: AsyncEngine, principals: dict[str, Any]
) -> None:
    """Otherwise the preference screen shows a default set while every user
    falls through to no basemap."""
    owner = principals["owner"]
    async with principal_session(engine, owner) as conn:
        with pytest.raises(NotFound):
            await preferences.set_user_default_basemap(conn, owner, basemap_id=uuid4())
