"""Layers and basemaps. `adr/0010`, `02-data-model.md` §3.7a, `07` §6.1.

A **layer** is a dataset plus how it is drawn. A **basemap** is an ordered
collection of layers. The two are independent objects, and that independence is
the whole point: two basemaps may share one layer, and a layer outlives any
particular map that uses it.

Sharing is also what makes deletion interesting. `delete_layer` refuses while a
basemap still uses the layer and **names the basemaps** — because the
alternative is that somebody else's map quietly loses a layer and nobody can
say when. The database enforces it with `ON DELETE RESTRICT` (migration 0004);
this module is what turns a foreign-key violation into a sentence someone can
act on.

Duplication is by reference, never by copy: `duplicate_layer` writes a new row
pointing at the same `dataset_id`. `07` §6.1's "the same shapefile styled two
ways is legitimately two layers" is exactly this case, and copying the data for
a styling variation would double the storage for nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import NotFound, PermissionDenied
from webmap_core.logging import get_logger
from webmap_core.permissions import Permission, Principal, Visibility
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.capabilities import require_publish_scope
from webmap_core.services.ownable import load_and_require, resolve_owner_team

log = get_logger(__name__)

#: How a layer is *drawn* — `presentation_t` from migration 0004. Not the
#: dataset kind: a grid can be a filled surface or a set of contours, and
#: default basemaps are keyed on this, so conflating the two would make "my
#: default for contour maps" inexpressible.
PRESENTATIONS = (
    "vector",
    "filled_grid",
    "contour",
    "filled_contour",
    "hillshade",
    "points",
)

#: A basemap with more layers than this is not a basemap, it is a map. The
#: limit exists because every layer in a basemap is resolved on every session
#: open, and the cost is paid by everyone who uses it rather than by whoever
#: built it.
MAX_BASEMAP_LAYERS = 30


@dataclass(frozen=True)
class LayerRow:
    """A layer as the services and API hand it about.

    Deliberately not the whole table — `schema_version` and the ownership block
    are the permission layer's business, and a caller that could read them here
    would eventually branch on them.
    """

    id: UUID
    name: str
    dataset_id: UUID
    presentation: str
    visibility: str
    default_opacity: float
    description: str | None = None
    style_template_id: UUID | None = None
    symbology: dict[str, Any] | None = None


def _validate(presentation: str, default_opacity: float) -> None:
    if presentation not in PRESENTATIONS:
        raise ValueError(
            f"'{presentation}' is not a presentation. A presentation says how a "
            f"layer is drawn, not what the dataset is: {', '.join(PRESENTATIONS)}."
        )
    if not 0.0 <= default_opacity <= 1.0:
        raise ValueError(
            f"Opacity runs from 0 to 1; got {default_opacity:g}. To hide a layer "
            f"use the visibility toggle in the map — an opacity of 0 leaves it "
            f"drawing and invisible, which is much harder to notice."
        )


# --- layers -------------------------------------------------------------------


async def create_layer(
    conn: AsyncConnection,
    principal: Principal,
    *,
    name: str,
    dataset_id: UUID,
    presentation: str,
    description: str | None = None,
    style_template_id: UUID | None = None,
    symbology: dict[str, Any] | None = None,
    default_opacity: float = 1.0,
    visibility: Visibility = Visibility.PRIVATE,
    owner_team_id: UUID | None = None,
) -> UUID:
    """Register a layer over a dataset the caller can see.

    **The dataset is permission-checked, not merely referenced.** Without the
    check a caller could wrap a layer around someone else's private dataset and
    then read it through the layer — the foreign key would accept the id
    happily, and RLS on `layer` would say the layer is theirs, because it is.
    This is the same hole `aggregation.resolve_inputs` closes for operands.
    """
    _validate(presentation, default_opacity)
    await load_and_require(conn, "dataset", dataset_id, principal, Permission.VIEWER)
    team = resolve_owner_team(principal, visibility, owner_team_id)
    await require_publish_scope(conn, principal, visibility, team)

    result = await conn.execute(
        text(
            """
            INSERT INTO layer (
                name, description, dataset_id, presentation, style_template_id,
                symbology, default_opacity, owner_user_id, owner_team_id, visibility)
            VALUES (
                :name, :description, :dataset_id,
                CAST(:presentation AS presentation_t), :template,
                CAST(:symbology AS JSONB), :opacity, :owner, :team,
                CAST(:visibility AS visibility_t))
            RETURNING id
            """
        ),
        {
            "name": name,
            "description": description,
            "dataset_id": dataset_id,
            "presentation": presentation,
            "template": style_template_id,
            "symbology": None if symbology is None else json.dumps(symbology),
            "opacity": default_opacity,
            "owner": principal.user_id,
            "team": team,
            "visibility": visibility.value,
        },
    )
    layer_id = UUID(str(result.scalar_one()))

    await record(
        conn,
        action=AuditAction.LAYER_CREATED,
        principal=principal,
        object_type="layer",
        object_id=layer_id,
        detail={"dataset_id": str(dataset_id), "presentation": presentation},
    )
    log.info("layer_created", layer_id=str(layer_id), dataset_id=str(dataset_id))
    return layer_id


async def get_layer(conn: AsyncConnection, principal: Principal, layer_id: UUID) -> LayerRow:
    await load_and_require(conn, "layer", layer_id, principal, Permission.VIEWER)
    row = (
        await conn.execute(
            text(
                "SELECT id, name, description, dataset_id, presentation, "
                "style_template_id, symbology, default_opacity, visibility "
                "FROM layer WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": layer_id},
        )
    ).one_or_none()
    if row is None:
        raise NotFound(
            f"Layer {layer_id} has been deleted. Deletes are soft for 30 days, "
            f"so an administrator can still restore it."
        )

    symbology = row.symbology
    if isinstance(symbology, str):
        symbology = json.loads(symbology)

    return LayerRow(
        id=row.id,
        name=row.name,
        description=row.description,
        dataset_id=row.dataset_id,
        presentation=str(row.presentation),
        style_template_id=row.style_template_id,
        symbology=symbology,
        default_opacity=float(row.default_opacity),
        visibility=str(row.visibility),
    )


async def list_layers(
    conn: AsyncConnection,
    principal: Principal,
    *,
    dataset_id: UUID | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Layers the caller can see, newest first. RLS does the filtering."""
    clause = "AND dataset_id = :dataset_id" if dataset_id is not None else ""
    params: dict[str, Any] = {"limit": limit}
    if dataset_id is not None:
        params["dataset_id"] = dataset_id

    result = await conn.execute(
        text(
            f"""
            SELECT id, name, description, dataset_id, presentation, visibility,
                   default_opacity, created_at
            FROM layer
            WHERE deleted_at IS NULL {clause}
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        params,
    )
    return [dict(row._mapping) for row in result]


async def update_layer(
    conn: AsyncConnection,
    principal: Principal,
    layer_id: UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    presentation: str | None = None,
    style_template_id: UUID | None = None,
    symbology: dict[str, Any] | None = None,
    default_opacity: float | None = None,
) -> None:
    """Edit a layer in place. Editor required.

    Only the fields passed are written, so a caller changing opacity cannot
    blank a description it never loaded. `dataset_id` is deliberately absent:
    repointing a layer at another dataset would change what every basemap using
    it shows, without any of their owners being involved. Duplicate instead.
    """
    await load_and_require(conn, "layer", layer_id, principal, Permission.EDITOR)

    sets: list[str] = []
    params: dict[str, Any] = {"id": layer_id}
    if name is not None:
        sets.append("name = :name")
        params["name"] = name
    if description is not None:
        sets.append("description = :description")
        params["description"] = description
    if presentation is not None:
        _validate(presentation, 1.0)
        sets.append("presentation = CAST(:presentation AS presentation_t)")
        params["presentation"] = presentation
    if style_template_id is not None:
        sets.append("style_template_id = :template")
        params["template"] = style_template_id
    if symbology is not None:
        sets.append("symbology = CAST(:symbology AS JSONB)")
        params["symbology"] = json.dumps(symbology)
    if default_opacity is not None:
        _validate(PRESENTATIONS[0], default_opacity)
        sets.append("default_opacity = :opacity")
        params["opacity"] = default_opacity

    if not sets:
        return

    changed = sorted(k for k in params if k != "id")
    sets.append("updated_at = now()")
    await conn.execute(
        text(f"UPDATE layer SET {', '.join(sets)} WHERE id = :id AND deleted_at IS NULL"),
        params,
    )
    await record(
        conn,
        action=AuditAction.LAYER_UPDATED,
        principal=principal,
        object_type="layer",
        object_id=layer_id,
        detail={"fields": changed},
    )


async def duplicate_layer(
    conn: AsyncConnection,
    principal: Principal,
    layer_id: UUID,
    *,
    name: str | None = None,
) -> UUID:
    """Copy a layer the caller can see into one of their own.

    **The dataset is referenced, not copied.** The roadmap's acceptance
    criterion for this is measured in storage rather than by inspection, which
    is only a meaningful test because the copy is by reference.

    The duplicate is **private and owned by the caller**, whatever the original
    was. Inheriting org visibility would republish someone else's work under a
    new name without their involvement.
    """
    source = await get_layer(conn, principal, layer_id)

    return await create_layer(
        conn,
        principal,
        name=name or f"{source.name} (copy)",
        dataset_id=source.dataset_id,
        presentation=source.presentation,
        description=source.description,
        style_template_id=source.style_template_id,
        symbology=source.symbology,
        default_opacity=source.default_opacity,
        visibility=Visibility.PRIVATE,
    )


async def basemaps_using(conn: AsyncConnection, layer_id: UUID) -> list[str]:
    """Names of the live basemaps that reference this layer.

    No permission check on purpose, and it is the one function here without
    one: the caller has already been checked against the *layer*, and the names
    exist to make the refusal message useful. Filtering them by what the caller
    can see would produce "cannot delete, used by 0 basemaps", which is the
    worst possible error message.
    """
    result = await conn.execute(
        text(
            """
            SELECT b.name
            FROM basemap_layer bl
            JOIN basemap b ON b.id = bl.basemap_id
            WHERE bl.layer_id = :id AND b.deleted_at IS NULL
            ORDER BY b.name
            """
        ),
        {"id": layer_id},
    )
    return [str(name) for name in result.scalars().all()]


async def delete_layer(conn: AsyncConnection, principal: Principal, layer_id: UUID) -> None:
    """Soft-delete a layer, unless a basemap still uses it.

    **The refusal names the basemaps** — `07` §6.1 — which is why this is not
    left to the foreign key. `ON DELETE RESTRICT` produces the right outcome
    and an unusable message; the person reading it needs to know *whose maps*
    they are about to break, because the next step is a conversation.

    Soft, not hard: `03` §8's thirty-day window applies here as everywhere.
    """
    await load_and_require(conn, "layer", layer_id, principal, Permission.EDITOR)

    used_by = await basemaps_using(conn, layer_id)
    if used_by:
        names = ", ".join(f"'{name}'" for name in used_by)
        raise PermissionDenied(
            f"This layer is used by {len(used_by)} basemap(s): {names}. Deleting "
            f"it would empty a layer out of every map built on them. Remove it "
            f"from those basemaps first, or delete the basemaps."
        )

    await conn.execute(
        text("UPDATE layer SET deleted_at = now() WHERE id = :id AND deleted_at IS NULL"),
        {"id": layer_id},
    )
    await record(
        conn,
        action=AuditAction.LAYER_DELETED,
        principal=principal,
        object_type="layer",
        object_id=layer_id,
    )


# --- basemaps ------------------------------------------------------------------


async def create_basemap(
    conn: AsyncConnection,
    principal: Principal,
    *,
    name: str,
    layer_ids: list[UUID],
    description: str | None = None,
    visibility: Visibility = Visibility.PRIVATE,
    owner_team_id: UUID | None = None,
) -> UUID:
    """A basemap over layers the caller can see, in the order given.

    **Every layer is permission-checked.** A basemap referencing a layer the
    caller cannot see would let them read it by opening the basemap.

    `z` comes from position in `layer_ids`, 0 at the bottom (`02` §3.7a).
    Storing the order rather than deriving it from creation time is what lets a
    basemap be reordered without being rebuilt.
    """
    _check_membership(layer_ids)
    for layer_id in layer_ids:
        await load_and_require(conn, "layer", layer_id, principal, Permission.VIEWER)

    team = resolve_owner_team(principal, visibility, owner_team_id)
    await require_publish_scope(conn, principal, visibility, team)
    result = await conn.execute(
        text(
            """
            INSERT INTO basemap (name, description, owner_user_id, owner_team_id, visibility)
            VALUES (:name, :description, :owner, :team, CAST(:visibility AS visibility_t))
            RETURNING id
            """
        ),
        {
            "name": name,
            "description": description,
            "owner": principal.user_id,
            "team": team,
            "visibility": visibility.value,
        },
    )
    basemap_id = UUID(str(result.scalar_one()))

    await _write_membership(conn, basemap_id, layer_ids)
    await record(
        conn,
        action=AuditAction.BASEMAP_CREATED,
        principal=principal,
        object_type="basemap",
        object_id=basemap_id,
        detail={"layers": len(layer_ids)},
    )
    log.info("basemap_created", basemap_id=str(basemap_id), layers=len(layer_ids))
    return basemap_id


def _check_membership(layer_ids: list[UUID]) -> None:
    if not layer_ids:
        raise ValueError(
            "A basemap needs at least one layer. An empty one resolves to "
            "nothing and is indistinguishable from a broken default."
        )
    if len(layer_ids) > MAX_BASEMAP_LAYERS:
        raise ValueError(
            f"A basemap may hold {MAX_BASEMAP_LAYERS} layers; this one has "
            f"{len(layer_ids)}. Every layer is resolved on every session open, "
            f"and the cost is paid by everyone who uses the basemap. Split it, "
            f"or keep the working layers in the map itself."
        )
    if len(set(layer_ids)) != len(layer_ids):
        raise ValueError(
            "The same layer appears twice in this basemap. Draw order is a "
            "position, so a repeat is ambiguous rather than a second copy — "
            "duplicate the layer if you want it drawn twice."
        )


async def _write_membership(
    conn: AsyncConnection, basemap_id: UUID, layer_ids: list[UUID]
) -> None:
    """Replace a basemap's membership with `layer_ids`, bottom first.

    Delete-then-insert rather than a diff. The membership is at most 30 rows
    and `z` is a dense sequence, so a diff would compute the same writes with
    more code and one more way to leave a gap in the ordering.
    """
    await conn.execute(
        text("DELETE FROM basemap_layer WHERE basemap_id = :b"), {"b": basemap_id}
    )
    for z, layer_id in enumerate(layer_ids):
        await conn.execute(
            text("INSERT INTO basemap_layer (basemap_id, layer_id, z) VALUES (:b, :l, :z)"),
            {"b": basemap_id, "l": layer_id, "z": z},
        )


async def basemap_layers(
    conn: AsyncConnection, principal: Principal, basemap_id: UUID
) -> list[dict[str, Any]]:
    """The basemap's layers, bottom first.

    Ordered by `z`, not by insertion: reordering rewrites `z`, and a query that
    ignored it would draw yesterday's order. Soft-deleted layers are excluded
    rather than returned as tombstones — `delete_layer` refuses while a basemap
    uses one, so a deleted layer here means an administrator removed it out of
    band, and the map should simply not draw it.
    """
    await load_and_require(conn, "basemap", basemap_id, principal, Permission.VIEWER)

    result = await conn.execute(
        text(
            """
            SELECT l.id, l.name, l.dataset_id, l.presentation, l.symbology,
                   l.style_template_id, l.default_opacity, bl.z
            FROM basemap_layer bl
            JOIN layer l ON l.id = bl.layer_id
            WHERE bl.basemap_id = :id AND l.deleted_at IS NULL
            ORDER BY bl.z
            """
        ),
        {"id": basemap_id},
    )
    return [dict(row._mapping) for row in result]


async def list_basemaps(
    conn: AsyncConnection, principal: Principal, *, limit: int = 50
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT b.id, b.name, b.description, b.visibility, b.created_at,
                   count(bl.layer_id) AS layer_count
            FROM basemap b
            LEFT JOIN basemap_layer bl ON bl.basemap_id = b.id
            WHERE b.deleted_at IS NULL
            GROUP BY b.id
            ORDER BY b.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row._mapping) for row in result]


async def set_basemap_layers(
    conn: AsyncConnection, principal: Principal, basemap_id: UUID, layer_ids: list[UUID]
) -> None:
    """Reorder or replace a basemap's layers. Editor on the basemap required.

    Viewer on each layer, not editor: putting a layer into a map is reading it,
    and requiring edit access would make an org-wide published layer unusable
    by anyone but its owner.
    """
    await load_and_require(conn, "basemap", basemap_id, principal, Permission.EDITOR)
    _check_membership(layer_ids)
    for layer_id in layer_ids:
        await load_and_require(conn, "layer", layer_id, principal, Permission.VIEWER)

    await _write_membership(conn, basemap_id, layer_ids)
    await conn.execute(
        text("UPDATE basemap SET updated_at = now() WHERE id = :id"), {"id": basemap_id}
    )
    await record(
        conn,
        action=AuditAction.BASEMAP_UPDATED,
        principal=principal,
        object_type="basemap",
        object_id=basemap_id,
        detail={"layers": len(layer_ids)},
    )


async def delete_basemap(conn: AsyncConnection, principal: Principal, basemap_id: UUID) -> None:
    """Soft-delete a basemap. Its layers survive, which is the point.

    The membership rows are left alone: `deleted_at` is what every query
    filters on, and hard-deleting the join rows would make an undelete restore
    an empty basemap — worse than not restoring it, because it looks like it
    worked.
    """
    await load_and_require(conn, "basemap", basemap_id, principal, Permission.EDITOR)
    await conn.execute(
        text("UPDATE basemap SET deleted_at = now() WHERE id = :id AND deleted_at IS NULL"),
        {"id": basemap_id},
    )
    await record(
        conn,
        action=AuditAction.BASEMAP_DELETED,
        principal=principal,
        object_type="basemap",
        object_id=basemap_id,
    )


async def save_map_as_basemap(
    conn: AsyncConnection,
    principal: Principal,
    *,
    name: str,
    layer_ids: list[UUID],
    active_layer_id: UUID | None = None,
    include_active: bool = False,
    visibility: Visibility = Visibility.PRIVATE,
) -> UUID:
    """`07` §6.1: any map can be saved as a basemap.

    The active layer is **excluded by default**, deliberately. Saving it means
    every map later built on this basemap opens with last week's working grid
    underneath it, and whoever gets it has no idea where it came from.
    """
    keep = [layer_id for layer_id in layer_ids if include_active or layer_id != active_layer_id]
    if not keep:
        raise ValueError(
            "That map has nothing to save but its active layer. Pass "
            "include_active=true if the working layer is the one you meant to "
            "keep as a backdrop."
        )
    return await create_basemap(
        conn, principal, name=name, layer_ids=keep, visibility=visibility
    )


__all__ = [
    "MAX_BASEMAP_LAYERS",
    "PRESENTATIONS",
    "LayerRow",
    "basemap_layers",
    "basemaps_using",
    "create_basemap",
    "create_layer",
    "delete_basemap",
    "delete_layer",
    "duplicate_layer",
    "get_layer",
    "list_basemaps",
    "list_layers",
    "save_map_as_basemap",
    "set_basemap_layers",
    "update_layer",
]
