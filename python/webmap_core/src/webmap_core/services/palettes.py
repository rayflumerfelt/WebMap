"""Palettes as stored objects. `02` §3.6, `08-styling-palettes.md` §5.

Thin over `webmap_core.style.palette_io`: the parsing is there, this is the
part that checks who may write, records where the palette came from, and hands
back what the styling UI needs.

`source_format` is stored because "where did this ramp come from" is asked
often and is unrecoverable afterwards — a `.cpt` and a hand-built ramp with the
same stops are indistinguishable once the stops are all that is kept, and the
first is something a colleague can send you again.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.logging import get_logger
from webmap_core.permissions import Permission, Principal, Visibility
from webmap_core.services.capabilities import require_publish_scope
from webmap_core.services.ownable import load_and_require, resolve_owner_team
from webmap_core.style.palette import InvalidPalette, Palette
from webmap_core.style.palette_io import read_palette, write_palette

log = get_logger(__name__)

#: A palette file larger than this is not a palette. 256-slice `.cpt` files are
#: a few kilobytes; a megabyte of text is a mislabelled upload, and parsing it
#: to find that out is work nobody asked for.
MAX_TEXT_BYTES = 256 * 1024


async def import_palette(
    conn: AsyncConnection,
    principal: Principal,
    *,
    text_content: str,
    fmt: str,
    name: str,
    visibility: Visibility = Visibility.PRIVATE,
    owner_team_id: UUID | None = None,
) -> tuple[UUID, Palette]:
    """Parse a palette file and store it.

    Private by default, whatever the caller's usual habit: an imported palette
    is somebody else's work until the person importing it decides otherwise,
    and a `.cpt` from cpt-city published organisation-wide on arrival is a
    surprise rather than a convenience.
    """
    if len(text_content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise InvalidPalette(
            f"That file is {len(text_content) / 1024:.0f} kB (limit "
            f"{MAX_TEXT_BYTES // 1024} kB). A colour palette is a few kilobytes; "
            f"this is probably not one."
        )

    palette_id = uuid4()
    palette = read_palette(text_content, fmt, name=name, palette_id=str(palette_id))

    team = resolve_owner_team(principal, visibility, owner_team_id)
    await require_publish_scope(conn, principal, visibility, team)

    await conn.execute(
        text(
            """
            INSERT INTO palette (
                id, name, is_continuous, stops, interpolation, source_format,
                owner_user_id, owner_team_id, visibility)
            VALUES (
                :id, :name, :continuous, CAST(:stops AS JSONB), :interpolation,
                :source, :owner, :team, CAST(:visibility AS visibility_t))
            """
        ),
        {
            "id": palette_id,
            "name": name,
            "continuous": bool(palette["isContinuous"]),
            "stops": json.dumps(palette["stops"]),
            "interpolation": palette["interpolation"],
            "source": fmt.lower().lstrip("."),
            "owner": principal.user_id,
            "team": team,
            "visibility": visibility.value,
        },
    )
    log.info(
        "palette_imported",
        palette_id=str(palette_id),
        source_format=fmt,
        stops=len(palette["stops"]),
    )
    return palette_id, palette


async def get_palette(conn: AsyncConnection, principal: Principal, palette_id: UUID) -> Palette:
    await load_and_require(conn, "palette", palette_id, principal, Permission.VIEWER)
    row = (
        await conn.execute(
            text(
                "SELECT id, name, is_continuous, stops, interpolation "
                "FROM palette WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": palette_id},
        )
    ).one()

    stops = row.stops
    if isinstance(stops, str):
        stops = json.loads(stops)

    return {
        "id": str(row.id),
        "name": str(row.name),
        "isContinuous": bool(row.is_continuous),
        "interpolation": str(row.interpolation),
        "stops": stops,
    }


async def list_palettes(
    conn: AsyncConnection, principal: Principal, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Every palette the caller can see, with its stops.

    Stops included rather than fetched per palette: the styling UI draws a
    preview strip for each one in the picker, so a list without them means a
    request per row, and a deployment with forty palettes then opens the picker
    with forty requests.
    """
    result = await conn.execute(
        text(
            """
            SELECT id, name, is_continuous, stops, interpolation, source_format
            FROM palette
            WHERE deleted_at IS NULL
            ORDER BY name
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    palettes: list[dict[str, Any]] = []
    for row in result:
        stops = row.stops
        if isinstance(stops, str):
            stops = json.loads(stops)
        palettes.append(
            {
                "id": str(row.id),
                "name": str(row.name),
                "isContinuous": bool(row.is_continuous),
                "interpolation": str(row.interpolation),
                "stops": stops,
                "sourceFormat": row.source_format,
            }
        )
    return palettes


async def export_palette(
    conn: AsyncConnection, principal: Principal, palette_id: UUID, fmt: str
) -> str:
    """A stored palette as text in `fmt`.

    Viewer access is enough: exporting a ramp is reading it, and a palette that
    can be seen can be sampled colour by colour anyway.
    """
    palette = await get_palette(conn, principal, palette_id)
    return write_palette(palette, fmt)


__all__ = [
    "MAX_TEXT_BYTES",
    "export_palette",
    "get_palette",
    "import_palette",
    "list_palettes",
]
