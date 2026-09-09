"""Map sessions. `02-data-model.md` §3.8, `04-mcp-server.md` §7.

The saved state Claude creates and the browser loads. **The session ID is the
shared vocabulary between the conversation and the application** (`01` §5.2):
Claude opens one, the user edits in the browser, and Claude reads the same
session back to pick up what changed.

Two consequences follow from that, and they shape everything here.

**Sessions store dataset references, never copies.** A session that embedded
its layers' data would balloon and go stale, and — worse — would become a
second, unaudited copy of data whose access the registry controls. A layer is
a dataset id plus how to draw it.

**Layer visibility is resolved on load, per principal, every time.** The
person opening a session is often not the person who created it, and the two
may see different subsets of its layers. A session is therefore a *request* to
show some datasets, not a grant to see them: `load_session` drops the layers
the caller cannot read and says how many it dropped, rather than refusing the
whole session or — far worse — returning them.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.exceptions import LimitExceeded, NotFound, SessionError, VersionConflict
from webmap_core.logging import get_logger
from webmap_core.permissions import Permission, Principal, Visibility
from webmap_core.services.audit import AuditAction, record
from webmap_core.services.ownable import load_and_require, resolve_owner_team

log = get_logger(__name__)

#: The current shape of `layers` and `view`. Bumped when a stored session can
#: no longer be read by this code without translation; `_migrate_document`
#: below is where that translation goes. Sessions are long-lived and a link
#: pasted into a chat six months ago must still open.
SCHEMA_VERSION = 1

#: Unambiguous over a phone or in a slide: no 0/O, no 1/l/I. A short code is a
#: locator, not a credential — every read still goes through RLS and an
#: explicit permission check — but 32^6 is a large enough space that scanning
#: for other people's sessions is not a productive use of anyone's afternoon.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_CODE_LENGTH = 6
_CODE_ATTEMPTS = 8

#: A session is a working view, not a project. Past a few dozen layers the map
#: is unreadable and the style compile alone takes longer than the page load.
MAX_LAYERS = 50


def generate_short_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))


# --- layers -----------------------------------------------------------------


def normalise_layers(layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and normalise the layer list.

    Draw order is `z`, assigned from list position when absent, because a
    caller that has bothered to order a list means that order. Claude passes a
    bare list; the SPA passes explicit `z` after a drag.
    """
    if len(layers) > MAX_LAYERS:
        raise LimitExceeded(
            f"A session holds at most {MAX_LAYERS} layers; {len(layers)} were given. "
            f"Split the map, or drop the layers that are not being compared."
        )

    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, layer in enumerate(layers):
        raw_id = layer.get("dataset_id")
        if raw_id is None:
            raise SessionError(
                f"Layer {position} has no dataset_id. A session stores references to "
                f"registered datasets, never copies of their data — every layer needs "
                f"the id of a dataset that already exists."
            )
        try:
            dataset_id = str(UUID(str(raw_id)))
        except ValueError as exc:
            raise SessionError(
                f"Layer {position} has dataset_id {raw_id!r}, which is not a UUID. "
                f"Use the id from webmap_search_datasets, not the dataset's name."
            ) from exc

        if dataset_id in seen:
            # Two layers over one dataset is legitimate — a fault set drawn
            # once as lines and once as labels — but it is more often a
            # duplicated paste, and silently drawing the layer twice at the
            # same opacity looks like a rendering bug.
            raise SessionError(
                f"Dataset {dataset_id} appears twice in the layer list. To draw one "
                f"dataset two ways, give the second layer a distinct "
                f"symbology_override and a different id."
            )
        seen.add(dataset_id)

        normalised.append(
            {
                "dataset_id": dataset_id,
                "style_template_id": _optional_uuid(layer.get("style_template_id")),
                "symbology_override": layer.get("symbology_override"),
                "opacity": _clamped_opacity(layer.get("opacity", 1.0), position),
                "visible": bool(_present(layer.get("visible"), True)),
                # `.get(key, default)` does not fall back for a key that is
                # present and null — and the API model serialises an unset z
                # as exactly that. Absent and null both mean "use list order".
                "z": int(_present(layer.get("z"), position)),
            }
        )

    normalised.sort(key=lambda entry: (entry["z"], entry["dataset_id"]))
    return normalised


