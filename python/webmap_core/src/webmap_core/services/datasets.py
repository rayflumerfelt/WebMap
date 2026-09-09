"""Dataset registry service. `02-data-model.md` §3.5.

The linchpin: when a geologist says "show me this data", Claude needs a
referent, and this is where names resolve to ids and ids resolve to storage
keys.

**The storage-key resolution is the security-critical part.** RLS protects the
registry row; it has no reach into object storage (`02` §4.1), so
`resolve_feature_object` is the single enforcement point for feature content
and must never be bypassed by reading `parquet_key` off a row directly.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import NotFound
from webmap_core.models import DatasetKind, GeometryKind
from webmap_core.permissions import Permission, Principal, Visibility, require_owner
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.grants import DbGrantStore
from webmap_core.services.ownable import load_and_require, load_ownable

#: Cap on a single page. Claude reads many summaries and a 500-row response
#: crowds out the conversation (`04-mcp-server.md` §1).
MAX_PAGE = 100


@dataclass(frozen=True)
class DatasetPage:
    total: int
    items: list[dict[str, Any]]
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    @property
    def next_offset(self) -> int | None:
        return self.offset + len(self.items) if self.has_more else None


async def list_datasets(
    conn: AsyncConnection,
    principal: Principal,
    *,
    project_id: UUID | None = None,
    kind: DatasetKind | None = None,
    limit: int = 25,
    offset: int = 0,
) -> DatasetPage:
    """List datasets the principal can access.

    The filtering here is RLS: the connection already carries the principal's
    context, so the read policy scopes the query. There is no second
    application-layer filter to write, and adding one that duplicated the
    policy would be a second place for the rule to drift.

    What the application layer still owns is the *shape* — pagination bounds
    and column selection — and `principal` is taken explicitly rather than
    inferred from the connection so that calling this without one is
    impossible rather than merely unlikely.
    """
    limit = max(1, min(limit, MAX_PAGE))
    filters = ["deleted_at IS NULL"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if project_id is not None:
        filters.append("project_id = :project_id")
        params["project_id"] = project_id
    if kind is not None:
        filters.append("kind = :kind")
        params["kind"] = kind.value
    where = " AND ".join(filters)

    total = await conn.execute(
        text(f"SELECT count(*) FROM dataset WHERE {where}"),
        params,
    )
    rows = await conn.execute(
        text(
            f"""
            SELECT id, name, kind, geometry_kind, feature_count, bbox_4326,
                   sync_state, synced_at, caption, updated_at
            FROM dataset WHERE {where}
            ORDER BY updated_at DESC, name
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    )
    return DatasetPage(
        total=int(total.scalar_one()),
        items=[dict(row._mapping) for row in rows],
        offset=offset,
        limit=limit,
    )


async def search_datasets(
    conn: AsyncConnection,
    principal: Principal,
    query: str,
    *,
    bbox: list[float] | None = None,
    kind: DatasetKind | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Resolve an informal name to datasets. `04-mcp-server.md` §4.2.

    Trigram similarity rather than prefix matching, because this is the tool
    that turns "our fault picks" into an id and it has to be forgiving of
    partial and misspelled terms. `pg_trgm` and the GIN index on
    `dataset.name` exist for this query.

    The bbox filter compares plain floats — `bbox_4326` is four doubles, not a
    geometry, because the control plane has no PostGIS (`adr/0002`).
    """
    limit = max(1, min(limit, 50))
    filters = ["deleted_at IS NULL", "(name % :q OR description ILIKE :like)"]
    params: dict[str, Any] = {"q": query, "like": f"%{query}%", "limit": limit}
    if kind is not None:
        filters.append("kind = :kind")
        params["kind"] = kind.value
    if bbox is not None:
        # Overlap, not containment: a geologist searching an area of interest
        # wants everything touching it, not only what fits inside it.
        filters.append(
            "bbox_4326 IS NOT NULL AND bbox_4326[1] <= :east AND bbox_4326[3] >= :west "
            "AND bbox_4326[2] <= :north AND bbox_4326[4] >= :south"
        )
        params |= {"west": bbox[0], "south": bbox[1], "east": bbox[2], "north": bbox[3]}

    rows = await conn.execute(
        text(
            f"""
            SELECT id, name, kind, geometry_kind, feature_count, bbox_4326,
                   sync_state, synced_at, caption,
                   similarity(name, :q) AS score
            FROM dataset WHERE {" AND ".join(filters)}
            ORDER BY score DESC, updated_at DESC
            LIMIT :limit
            """
        ),
        params,
    )
    return [dict(row._mapping) for row in rows]


async def get_dataset(
    conn: AsyncConnection, principal: Principal, dataset_id: UUID
) -> dict[str, Any]:
    """Full detail for one dataset, with an explicit viewer check."""
    await load_and_require(conn, "dataset", dataset_id, principal, Permission.VIEWER)

    result = await conn.execute(
        text(
            """
            SELECT d.*, u.display_name AS owner_name
            FROM dataset d JOIN app_user u ON u.id = d.owner_user_id
            WHERE d.id = :id
            """
        ),
        {"id": dataset_id},
    )
    row = result.one()
    detail = dict(row._mapping)

    lineage = await conn.execute(
        text(
            """
            SELECT operation, parameters, input_dataset_ids, webmap_geo_version,
                   created_at
            FROM lineage WHERE output_dataset_id = :id
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"id": dataset_id},
    )
    lineage_row = lineage.one_or_none()
    detail["lineage"] = dict(lineage_row._mapping) if lineage_row else None
    return detail


