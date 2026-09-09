"""Projects. `02-data-model.md` §3.4.

The container that fixes the analysis CRS and units. That is its whole reason
to exist: `02` §1 requires `analysis_srid` to be explicit and projected, never
inferred and never defaulted to 3857, and a project is where that decision is
recorded once instead of at every operation.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.models import Bbox, LengthUnit
from webmap_core.permissions import Permission, Principal, Visibility, require_owner
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.grants import DbGrantStore
from webmap_core.services.ownable import load_and_require, load_ownable, resolve_owner_team


def validate_analysis_srid(srid: int) -> LengthUnit:
    """Refuse a geographic CRS, and return the horizontal unit.

    `02` §1: interpolation, variogram estimation, distance, area, and
    buffering run only in a projected CRS. A variogram range in decimal
    degrees is meaningless and anisotropy in degrees is worse — and neither
    fails, they just produce a plausible wrong answer.

    Validated here, at the boundary that records the decision, so that every
    downstream operation can assume it.
    """
    from webmap_geo.crs import axis_units

    # `axis_units` raises NotProjected for a geographic CRS, with the message
    # that names the fix. Re-raising it as something vaguer would lose that.
    #
    # webmap_geo speaks a Literal and webmap_core a StrEnum of the same three
    # values. Converting here rather than sharing a type keeps webmap_geo free
    # of webmap_core, which adr/0003 requires.
    return LengthUnit(axis_units(srid))


async def create_project(
    conn: AsyncConnection,
    principal: Principal,
    *,
    slug: str,
    name: str,
    analysis_srid: int,
    vertical_unit: LengthUnit | None = None,
    depth_positive_down: bool = True,
    description: str | None = None,
    default_extent: Bbox | None = None,
    owner_team_id: UUID | None = None,
    visibility: Visibility = Visibility.TEAM,
) -> UUID:
    """Create a project.

    `horizontal_unit` is derived from the CRS rather than accepted from the
    caller: it is a property of `analysis_srid`, and letting the two disagree
    would mean a project claiming metres over a State Plane zone in feet.

    `vertical_unit` genuinely is independent — a grid can be feet-vertical on
    metres-horizontal (`02` §1 rule 4) — so it is asked for, and defaults to
    the horizontal unit only because that is the common case.
    """
    horizontal_unit = validate_analysis_srid(analysis_srid)
    owner_team_id = resolve_owner_team(principal, visibility, owner_team_id)

    result = await conn.execute(
        text(
            """
            INSERT INTO project (
                slug, name, description, analysis_srid, horizontal_unit,
                vertical_unit, depth_positive_down, default_extent,
                owner_user_id, owner_team_id, visibility)
            VALUES (
                :slug, :name, :description, :analysis_srid,
                CAST(:horizontal_unit AS length_unit_t),
                CAST(:vertical_unit AS length_unit_t),
                :depth_positive_down, :default_extent,
                :owner_user_id, :owner_team_id, CAST(:visibility AS visibility_t))
            RETURNING id
            """
        ),
        {
            "slug": slug,
            "name": name,
            "description": description,
            "analysis_srid": analysis_srid,
            "horizontal_unit": horizontal_unit,
            "vertical_unit": (vertical_unit or horizontal_unit),
            "depth_positive_down": depth_positive_down,
            "default_extent": default_extent,
            "owner_user_id": principal.user_id,
            "owner_team_id": owner_team_id,
            "visibility": visibility.value,
        },
    )
    project_id = UUID(str(result.scalar_one()))

    await record(
        conn,
        action=AuditAction.PROJECT_CREATED,
        principal=principal,
        object_type="project",
        object_id=project_id,
        detail={"slug": slug, "analysis_srid": analysis_srid},
    )
    return project_id


async def list_projects(
    conn: AsyncConnection, principal: Principal, *, limit: int = 50
) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(
            """
            SELECT id, slug, name, description, analysis_srid, horizontal_unit,
                   vertical_unit, depth_positive_down, default_extent,
                   visibility, updated_at
            FROM project WHERE deleted_at IS NULL
            ORDER BY name LIMIT :limit
            """
        ),
        {"limit": max(1, min(limit, 200))},
    )
    return [dict(row._mapping) for row in result]


async def get_project(
    conn: AsyncConnection, principal: Principal, project_id: UUID
) -> dict[str, Any]:
    await load_and_require(conn, "project", project_id, principal, Permission.VIEWER)

    result = await conn.execute(
        text(
            """
            SELECT p.*, u.display_name AS owner_name,
                   (SELECT count(*) FROM dataset d
                    WHERE d.project_id = p.id AND d.deleted_at IS NULL) AS dataset_count
            FROM project p JOIN app_user u ON u.id = p.owner_user_id
            WHERE p.id = :id
            """
        ),
        {"id": project_id},
    )
    detail = dict(result.one()._mapping)

    # The CRS definition, for the browser's cursor readout. Served rather than
    # looked up client-side so there is one source of truth for what the CRS
    # means — two EPSG tables eventually disagree about a datum shift, and the
    # disagreement stays invisible until someone checks a readout against a
    # well file.
    from webmap_geo.crs import crs_definition

    detail["crs_wkt"] = crs_definition(int(detail["analysis_srid"]))
    return detail


async def update_project(
    conn: AsyncConnection,
    principal: Principal,
    project_id: UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    default_extent: Bbox | None = None,
    visibility: Visibility | None = None,
) -> None:
    """Update a project. Requires editor.

    **`analysis_srid` is deliberately not updatable.** Changing it would
    silently reinterpret every grid, contour, and lineage record already
    produced under the project — the stored numbers would stay the same and
    mean something else. A project in the wrong CRS is replaced, not edited.
    """
    await load_and_require(conn, "project", project_id, principal, Permission.EDITOR)

    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if default_extent is not None:
        fields["default_extent"] = default_extent
    if visibility is not None:
        fields["visibility"] = visibility.value
    if not fields:
        return

    assignments = ", ".join(
        f"{k} = CAST(:{k} AS visibility_t)" if k == "visibility" else f"{k} = :{k}"
        for k in fields
    )
    await conn.execute(
        text(f"UPDATE project SET {assignments}, updated_at = now() WHERE id = :id"),
        {**fields, "id": project_id},
    )
    await record(
        conn,
        action=AuditAction.PROJECT_UPDATED,
        principal=principal,
        object_type="project",
        object_id=project_id,
        detail={"fields": sorted(fields)},
    )


async def soft_delete_project(
    conn: AsyncConnection, principal: Principal, project_id: UUID
) -> int:
    """Soft delete. Owner-only. Returns how many datasets were left orphaned.

    Datasets are *not* deleted with the project: `dataset.project_id` is
    `ON DELETE SET NULL`, and a dataset outliving its project is far less
    destructive than a cascade that removes a colleague's work because the
    container it happened to sit in was tidied away.
    """
    obj = await load_ownable(conn, "project", project_id)
    await require_owner(DbGrantStore(conn, "project"), principal, obj)

    orphaned = await conn.execute(
        text("SELECT count(*) FROM dataset WHERE project_id = :id AND deleted_at IS NULL"),
        {"id": project_id},
    )
    count = int(orphaned.scalar_one())

    await conn.execute(
        text("UPDATE project SET deleted_at = now(), updated_at = now() WHERE id = :id"),
        {"id": project_id},
    )
    await record(
        conn,
        action=AuditAction.PROJECT_DELETED,
        principal=principal,
        object_type="project",
        object_id=project_id,
        detail={"slug": obj.name, "datasets_retained": count},
    )
    return count
