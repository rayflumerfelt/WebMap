"""Tile endpoints and the TiTiler auth proxy. `03-auth-security.md` §6.

Tile endpoints are hit thousands of times during a single pan. The temptation
to leave them open for performance is exactly how data leaks.

Two credentials are accepted, and they exist for different callers:

- **A scoped token** (`?token=`), minted after a permission check and valid
  for one dataset, one user, fifteen minutes. This is what the browser puts in
  a style's tile URL, where it will be visible in devtools and in any bug
  report — so a leak exposes exactly one already-authorized layer.
- **An ordinary bearer token**, for the render service and for anything
  calling the API directly. Multi-layer by definition, so a per-dataset token
  cannot serve it (`03` §6.1).

Both converge on the same `require(..., VIEWER)` check. **One authorization
code path, not two** — which is what §3.1 asks for, and it means a render can
never reach a layer its requester cannot.
"""

import hashlib
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_api.dependencies import (
    AppSettings,
    CurrentPrincipal,
    ScopedConn,
    get_channel,
    get_claims,
)
from webmap_core.db.session import principal_session, unscoped_session
from webmap_core.exceptions import InvalidToken, NotFound
from webmap_core.identity import AuthenticationFailed, Claims
from webmap_core.logging import get_logger
from webmap_core.permissions import Channel, Principal
from webmap_core.services import datasets as service
from webmap_core.services.directory import resolve_principal, team_ids_for
from webmap_core.signing import verify_tile_token
from webmap_geo.exceptions import DegenerateInput

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["tiles"])

#: `06-rendering.md` §7: cached by (dataset_id, version, z, x, y). The version
#: in the key means an edit invalidates exactly the layer that changed, and
#: nothing else, with no explicit purge — the pointer advances and every old
#: key is simply never requested again.
_TILE_CACHE: dict[str, bytes] = {}
_CACHE_LIMIT = 2_000


def _cache_key(dataset_id: UUID, version: int, z: int, x: int, y: int) -> str:
    return f"{dataset_id}:{version}:{z}:{x}:{y}"


async def tile_principal(
    request: Request,
    settings: AppSettings,
    dataset_id: UUID,
    token: Annotated[str | None, Query(description="Scoped tile token")] = None,
    authorization: Annotated[str | None, Header()] = None,
    channel: Annotated[Channel, Depends(get_channel)] = Channel.WEB,
) -> Principal:
    """Resolve the caller from either credential, then hand back a Principal.

    The scoped token proves exactly one thing: that this user was authorized
    for this dataset within the last fifteen minutes. It is deliberately not
    treated as a standing permission — the principal it names is rebuilt and
    the ordinary `require(..., VIEWER)` check runs again on every tile, so a
    grant revoked thirty seconds ago takes effect on the next request rather
    than at token expiry (`03-auth-security.md` §4.4).

    Team membership comes from `team_member` here rather than from a token's
    group claim, because a scoped token carries no claims. That is the one
    place the cache is authoritative, and it is why the reconcile in
    `directory.py` exists rather than being an optimisation.
    """
    if token:
        secret = settings.tile_token_secret.get_secret_value().encode()
        try:
            user_id = verify_tile_token(token, dataset_id, secret)
        except InvalidToken as exc:
            # One message for every cause. Distinguishing an expired token
            # from a forged one tells a caller which half to fix.
            raise AuthenticationFailed(
                "This tile link is not valid for this layer, or it has "
                "expired. Reload the map to get a fresh one."
            ) from exc

        async with unscoped_session(request.app.state.engine) as conn:
            teams = await team_ids_for(conn, user_id)
        return Principal(user_id=user_id, team_ids=teams, channel=channel)

    claims: Claims = await get_claims(request, request.app.state.verifier, authorization)
    async with unscoped_session(request.app.state.engine) as conn:
        return await resolve_principal(conn, claims, channel)