async def resolve_feature_object(
    conn: AsyncConnection, principal: Principal, dataset_id: UUID
) -> tuple[str, int]:
    """Return `(parquet_key, version)` after an explicit permission check.

    **This is the single enforcement point for feature content.** RLS protects
    the registry row; it has no reach into object storage, so nothing else
    stands between a principal and the bytes (`02-data-model.md` §4.1).

    Every tile, export, and feature read goes through here. Reading
    `parquet_key` off a row loaded some other way is the bug this function
    exists to prevent, and it is the review point `12-roadmap.md` Phase 1
    calls out.
    """
    await load_and_require(conn, "dataset", dataset_id, principal, Permission.VIEWER)

    result = await conn.execute(
        text("SELECT parquet_key, version, kind FROM dataset WHERE id = :id"),
        {"id": dataset_id},
    )
    row = result.one()
    if row.parquet_key is None:
        raise NotFound(
            f"Dataset {dataset_id} is a {row.kind} and has no feature object. "
            f"Grids are stored as COGs — use resolve_grid_object instead."
        )
    return str(row.parquet_key), int(row.version)


async def resolve_grid_object(
    conn: AsyncConnection, principal: Principal, dataset_id: UUID
) -> str:
    """Return the COG key after an explicit permission check.

    The raster counterpart of `resolve_feature_object`, and the same rule
    applies: the API is the only thing standing between a principal and the
    object.
    """
    await load_and_require(conn, "dataset", dataset_id, principal, Permission.VIEWER)

    result = await conn.execute(
        text("SELECT cog_key, kind FROM dataset WHERE id = :id"), {"id": dataset_id}
    )
    row = result.one()
    if row.cog_key is None:
        raise NotFound(
            f"Dataset {dataset_id} is a {row.kind} and has no grid object. "
            f"Vector layers are stored as GeoParquet — use "
            f"resolve_feature_object instead."
        )
    return str(row.cog_key)