def _present(value: Any, fallback: Any) -> Any:
    """`value` unless it is None, in which case `fallback`.

    Distinct from `dict.get(key, default)`, which returns None for a key that
    exists and holds None — the shape a Pydantic model produces for an unset
    optional field, and therefore the shape every request from the API arrives
    in.
    """
    return fallback if value is None else value


def _optional_uuid(value: Any) -> str | None:
    if value is None:
        return None
    return str(UUID(str(value)))


def _clamped_opacity(value: Any, position: int) -> float:
    try:
        opacity = float(value)
    except (TypeError, ValueError) as exc:
        raise SessionError(
            f"Layer {position} has opacity {value!r}, which is not a number. "
            f"Opacity runs from 0 (invisible) to 1 (opaque)."
        ) from exc
    if not 0.0 <= opacity <= 1.0:
        raise SessionError(
            f"Layer {position} has opacity {opacity:g}, outside 0–1. A percentage "
            f"goes in as a fraction: 60% is 0.6."
        )
    return opacity


def normalise_view(view: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the stored view.

    Either a camera (`center`/`zoom`) or a `bbox`, per `02` §3.8. Both forms
    are kept rather than converting to one, because converting a bbox to a
    centre and zoom needs the viewport aspect ratio, which the server does not
    have and the browser does.
    """
    if not view:
        raise SessionError(
            "A session needs a view: either {'center': [lon, lat], 'zoom': n} or "
            "{'bbox': [west, south, east, north]}. Without one the map has nowhere "
            "to open."
        )

    if "bbox" in view:
        bbox = view["bbox"]
        if not isinstance(bbox, list | tuple) or len(bbox) != 4:
            raise SessionError(
                f"view.bbox must be four numbers [west, south, east, north]; got {bbox!r}."
            )
        west, south, east, north = (float(v) for v in bbox)
        if west >= east or south >= north:
            raise SessionError(
                f"view.bbox is empty or inverted: [{west:g}, {south:g}, {east:g}, "
                f"{north:g}]. Order is [west, south, east, north] in WGS84 degrees — "
                f"a swapped pair here puts the map in the wrong hemisphere."
            )
        return {"bbox": [west, south, east, north]}

    if "center" not in view or "zoom" not in view:
        raise SessionError(
            f"view must carry either 'bbox' or both 'center' and 'zoom'; got keys "
            f"{sorted(view)}."
        )

    center = view["center"]
    if not isinstance(center, list | tuple) or len(center) != 2:
        raise SessionError(f"view.center must be [longitude, latitude]; got {center!r}.")
    lon, lat = float(center[0]), float(center[1])
    if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
        # Longitude first, RFC 7946 order. A swap puts a Midland Basin session
        # at 31°N 102°E, in the Gobi — which renders happily and is wrong.
        raise SessionError(
            f"view.center is [{lon:g}, {lat:g}], outside valid longitude/latitude. "
            f"Order is [longitude, latitude] — a swap here is the usual cause."
        )

    normalised: dict[str, Any] = {
        "center": [lon, lat],
        "zoom": max(0.0, min(float(view["zoom"]), 24.0)),
    }
    if "bearing" in view:
        normalised["bearing"] = float(view["bearing"]) % 360.0
    if "pitch" in view:
        normalised["pitch"] = max(0.0, min(float(view["pitch"]), 85.0))
    return normalised


# --- create -----------------------------------------------------------------


async def create_session(
    conn: AsyncConnection,
    principal: Principal,
    *,
    layers: list[dict[str, Any]],
    view: dict[str, Any],
    name: str | None = None,
    project_id: UUID | None = None,
    created_by_claude: bool = False,
    owner_team_id: UUID | None = None,
    visibility: Visibility = Visibility.TEAM,
) -> dict[str, Any]:
    """Create a session and return its id and short code.

    Every referenced dataset is checked for viewer access **by the creating
    principal**, before the row is written. A session whose layers the creator
    cannot see is a mistake worth catching at the point it is made, when the
    caller still knows which dataset they meant — rather than at load, when a
    layer would simply be missing and nobody would know why.

    That check is not a grant. It is repeated per principal on every load.
    """
    normalised = normalise_layers(layers)
    if not normalised:
        raise SessionError(
            "A session needs at least one layer. To open an empty map, create the "
            "session once the user has chosen what to show."
        )
    for layer in normalised:
        # load_and_require raises NotFound for a dataset RLS hides, and
        # PermissionDenied naming the owner for one visible but not readable.
        # Both are better answers than a session that silently loses a layer.
        await load_and_require(
            conn, "dataset", UUID(layer["dataset_id"]), principal, Permission.VIEWER
        )

    if project_id is None:
        project_id = await _project_of(conn, [UUID(x["dataset_id"]) for x in normalised])

    owner_team_id = resolve_owner_team(principal, visibility, owner_team_id)
    document = {"layers": normalised, "view": normalise_view(view)}

    for attempt in range(_CODE_ATTEMPTS):
        code = generate_short_code()
        result = await conn.execute(
            text(
                """
                INSERT INTO map_session (
                    short_code, project_id, name, schema_version, layers, view,
                    created_by_claude, owner_user_id, owner_team_id, visibility)
                VALUES (
                    :short_code, :project_id, :name, :schema_version,
                    CAST(:layers AS jsonb), CAST(:view AS jsonb),
                    :created_by_claude, :owner_user_id, :owner_team_id,
                    CAST(:visibility AS visibility_t))
                ON CONFLICT (short_code) DO NOTHING
                RETURNING id
                """
            ),
            {
                "short_code": code,
                "project_id": project_id,
                "name": name,
                "schema_version": SCHEMA_VERSION,
                "layers": json.dumps(document["layers"]),
                "view": json.dumps(document["view"]),
                "created_by_claude": created_by_claude,
                "owner_user_id": principal.user_id,
                "owner_team_id": owner_team_id,
                "visibility": visibility.value,
            },
        )
        session_id = result.scalar_one_or_none()
        if session_id is not None:
            break
        # A collision at 32^6 means either extraordinary luck or a short code
        # space that has filled up. Retrying is right for the first; the
        # attempt cap is what stops the second spinning forever.
        log.info("session_short_code_collision", attempt=attempt)
    else:
        raise SessionError(
            f"Could not allocate a unique session code in {_CODE_ATTEMPTS} attempts. "
            f"This should not happen — if it recurs, the short code space needs to "
            f"grow (webmap_core.services.sessions._CODE_LENGTH)."
        )

    created = UUID(str(session_id))
    await record(
        conn,
        action=AuditAction.SESSION_CREATED,
        principal=principal,
        object_type="map_session",
        object_id=created,
        detail={
            "short_code": code,
            "layer_count": len(normalised),
            "created_by_claude": created_by_claude,
        },
    )
    return {"id": created, "short_code": code, "layer_count": len(normalised)}


async def _project_of(conn: AsyncConnection, dataset_ids: list[UUID]) -> UUID | None:
    """The project a session belongs to, inferred from its layers.

    A session's project is where its **analysis CRS** comes from, and without
    one the status bar cannot show cursor coordinates in anything but degrees
    — which is the readout a geologist checks constantly (`07` §5.2).

    Callers rarely have a project to pass: the MCP tools have no concept of one
    (`04` §7.1 takes layers and a bbox), and the browser's "open these datasets"
    path does not either. So it is inferred, and only when unambiguous. Layers
    spanning two projects leave it null rather than picking one, because the
    two may have different analysis CRSs and choosing silently would put the
    readout in the wrong frame — the exact failure `02` §1 exists to prevent.
    """
    result = await conn.execute(
        text(
            "SELECT DISTINCT project_id FROM dataset "
            "WHERE id = ANY(:ids) AND project_id IS NOT NULL"
        ),
        {"ids": dataset_ids},
    )
    projects = [row[0] for row in result]
    if len(projects) == 1:
        return UUID(str(projects[0]))
    if len(projects) > 1:
        log.info("session_project_ambiguous", project_count=len(projects))
    return None


# --- read -------------------------------------------------------------------


async def load_session(
    conn: AsyncConnection,
    principal: Principal,
    *,
    short_code: str | None = None,
    session_id: UUID | None = None,
) -> dict[str, Any]:
    """Load a session, resolving each layer against *this* principal.

    Layers the caller cannot read are dropped and counted, not returned and
    not fatal. Three reasons, in order of importance:

    1. Returning them would leak the existence, name, and styling of datasets
       the caller has no access to — the session document would become a way
       around the registry's permissions.
    2. Refusing the whole session would make one restricted layer break a map
       that is otherwise entirely shareable, which is the common case when a
       session crosses teams.
    3. Silently dropping them would leave the recipient wondering why the map
       looks wrong. Hence `hidden_layer_count`, which the SPA shows as a
       notice naming who to ask.
    """
    row = await _fetch(conn, short_code=short_code, session_id=session_id)
    document = _migrate_document(dict(row))

    visible: list[dict[str, Any]] = []
    hidden = 0
    for layer in document["layers"]:
        dataset = await _readable_dataset(conn, UUID(layer["dataset_id"]))
        if dataset is None:
            hidden += 1
            continue
        visible.append({**layer, "dataset": dataset})

    if hidden:
        log.info(
            "session_layers_hidden",
            session_id=str(row["id"]),
            hidden=hidden,
            user_id=str(principal.user_id),
        )

    return {
        "id": row["id"],
        "short_code": row["short_code"],
        "name": row["name"],
        "project_id": row["project_id"],
        "schema_version": SCHEMA_VERSION,
        "layers": visible,
        "view": document["view"],
        "created_by_claude": row["created_by_claude"],
        "owner_user_id": row["owner_user_id"],
        "visibility": row["visibility"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "expires_at": row["expires_at"],
        "hidden_layer_count": hidden,
    }


async def _fetch(
    conn: AsyncConnection, *, short_code: str | None, session_id: UUID | None
) -> dict[str, Any]:
    if not short_code and not session_id:
        raise ValueError("load_session needs a short_code or a session_id")

    clause = "short_code = :key" if short_code else "id = :key"
    result = await conn.execute(
        text(
            f"""
            SELECT id, short_code, project_id, name, schema_version, layers, view,
                   created_by_claude, owner_user_id, owner_team_id, visibility,
                   created_at, updated_at, expires_at
            FROM map_session
            WHERE {clause} AND deleted_at IS NULL
            """
        ),
        {"key": short_code or session_id},
    )
    row = result.one_or_none()
    if row is None:
        # RLS may have hidden it, it may not exist, or it may be deleted. One
        # answer for all three: distinguishing them would confirm that a given
        # short code belongs to someone.
        raise NotFound(
            f"No session {short_code or session_id}. The link may be wrong, the "
            f"session may have been deleted, or it may belong to someone who has "
            f"not shared it with you."
        )

    mapping = dict(row._mapping)
    expires_at = mapping.get("expires_at")
    if expires_at is not None:
        from datetime import UTC, datetime

        if expires_at <= datetime.now(UTC):
            raise NotFound(
                f"Session {mapping['short_code']} expired on "
                f"{expires_at:%Y-%m-%d}. Ask Claude to open a new one — the "
                f"datasets it referenced are still there."
            )
    return mapping


async def _readable_dataset(conn: AsyncConnection, dataset_id: UUID) -> dict[str, Any] | None:
    """The dataset a layer points at, or None if this principal cannot read it.

    Relies on RLS rather than a second permission check: the row is invisible
    to a principal without access, which is exactly the question being asked.
    """
    result = await conn.execute(
        text(
            """
            SELECT id, name, kind, geometry_kind, storage_srid, feature_count,
                   bbox_4326, value_min, value_max, version
            FROM dataset WHERE id = :id AND deleted_at IS NULL
            """
        ),
        {"id": dataset_id},
    )
    row = result.one_or_none()
    return dict(row._mapping) if row is not None else None


async def list_sessions(
    conn: AsyncConnection, principal: Principal, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Recent sessions, newest first — what the app's landing page shows."""
    result = await conn.execute(
        text(
            """
            SELECT id, short_code, name, project_id, created_by_claude, visibility,
                   jsonb_array_length(layers) AS layer_count,
                   created_at, updated_at, expires_at
            FROM map_session
            WHERE deleted_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
            ORDER BY updated_at DESC
            LIMIT :limit
            """
        ),
        {"limit": max(1, min(limit, 200))},
    )
    return [dict(row._mapping) for row in result]


# --- update -----------------------------------------------------------------


async def update_session(
    conn: AsyncConnection,
    principal: Principal,
    session_id: UUID,
    *,
    layers: list[dict[str, Any]] | None = None,
    view: dict[str, Any] | None = None,
    name: str | None = None,
    expected_updated_at: Any = None,
    autosave: bool = False,
) -> dict[str, Any]:
    """Update a session. Requires editor.

    **Autosave passes `autosave=True` and writes no audit record.** The browser
    saves every few seconds while a user pans; recording each one would bury
    the events `03` §10 exists to preserve under a mountain of camera moves.
    The row's `updated_at` still moves, so nothing is lost about *when* the
    session last changed — only the claim that each pan was a deliberate act.

    `expected_updated_at` is optimistic concurrency, and it matters here more
    than elsewhere: a session open in two browser tabs, or in a tab and in
    Claude at once, is the normal case rather than the exception.
    """
    await load_and_require(conn, "map_session", session_id, principal, Permission.EDITOR)

    fields: dict[str, Any] = {}
    if layers is not None:
        normalised = normalise_layers(layers)
        for layer in normalised:
            await load_and_require(
                conn, "dataset", UUID(layer["dataset_id"]), principal, Permission.VIEWER
            )
        fields["layers"] = json.dumps(normalised)
    if view is not None:
        fields["view"] = json.dumps(normalise_view(view))
    if name is not None:
        fields["name"] = name
    if not fields:
        return {"updated": False}

    assignments = ", ".join(
        f"{key} = CAST(:{key} AS jsonb)" if key in ("layers", "view") else f"{key} = :{key}"
        for key in fields
    )
    guard = " AND updated_at = :expected_updated_at" if expected_updated_at else ""
    result = await conn.execute(
        text(
            f"""
            UPDATE map_session SET {assignments}, updated_at = now()
            WHERE id = :id AND deleted_at IS NULL{guard}
            RETURNING updated_at
            """
        ),
        {
            **fields,
            "id": session_id,
            **({"expected_updated_at": expected_updated_at} if expected_updated_at else {}),
        },
    )
    updated_at = result.scalar_one_or_none()
    if updated_at is None:
        raise VersionConflict(
            f"Session {session_id} changed since you loaded it — another tab or "
            f"another person saved first. Reload the session and reapply your "
            f"change; your edits were not written."
        )

    if not autosave:
        await record(
            conn,
            action=AuditAction.SESSION_UPDATED,
            principal=principal,
            object_type="map_session",
            object_id=session_id,
            detail={"fields": sorted(fields)},
        )
    return {"updated": True, "updated_at": updated_at}


async def soft_delete_session(
    conn: AsyncConnection, principal: Principal, session_id: UUID
) -> None:
    """Soft delete, 30 days (CLAUDE.md §3.4).

    A session link lives in a chat transcript and in people's bookmarks. A hard
    delete would break those with no way back; thirty days is long enough for
    someone to notice and ask.
    """
    await load_and_require(conn, "map_session", session_id, principal, Permission.OWNER)

    await conn.execute(
        text(
            "UPDATE map_session SET deleted_at = now(), updated_at = now() "
            "WHERE id = :id AND deleted_at IS NULL"
        ),
        {"id": session_id},
    )
    await record(
        conn,
        action=AuditAction.SESSION_DELETED,
        principal=principal,
        object_type="map_session",
        object_id=session_id,
    )


# --- schema migration -------------------------------------------------------


def _migrate_document(row: dict[str, Any]) -> dict[str, Any]:
    """Bring a stored session document up to `SCHEMA_VERSION`.

    A session link pasted into a chat six months ago must still open. This is
    where a change to the stored shape is absorbed, so that exactly one place
    knows about old forms and the rest of the code sees only the current one.

    Version 1 is the original shape, so there is nothing to do yet — but the
    seam exists now, because retrofitting it after the first shape change
    means writing the migration *and* finding every reader that assumed the
    new shape.
    """
    stored = int(row.get("schema_version") or 1)
    if stored > SCHEMA_VERSION:
        raise SessionError(
            f"Session {row.get('short_code')} was written by a newer version of "
            f"WebMap (document schema {stored}, this server reads {SCHEMA_VERSION}). "
            f"Reload the page to pick up the current app."
        )

    layers = row.get("layers") or []
    view = row.get("view") or {}
    if isinstance(layers, str):
        layers = json.loads(layers)
    if isinstance(view, str):
        view = json.loads(view)
    return {"layers": list(layers), "view": dict(view)}


__all__ = [
    "MAX_LAYERS",
    "SCHEMA_VERSION",
    "SessionError",
    "create_session",
    "generate_short_code",
    "list_sessions",
    "load_session",
    "normalise_layers",
    "normalise_view",
    "soft_delete_session",
    "update_session",
]