@router.post("/datasets/{dataset_id}/tile-token")
async def mint_tile_token(
    principal: CurrentPrincipal,
    conn: ScopedConn,
    settings: AppSettings,
    dataset_id: UUID,
) -> dict[str, Any]:
    """Mint a scoped tile token after checking the caller may see the layer.

    The check happens here, once, rather than on every tile — that is the
    whole point of a scoped token. The token is not a general credential: one
    minted for dataset A cannot fetch dataset B, so a tile URL pasted into a
    bug report exposes exactly one layer the reporter could already see.
    """
    from webmap_core.signing import mint_tile_token as mint

    await service.get_dataset(conn, principal, dataset_id)

    token = mint(
        dataset_id,
        principal.user_id,
        settings.tile_token_secret.get_secret_value().encode(),
        ttl_seconds=settings.tile_token_ttl_seconds,
    )
    return {
        "token": token,
        "expires_in": settings.tile_token_ttl_seconds,
        "dataset_id": str(dataset_id),
    }


@router.get("/tiles/{dataset_id}/{z}/{x}/{y}.mvt")
async def vector_tile(
    request: Request,
    settings: AppSettings,
    dataset_id: UUID,
    z: int,
    x: int,
    y: int,
    principal: Annotated[Principal, Depends(tile_principal)],
) -> Response:
    """One vector tile, generated in-process from GeoParquet via DuckDB.

    The permission check runs on every request even when a scoped token was
    presented. Skipping it for a token-bearing request would make revocation
    wait fifteen minutes, and the check is one indexed row read against a
    connection that is already open.
    """
    from webmap_geo.dataplane import ObjectStore
    from webmap_geo.tiles import TileRequest, render_mvt

    async with principal_session(request.app.state.engine, principal) as conn:
        key, version = await service.resolve_feature_object(conn, principal, dataset_id)
        srid = await _storage_srid(conn, dataset_id)

    cache_key = _cache_key(dataset_id, version, z, x, y)
    cached = _TILE_CACHE.get(cache_key)
    if cached is not None:
        return _tile_response(cached, cache_key, hit=True)

    store = ObjectStore(
        endpoint=settings.s3_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    data = render_mvt(
        TileRequest(
            dataset_id=str(dataset_id),
            parquet_key=f"s3://{settings.s3_bucket}/{key}",
            storage_srid=srid,
            z=z,
            x=x,
            y=y,
        ),
        store,
    )

    if len(_TILE_CACHE) >= _CACHE_LIMIT:
        # Crude, and deliberately so: a real LRU here would be a cache
        # library's job, and this one exists to make a pan smooth rather than
        # to be optimal. Cleared wholesale rather than evicting one entry,
        # because a half-full dict of cold tiles is not worth the bookkeeping.
        _TILE_CACHE.clear()
    _TILE_CACHE[cache_key] = data

    return _tile_response(data, cache_key, hit=False)


def _tile_response(data: bytes, cache_key: str, *, hit: bool) -> Response:
    """204 for an empty tile, so MapLibre stops asking about that area."""
    if not data:
        return Response(status_code=204, headers={"X-Tile-Cache": "hit" if hit else "miss"})
    return Response(
        content=data,
        media_type="application/vnd.mapbox-vector-tile",
        headers={
            # Private: a tile is one user's authorized view, and a shared
            # cache serving it to the next requester would undo the scoping.
            "Cache-Control": "private, max-age=300",
            "ETag": f'W/"{hashlib.sha256(cache_key.encode()).hexdigest()[:16]}"',
            "X-Tile-Cache": "hit" if hit else "miss",
        },
    )


@router.get("/features/{dataset_id}.geojson")
async def feature_collection(
    request: Request,
    settings: AppSettings,
    dataset_id: UUID,
    principal: Annotated[Principal, Depends(tile_principal)],
) -> Any:
    """The whole layer as GeoJSON, for layers under the switch threshold.

    Refuses above it rather than truncating: a silently partial layer is worse
    than an error, because it looks like the data (`06-rendering.md` §7.1).
    """
    from webmap_geo.dataplane import ObjectStore
    from webmap_geo.tiles import GEOJSON_FEATURE_LIMIT, geojson_features, should_use_geojson

    async with principal_session(request.app.state.engine, principal) as conn:
        key, _ = await service.resolve_feature_object(conn, principal, dataset_id)
        detail = await service.get_dataset(conn, principal, dataset_id)

    count = detail.get("feature_count")
    if not should_use_geojson(count):
        raise NotFound(
            f"This layer has {count if count is not None else 'an unknown number of'} "
            f"features, at or above the {GEOJSON_FEATURE_LIMIT:,} limit for GeoJSON "
            f"delivery. Use the vector tile endpoint instead — anything larger "
            f"stalls the browser's main thread (06-rendering.md §7.1)."
        )

    store = ObjectStore(
        endpoint=settings.s3_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    return geojson_features(
        f"s3://{settings.s3_bucket}/{key}", int(detail["storage_srid"]), store
    )


@router.get("/features/{dataset_id}/attributes")
async def feature_attributes(
    request: Request,
    settings: AppSettings,
    dataset_id: UUID,
    principal: Annotated[Principal, Depends(tile_principal)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    order_by: Annotated[str | None, Query(description="Attribute to sort by")] = None,
    descending: bool = False,
) -> dict[str, Any]:
    """A page of a layer's attributes, without geometry.

    The GeoJSON endpoint refuses above the switch threshold rather than
    truncating, which is right for a map source and useless for a table — it
    left a 500k-feature layer with no attribute view at all. This pages
    instead, and reads no geometry, so a column of porosities costs a column of
    porosities rather than megabytes of coordinates.
    """
    from webmap_geo.attributes import feature_attributes as read_attributes
    from webmap_geo.dataplane import ObjectStore

    async with principal_session(request.app.state.engine, principal) as conn:
        key, _ = await service.resolve_feature_object(conn, principal, dataset_id)

    store = ObjectStore(
        endpoint=settings.s3_endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    page = read_attributes(
        f"s3://{settings.s3_bucket}/{key}",
        store,
        offset=offset,
        limit=limit,
        order_by=order_by,
        descending=descending,
    )
    return {
        "items": page.items,
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
        "has_more": page.has_more,
    }


#: A colormap is forwarded to TiTiler as a query parameter, so it is bounded.
#:
#: A 256-entry RGBA lookup is about 6 KB and a discrete band set is far
#: smaller, so 32 KB is generous. It is deliberately well under the ~64 KB URL
#: length that clients and proxies enforce — a limit above that would be
#: unreachable, because the request would be refused before it arrived. Note
#: that many reverse proxies cap URLs at 8 KB, so a palette approaching this
#: size may not survive a production deployment even though the API accepts it.
MAX_COLORMAP_BYTES = 32 * 1024


def _validated_colormap(raw: str) -> tuple[str, bool]:
    """Parse-check an explicit colormap. Returns it with "is it discrete?".

    Only the shape is checked, not the semantics — TiTiler owns those. The
    point is that a malformed palette fails here, naming what is wrong, rather
    than reaching an internal service and coming back as an opaque 500.

    **The discrete flag decides whether `rescale` may be sent**, and getting
    that wrong renders the whole grid transparent. `rescale` normalises the
    data to 0-255 *before* the colormap is applied, so:

    - a **list** of `[[min,max],[r,g,b,a]]` bands is in raw data units and
      must not be rescaled — its bounds would be compared against 0-255 and
      nothing would ever match;
    - a **dict** keyed 0-255 is a lookup and needs `rescale` to mean anything.

    Sending both produced a fully transparent tile, which is how this was
    found: 65,536 pixels of alpha zero and a 200 response.
    """
    import json

    if len(raw.encode()) > MAX_COLORMAP_BYTES:
        raise DegenerateInput(
            f"The colormap is {len(raw.encode()):,} bytes (limit "
            f"{MAX_COLORMAP_BYTES:,}). A 256-entry lookup is about 6 KB; "
            f"something this large is not a palette."
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DegenerateInput(
            f"The colormap is not valid JSON ({error}). Send either discrete "
            f"bands — [[[min,max],[r,g,b,a]], ...] — or a lookup keyed 0-255."
        ) from error

    if not isinstance(parsed, list | dict) or not parsed:
        raise DegenerateInput(
            "A colormap is a non-empty list of bands or a lookup object keyed "
            "0-255. An empty one would render the whole grid transparent."
        )
    return raw, isinstance(parsed, list)


@router.get("/cog/{dataset_id}/{z}/{x}/{y}.png")
async def raster_tile(
    request: Request,
    settings: AppSettings,
    dataset_id: UUID,
    z: int,
    x: int,
    y: int,
    principal: Annotated[Principal, Depends(tile_principal)],
    colormap_name: Annotated[str, Query()] = "viridis",
    colormap: Annotated[
        str | None,
        Query(
            description=(
                "Explicit colormap as JSON, overriding colormap_name. Either "
                "discrete bands in data units — [[[min,max],[r,g,b,a]], ...] — "
                'or a 0-255 lookup {"0": [r,g,b,a], ...} used with rescale.'
            )
        ),
    ] = None,
    rescale: Annotated[str | None, Query(description="min,max")] = None,
) -> Response:
    """Proxy a COG tile from TiTiler, after checking permission.

    TiTiler sits behind this and is never exposed directly (`03` §6): it has no
    concept of a WebMap user, so anything that could reach it could read any
    grid in the bucket. The auth decision happens here; TiTiler only renders.

    Changing the colour ramp is a URL parameter change rather than a regrid,
    which is what makes palette editing feel instant (`01` §2.5).

    **A grid is not coloured by MapLibre.** Raster layers have no data-driven
    paint, so the colour is baked into this PNG before the browser sees it —
    which is why a custom palette has to arrive here rather than in the style.
    `colormap_name` covers the named ramps; `colormap` carries the two forms a
    palette editor produces:

    - **Discrete bands**, in data units, for interval symbology. No `rescale`:
      the bands say what they mean, so the same bands compare directly across
      grids.
    - **A 0-255 lookup**, used with `rescale`, for a gradient. Relative to the
      range, which is why `rescale` matters so much.
    """
    async with principal_session(request.app.state.engine, principal) as conn:
        cog_key = await service.resolve_grid_object(conn, principal, dataset_id)
        detail = await service.get_dataset(conn, principal, dataset_id)

    default_rescale: str | None = None
    if rescale is None and detail.get("value_min") is not None:
        # Default to the dataset's own range. Without it TiTiler stretches to
        # the tile's local range, so every tile gets its own scale and the map
        # becomes a patchwork.
        default_rescale = f"{detail['value_min']},{detail['value_max']}"
        rescale = default_rescale

    params: dict[str, str] = {"url": f"s3://{settings.s3_bucket}/{cog_key}"}
    discrete = False
    if colormap:
        # Validated rather than proxied blind: this string is forwarded to an
        # internal service, and an unparseable one should fail here with a
        # message naming the problem rather than as a 500 from TiTiler.
        params["colormap"], discrete = _validated_colormap(colormap)
    else:
        params["colormap_name"] = colormap_name

    # Discrete bands are in data units and are ruined by rescale — see
    # `_validated_colormap`. An explicitly requested rescale is still honoured,
    # because a caller who sends both has said what they mean.
    if rescale and not (discrete and rescale is default_rescale):
        params["rescale"] = rescale

    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.get(
            f"{settings.titiler_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png",
            params=params,
        )

    if upstream.status_code == 404:
        return Response(status_code=204)
    if upstream.status_code >= 400:
        log.warning("titiler_error", status=upstream.status_code, dataset_id=str(dataset_id))
        raise NotFound(
            f"The raster tile service could not render this grid "
            f"(HTTP {upstream.status_code}). The dataset is registered, so this "
            f"is a rendering problem rather than a permission one."
        )

    return Response(
        content=upstream.content,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


async def _storage_srid(conn: AsyncConnection, dataset_id: UUID) -> int:
    from sqlalchemy import text

    result = await conn.execute(
        text("SELECT storage_srid FROM dataset WHERE id = :id"), {"id": dataset_id}
    )
    return int(result.scalar_one())


__all__ = ["router"]