async def create_dataset(
    conn: AsyncConnection,
    principal: Principal,
    *,
    name: str,
    kind: DatasetKind,
    storage_srid: int,
    connector: str,
    project_id: UUID | None = None,
    geometry_kind: GeometryKind | None = None,
    parquet_key: str | None = None,
    cog_key: str | None = None,
    feature_count: int | None = None,
    bbox_4326: list[float] | None = None,
    attribute_schema: list[dict[str, Any]] | None = None,
    owner_team_id: UUID | None = None,
    visibility: Visibility = Visibility.TEAM,
    caption: str | None = None,
    description: str | None = None,
    source_uri: str | None = None,
    source_checksum: str | None = None,
) -> UUID:
    """Register a dataset.

    `owner_user_id` is the principal, always. The INSERT policy enforces that
    at the database too, so a request that tries to create an object owned by
    someone else fails twice.
    """
    result = await conn.execute(
        text(
            """
            INSERT INTO dataset (
                project_id, name, description, kind, geometry_kind, connector,
                source_uri, source_checksum, sync_state, storage_srid,
                bbox_4326, parquet_key, version, feature_count,
                attribute_schema, cog_key, caption,
                owner_user_id, owner_team_id, visibility)
            VALUES (
                :project_id, :name, :description, :kind, :geometry_kind,
                :connector, :source_uri, :source_checksum, 'ready',
                :storage_srid, :bbox_4326, :parquet_key, 1, :feature_count,
                CAST(:attribute_schema AS jsonb), :cog_key, :caption,
                :owner_user_id, :owner_team_id, :visibility)
            RETURNING id
            """
        ),
        {
            "project_id": project_id,
            "name": name,
            "description": description,
            "kind": kind.value,
            "geometry_kind": geometry_kind.value if geometry_kind else None,
            "connector": connector,
            "source_uri": source_uri,
            "source_checksum": source_checksum,
            "storage_srid": storage_srid,
            "bbox_4326": bbox_4326,
            "parquet_key": parquet_key,
            "feature_count": feature_count,
            "attribute_schema": (json.dumps(attribute_schema) if attribute_schema else None),
            "cog_key": cog_key,
            "caption": caption,
            "owner_user_id": principal.user_id,
            "owner_team_id": owner_team_id,
            "visibility": visibility.value,
        },
    )
    dataset_id = UUID(str(result.scalar_one()))

    if parquet_key is not None:
        await conn.execute(
            text(
                """
                INSERT INTO dataset_version (
                    dataset_id, version, parquet_key, feature_count, created_by)
                VALUES (:id, 1, :key, :count, :by)
                """
            ),
            {
                "id": dataset_id,
                "key": parquet_key,
                "count": feature_count,
                "by": principal.user_id,
            },
        )

    await record(
        conn,
        action=AuditAction.DATASET_CREATED,
        principal=principal,
        object_type="dataset",
        object_id=dataset_id,
        detail={"name": name, "kind": kind.value, "connector": connector},
    )
    return dataset_id


async def update_dataset(
    conn: AsyncConnection,
    principal: Principal,
    dataset_id: UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    visibility: Visibility | None = None,
    caption: str | None = None,
) -> None:
    """Update dataset metadata. Requires editor.

    Visibility changes are an editor operation rather than owner-only, which
    is a deliberate reading of `03` §3.3: it lists delete, grant management,
    and ownership transfer as owner-only and does not list visibility. An
    editor can already read and change the content.
    """
    await load_and_require(conn, "dataset", dataset_id, principal, Permission.EDITOR)

    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if visibility is not None:
        fields["visibility"] = visibility.value
    if caption is not None:
        fields["caption"] = caption
    if not fields:
        return

    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    await conn.execute(
        text(f"UPDATE dataset SET {assignments}, updated_at = now() WHERE id = :id"),
        {**fields, "id": dataset_id},
    )
    await record(
        conn,
        action=AuditAction.DATASET_UPDATED,
        principal=principal,
        object_type="dataset",
        object_id=dataset_id,
        detail={"fields": sorted(fields)},
    )


async def soft_delete_dataset(
    conn: AsyncConnection, principal: Principal, dataset_id: UUID
) -> None:
    """Soft delete. Owner-only, and reversible for 30 days.

    Deleted datasets stay resolvable by lineage so provenance chains do not
    break (`03-auth-security.md` §8), which is why this sets `deleted_at`
    rather than issuing a DELETE — and why the RLS policies deliberately do
    not filter on it.
    """
    obj = await load_ownable(conn, "dataset", dataset_id)
    await require_owner(DbGrantStore(conn, "dataset"), principal, obj)

    await conn.execute(
        text("UPDATE dataset SET deleted_at = now(), updated_at = now() WHERE id = :id"),
        {"id": dataset_id},
    )
    await record(
        conn,
        action=AuditAction.DATASET_DELETED,
        principal=principal,
        object_type="dataset",
        object_id=dataset_id,
        detail={"name": obj.name, "soft": True, "at": datetime.now(UTC).isoformat()},
    )
