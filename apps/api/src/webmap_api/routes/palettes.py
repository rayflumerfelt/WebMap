"""Palette endpoints. `08-styling-palettes.md` §5.1.

Import takes the file's **text**, not a multipart upload: a palette is a few
kilobytes of ASCII, the browser control has already read it to detect the
format, and a JSON body keeps this on the same error path as every other
endpoint. `webmap_api.routes.uploads` exists for the multipart case, and a
palette is not it.
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Path, Query, Response, status

from webmap_api.dependencies import CurrentPrincipal, ScopedConn
from webmap_core.permissions import Visibility
from webmap_core.services import palettes as service
from webmap_core.style.palette_io import FORMATS

router = APIRouter(prefix="/api/v1/palettes", tags=["palettes"])

PaletteFormat = Literal["clr", "cpt", "xml", "json"]


@router.get("")
async def list_palettes(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Palettes the caller can see, stops included.

    The styling UI draws a preview strip per row, so a list without stops means
    one request per palette when the picker opens.
    """
    items = await service.list_palettes(conn, principal, limit=limit)
    return {"count": len(items), "items": items}


@router.get("/{palette_id}")
async def get_palette(
    principal: CurrentPrincipal, conn: ScopedConn, palette_id: UUID
) -> dict[str, Any]:
    return dict(await service.get_palette(conn, principal, palette_id))


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_palette(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    name: Annotated[str, Body(min_length=1, max_length=120)],
    format: Annotated[  # noqa: A002 — the field is `format` in the request body
        PaletteFormat,
        Body(
            description=(
                f"One of {', '.join(FORMATS)}. Passed rather than sniffed: a "
                f".clr and a .cpt overlap in shape, and a wrong guess makes a "
                f"palette with plausible colours in the wrong places."
            )
        ),
    ],
    text: Annotated[str, Body(description="The file's contents.")],
    visibility: Annotated[Visibility, Body()] = Visibility.PRIVATE,
    owner_team_id: Annotated[UUID | None, Body()] = None,
) -> dict[str, Any]:
    """Parse a Surfer, GMT, QGIS or WebMap palette file and store it.

    Private by default: an imported palette is somebody else's work until the
    person importing it decides otherwise.
    """
    palette_id, palette = await service.import_palette(
        conn,
        principal,
        text_content=text,
        fmt=format,
        name=name,
        visibility=visibility,
        owner_team_id=owner_team_id,
    )
    return {"palette_id": str(palette_id), "palette": dict(palette)}


@router.get("/{palette_id}/export.{fmt}")
async def export_palette(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    palette_id: UUID,
    fmt: Annotated[
        Literal["clr", "cpt", "json"],
        Path(description="QGIS ramps are read but not written — see the service."),
    ],
) -> Response:
    """The palette as a downloadable file.

    `text/plain` with a filename rather than a JSON envelope: what the caller
    wants is the bytes to hand to Surfer or GMT, and wrapping them would mean
    every consumer unwraps them again.
    """
    body = await service.export_palette(conn, principal, palette_id, fmt)
    name = (await service.get_palette(conn, principal, palette_id))["name"]
    safe = "".join(char if char.isalnum() or char in "-_ " else "_" for char in name).strip()
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe or "palette"}.{fmt}"'},
    )
