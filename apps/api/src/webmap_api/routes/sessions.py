"""Map session endpoints. `02-data-model.md` §3.8, `04-mcp-server.md` §7.

The short code is the public handle: `/s/k3n8fq` is what gets pasted into a
chat, a ticket, or a slide. It resolves through the same permission path as
everything else — a link is a locator, never a credential — so a session
someone cannot see returns 404 whether or not it exists.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import Field

from webmap_api.dependencies import CurrentPrincipal, ScopedConn
from webmap_core.models import WebMapModel
from webmap_core.permissions import Channel, Visibility
from webmap_core.services import sessions as service

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


class LayerRef(WebMapModel):
    """One layer of a session: a reference plus how to draw it.

    Never the data itself (`02` §3.8) — a session that embedded its layers
    would balloon, go stale, and become a second copy of data whose access the
    registry is supposed to control.
    """

    dataset_id: UUID
    style_template_id: UUID | None = None
    symbology_override: dict[str, Any] | None = None
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    visible: bool = True
    z: int | None = None


class CreateSession(WebMapModel):
    layers: list[LayerRef] = Field(min_length=1)
    view: dict[str, Any]
    name: str | None = None
    project_id: UUID | None = None
    visibility: Visibility = Visibility.TEAM
    owner_team_id: UUID | None = None


class UpdateSession(WebMapModel):
    """A partial update. Omitted fields are left alone.

    `expected_updated_at` is optimistic concurrency. It matters more here than
    elsewhere because a session open in two tabs — or in a tab and in Claude at
    once — is the normal case, not the exception.
    """

    layers: list[LayerRef] | None = None
    view: dict[str, Any] | None = None
    name: str | None = None
    expected_updated_at: datetime | None = None


@router.post("", status_code=201)
async def create_session(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    request: Request,
    body: CreateSession,
) -> dict[str, Any]:
    result = await service.create_session(
        conn,
        principal,
        layers=[layer.model_dump(mode="json") for layer in body.layers],
        view=body.view,
        name=body.name,
        project_id=body.project_id,
        visibility=body.visibility,
        owner_team_id=body.owner_team_id,
        # Set from the channel rather than accepted from the body: a client
        # claiming "Claude made this" would make the flag useless for the
        # conversational continuity it exists to support.
        created_by_claude=principal.channel is Channel.CLAUDE,
    )
    return {
        "id": str(result["id"]),
        "short_code": result["short_code"],
        "url": _session_url(request, result["short_code"]),
        "layer_count": result["layer_count"],
    }


@router.get("")
async def list_sessions(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items = await service.list_sessions(conn, principal, limit=limit)
    return {"items": items}


@router.get("/{key}")
async def get_session(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    key: str,
) -> dict[str, Any]:
    """Load by short code or by id — the same session by two names.

    One endpoint rather than two because callers have one or the other and
    should not have to know which: Claude holds the id it was returned, the
    browser holds the code from the URL bar.
    """
    return await service.load_session(conn, principal, **_key_for(key))


@router.patch("/{session_id}")
async def update_session(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    session_id: UUID,
    body: UpdateSession,
    autosave: Annotated[
        bool,
        Query(description="Suppress the audit record. For the browser's periodic save."),
    ] = False,
) -> dict[str, Any]:
    return await service.update_session(
        conn,
        principal,
        session_id,
        layers=(
            [layer.model_dump(mode="json") for layer in body.layers]
            if body.layers is not None
            else None
        ),
        view=body.view,
        name=body.name,
        expected_updated_at=body.expected_updated_at,
        autosave=autosave,
    )


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    principal: CurrentPrincipal, conn: ScopedConn, session_id: UUID
) -> None:
    """Soft delete, 30 days (CLAUDE.md §3.4).

    A session link lives in a chat transcript and in people's bookmarks; a hard
    delete would break those with no way back.
    """
    await service.soft_delete_session(conn, principal, session_id)


def _key_for(key: str) -> dict[str, Any]:
    try:
        return {"session_id": UUID(key)}
    except ValueError:
        return {"short_code": key}


def _session_url(request: Request, short_code: str) -> str:
    """The link a user clicks.

    Built from the configured public base URL rather than from the request's
    Host header: the API sits behind a proxy, and a Host-derived link would
    hand out an internal hostname the user's browser cannot resolve.
    """
    base = str(request.app.state.settings.public_base_url).rstrip("/")
    return f"{base}/s/{short_code}"


__all__ = ["router"]
