"""Render endpoints. `06-rendering.md` §5–§6, `04-mcp-server.md` §6.1.

The API owns the three things the render service deliberately does not: who may
see what, what the style is, and what the image means.

**One authorization code path.** The style is assembled through
`get_dataset`, the same permission path the tile endpoints use, and the tile
tokens it mints are the caller's own. A render therefore cannot reach a layer
its requester could not — which is the property that lets the render worker be
isolated from the database entirely (`03-auth-security.md` §7.3).
"""

from __future__ import annotations

import base64
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Query, Request, Response
from pydantic import Field

from webmap_api.dependencies import AppSettings, CurrentPrincipal, ScopedConn
from webmap_core.exceptions import NotFound
from webmap_core.logging import get_logger
from webmap_core.models import WebMapModel
from webmap_core.services import datasets as dataset_service
from webmap_core.services import renders as render_service
from webmap_core.services.style_builder import bounds_of, build_style
from webmap_core.signing import mint_tile_token
from webmap_io.storage import StorageConfig, get_bytes, put_bytes
from webmap_io.storage import client as storage_client

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/renders", tags=["renders"])

#: A render fetches many tiles through the browser, each of which needs the
#: token to still be valid. Longer than the interactive TTL for that reason,
#: and no longer than it needs to be.
RENDER_TOKEN_TTL_SECONDS = 300


class LayerRequest(WebMapModel):
    dataset_id: UUID
    symbology: dict[str, Any] | None = None
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    colormap: str | None = None


class CreateRender(WebMapModel):
    layers: list[LayerRequest] = Field(min_length=1)
    title: str | None = None
    subtitle: str | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    size: str = "slide_full"
    show_legend: bool = True
    show_scale_bar: bool = True
    show_north_arrow: bool = True
    transparent_background: bool = False
    session_id: UUID | None = None


@router.post("", status_code=201)
async def create_render(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    settings: AppSettings,
    request: Request,
    body: CreateRender,
) -> dict[str, Any]:
    """Render a map and persist it.

    Returns the preview inline and the render id. `adr/0006`: the preview is
    what reaches Claude in an MCP response, the master is what goes on a slide,
    and inlining a 2560x1440 master on every render would fill a conversation
    with a dozen of them.
    """
    details = [
        await dataset_service.get_dataset(conn, principal, layer.dataset_id)
        for layer in body.layers
    ]

    secret = settings.tile_token_secret.get_secret_value().encode()

    def token_for(dataset_id: UUID) -> str:
        # The caller's own token, minted after the permission check above. A
        # service credential here would be a second path to the data that the
        # audit log could not attribute to a person.
        return mint_tile_token(
            dataset_id, principal.user_id, secret, ttl_seconds=RENDER_TOKEN_TTL_SECONDS
        )

    style = await build_style(
        conn,
        principal,
        [layer.model_dump(mode="json") for layer in body.layers],
        tiles_base=f"{settings.internal_api_base}/api/v1",
        titiler_base=f"{settings.internal_api_base}/api/v1",
        static_base=settings.internal_static,
        token_for=token_for,
    )

    bounds = tuple(body.bbox) if body.bbox else bounds_of(details)
    if bounds is None:
        raise NotFound(
            "None of these datasets has a known extent, so there is nowhere to "
            "point the map. Supply bbox explicitly, or re-import the datasets so "
            "their bounds are recorded."
        )

    metadata = _metadata_for(details, body, bounds)
    overlay = _overlay_for(body, metadata, bounds)

    payload = {
        "style": style,
        "size_preset": body.size,
        "bounds": list(bounds),
        "transparent": body.transparent_background,
        **({"overlay": overlay} if overlay else {}),
    }

    async with httpx.AsyncClient(timeout=90.0) as http:
        upstream = await http.post(
            f"{settings.render_url}/render",
            json=payload,
            headers={"Authorization": request.headers.get("authorization", "")},
        )
    if upstream.status_code >= 400:
        detail = upstream.json().get("detail", upstream.text[:200])
        log.warning("render_service_error", status=upstream.status_code)
        raise NotFound(
            f"The render service could not draw this map: {detail}. The datasets "
            f"are registered, so this is a rendering problem rather than a "
            f"permission one."
        )

    result = upstream.json()
    image = base64.b64decode(result["image_base64"])
    preview = base64.b64decode(result["preview_base64"])

    s3 = _storage(settings)
    stem = uuid4().hex
    image_key = f"renders/{stem}.png"
    preview_key = f"renders/{stem}-preview.png"
    put_bytes(s3, settings.s3_bucket, image_key, image, content_type="image/png")
    put_bytes(s3, settings.s3_bucket, preview_key, preview, content_type="image/png")

    render_id = await render_service.create_render(
        conn,
        principal,
        image_key=image_key,
        preview_key=preview_key,
        width=result["width"],
        height=result["height"],
        scale_factor=2 if body.size != "thumbnail" else 1,
        size_preset=body.size,
        metadata=metadata,
        session_id=body.session_id,
        caption=render_service.caption_for(metadata),
        failed_requests=result["failed_requests"],
    )

    return {
        "id": str(render_id),
        "width": result["width"],
        "height": result["height"],
        "preview_base64": result["preview_base64"],
        "metadata": metadata,
        "caption": render_service.caption_for(metadata),
        # Surfaced, not swallowed: a map with a hole in it looks like sparse
        # data (`06` §5.1).
        "failed_requests": result["failed_requests"],
    }


