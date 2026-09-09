"""Render persistence. `02-data-model.md` §3.9, `04-mcp-server.md` §6.1.

"Persisted image artifacts. Claude references these by ID across turns and
across slides."

That is the whole reason renders are rows rather than transient bytes: a deck
is built over several turns, and re-rendering the same map for each slide would
give slightly different images — a different label placement, a different tile
arriving in a different order — for no reason a reader could explain.

**The metadata is the point as much as the image.** `04` §6.1 tells Claude to
write captions from it and *not* to describe the image from its pixels or state
a value range it did not receive. That instruction is only honest if the range,
units, CRS and vintage stored here came from the source data — so they are read
from the dataset registry at render time, never inferred.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import NotFound
from webmap_core.permissions import Permission, Principal, Visibility
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.ownable import load_and_require, resolve_owner_team


async def create_render(
    conn: AsyncConnection,
    principal: Principal,
    *,
    image_key: str,
    preview_key: str,
    width: int,
    height: int,
    scale_factor: int,
    size_preset: str,
    metadata: dict[str, Any],
    session_id: UUID | None = None,
    job_id: UUID | None = None,
    caption: str | None = None,
    failed_requests: list[dict[str, Any]] | None = None,
    owner_team_id: UUID | None = None,
    visibility: Visibility = Visibility.TEAM,
) -> UUID:
    """Record a render.

    `failed_requests` is stored rather than logged and forgotten
    (`06-rendering.md` §5.1): a map with a hole in it looks like sparse data,
    and six weeks later the only way to tell whether the tile server was down
    is this column.
    """
    owner_team_id = resolve_owner_team(principal, visibility, owner_team_id)

    result = await conn.execute(
        text(
            """
            INSERT INTO render (
                session_id, job_id, image_key, width, height, scale_factor,
                size_preset, metadata, caption, owner_user_id, owner_team_id,
                visibility)
            VALUES (
                :session_id, :job_id, :image_key, :width, :height,
                :scale_factor, :size_preset, CAST(:metadata AS jsonb), :caption,
                :owner_user_id, :owner_team_id, CAST(:visibility AS visibility_t))
            RETURNING id
            """
        ),
        {
            "session_id": session_id,
            "job_id": job_id,
            "image_key": image_key,
            "width": width,
            "height": height,
            "scale_factor": scale_factor,
            "size_preset": size_preset,
            "metadata": json.dumps(
                {
                    **metadata,
                    "preview_key": preview_key,
                    "failed_requests": failed_requests or [],
                }
            ),
            "caption": caption,
            "owner_user_id": principal.user_id,
            "owner_team_id": owner_team_id,
            "visibility": visibility.value,
        },
    )
    render_id = UUID(str(result.scalar_one()))

    await record(
        conn,
        action=AuditAction.RENDER_CREATED,
        principal=principal,
        object_type="render",
        object_id=render_id,
        detail={
            "size_preset": size_preset,
            "layer_count": len(metadata.get("layers", [])),
            "incomplete": bool(failed_requests),
        },
    )
    return render_id


async def get_render(
    conn: AsyncConnection, principal: Principal, render_id: UUID
) -> dict[str, Any]:
    """One render, with everything needed to write a caption about it."""
    await load_and_require(conn, "render", render_id, principal, Permission.VIEWER)

    result = await conn.execute(
        text(
            """
            SELECT id, session_id, job_id, image_key, width, height,
                   scale_factor, size_preset, metadata, caption, created_at
            FROM render WHERE id = :id AND deleted_at IS NULL
            """
        ),
        {"id": render_id},
    )
    row = result.one_or_none()
    if row is None:
        raise NotFound(
            f"No render {render_id}. It may have been deleted, or it may belong "
            f"to someone who has not shared it with you."
        )

    detail = dict(row._mapping)
    metadata = detail.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    detail["metadata"] = metadata
    detail["preview_key"] = metadata.get("preview_key")
    detail["failed_requests"] = metadata.get("failed_requests", [])
    return detail


async def list_renders(
    conn: AsyncConnection,
    principal: Principal,
    *,
    session_id: UUID | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Recent renders, newest first. Scoped to a session when given."""
    clause = "AND session_id = :session_id" if session_id else ""
    result = await conn.execute(
        text(
            f"""
            SELECT id, session_id, width, height, size_preset, caption, created_at
            FROM render
            WHERE deleted_at IS NULL {clause}
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {
            "limit": max(1, min(limit, 100)),
            **({"session_id": session_id} if session_id else {}),
        },
    )
    return [dict(row._mapping) for row in result]


def caption_for(metadata: dict[str, Any]) -> str:
    """A suggested caption, built from the metadata rather than the pixels.

    `04-mcp-server.md` §6.1 tells Claude to write captions from the metadata
    and never to state a value range it did not receive. Offering a correct one
    here is more reliable than asking and hoping — and it makes the shape of an
    acceptable caption explicit: what, where, how, and over what range.
    """
    layers = metadata.get("layers") or []
    primary = layers[0] if layers else {}
    name = primary.get("name", "Map")

    parts = [name]
    if metadata.get("extent_name"):
        parts.append(str(metadata["extent_name"]))

    sentence = ", ".join(parts) + "."

    method = metadata.get("method")
    if method:
        sentence += f" {method}."

    value_range = metadata.get("value_range")
    if value_range and value_range.get("min") is not None:
        unit = value_range.get("unit") or ""
        sentence += (
            f" Values {value_range['min']:g}–{value_range['max']:g}"
            f"{(' ' + unit) if unit else ''}."
        )

    if len(layers) > 1:
        others = ", ".join(str(layer.get("name")) for layer in layers[1:])
        sentence += f" With {others}."

    return sentence


__all__ = ["caption_for", "create_render", "get_render", "list_renders"]