@router.get("/{render_id}")
async def get_render(
    principal: CurrentPrincipal, conn: ScopedConn, render_id: UUID
) -> dict[str, Any]:
    detail = await render_service.get_render(conn, principal, render_id)
    return {
        "id": str(detail["id"]),
        "width": detail["width"],
        "height": detail["height"],
        "size_preset": detail["size_preset"],
        "caption": detail["caption"],
        "metadata": detail["metadata"],
        "failed_requests": detail["failed_requests"],
        "created_at": detail["created_at"],
    }


@router.get("/{render_id}/image")
async def get_render_image(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    settings: AppSettings,
    render_id: UUID,
    size: Annotated[str, Query(pattern="^(master|preview)$")] = "preview",
) -> Response:
    """The image bytes.

    `size=master` is the full-resolution artifact for a slide; `preview` is the
    downsampled copy. Two artifacts from one screenshot (`adr/0006`).
    """
    detail = await render_service.get_render(conn, principal, render_id)
    key = detail["image_key"] if size == "master" else detail["preview_key"]
    if not key:
        raise NotFound(
            f"Render {render_id} has no {size} image recorded. Re-render the map "
            f"to produce one."
        )

    return Response(
        content=get_bytes(_storage(settings), settings.s3_bucket, key),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("")
async def list_renders(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    session_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    items = await render_service.list_renders(
        conn, principal, session_id=session_id, limit=limit
    )
    return {"items": items}


def _storage(settings: Any) -> Any:
    return storage_client(
        StorageConfig(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
            region=settings.s3_region,
        )
    )


def _metadata_for(
    details: list[dict[str, Any]],
    body: CreateRender,
    bounds: tuple[float, ...],
) -> dict[str, Any]:
    """What the render means, from the source data.

    `04-mcp-server.md` §6.1 tells Claude to write captions from this and never
    to state a value range it did not receive. That is only honest if every
    number here came from the dataset registry rather than from the image.
    """
    primary = details[0]
    value_range: dict[str, Any] | None = None
    for detail in details:
        if detail.get("value_min") is not None:
            value_range = {
                "min": float(detail["value_min"]),
                "max": float(detail["value_max"]),
                "unit": detail.get("value_unit"),
            }
            break

    return {
        "title": body.title,
        "layers": [
            {
                "id": str(detail["id"]),
                "name": detail["name"],
                "kind": detail["kind"],
                "feature_count": detail.get("feature_count"),
                "version": detail.get("version"),
            }
            for detail in details
        ],
        "value_range": value_range,
        "crs": {
            "srid": primary.get("storage_srid"),
            "name": primary.get("crs_name"),
        },
        "extent_4326": list(bounds),
        "vintage": _isoformat(primary.get("data_vintage")),
        # Populated from the lineage record once Phase 4 produces one. Named
        # here so a caption template does not have to change later.
        "method": None,
        "grid": None,
    }


def _overlay_for(
    body: CreateRender, metadata: dict[str, Any], bounds: tuple[float, ...]
) -> dict[str, Any] | None:
    """The overlay spec handed to the shell.

    A legend on a map going into a presentation is mandatory (`06` §9) — the
    default is on, and turning it off is a deliberate act.
    """
    overlay: dict[str, Any] = {}

    if body.title:
        overlay["title"] = {
            "text": body.title,
            **({"subtitle": body.subtitle} if body.subtitle else {}),
        }
    if body.show_scale_bar:
        # Latitude of the extent's centre: Web Mercator scale varies with it,
        # and a bar computed at the equator is 18% wrong in the Midland Basin.
        overlay["scaleBar"] = {
            "latitude": (bounds[1] + bounds[3]) / 2,
            "zoom": 9,
            "unit": "imperial",
        }
    if body.show_north_arrow:
        overlay["northArrow"] = {"bearing": 0}

    vintage = metadata.get("vintage")
    if vintage:
        # `06` §9: cheap, and means the map defends itself once it is out of
        # our hands.
        overlay["provenance"] = f"WebMap · data vintage {vintage}"

    return overlay or None


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


__all__ = ["router"]
