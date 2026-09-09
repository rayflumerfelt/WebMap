"""Server-side style assembly. `06-rendering.md` §6, `03-auth-security.md` §7.3.

**Every source URL is minted here.** Nothing is passed through from the caller.
That is the structural rule §7.3 states, and it is what makes the render
service's SSRF guards a second line rather than the only one: a style that
never contained a caller-supplied URL cannot name an internal endpoint,
whatever the caller sent.

Client-supplied *symbology* is accepted — it is appearance, it is validated by
the compiler, and refusing it would mean a render could not show what the
browser shows. Client-supplied *source URLs* are not accepted at all.

The layer order is fixed here too, bottom to top: basemap, then the requested
layers in the given order, then labels. A caller cannot put the basemap on top
of the data by asking.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from webmap_core.permissions import Principal
from webmap_core.services import datasets as dataset_service
from webmap_core.style.compile import compile_symbology
from webmap_core.style.palette import Palette

#: Kinds whose features are served as vector tiles. A grid is a COG and goes
#: through TiTiler instead.
VECTOR_KINDS = frozenset({"vector", "pointset", "fault_network", "polygon_set"})


class StyleAssemblyError(Exception):
    """A layer reference cannot be turned into a style."""


async def build_style(
    conn: AsyncConnection,
    principal: Principal,
    layers: list[dict[str, Any]],
    *,
    tiles_base: str,
    titiler_base: str,
    static_base: str,
    palettes: dict[str, Palette] | None = None,
    token_for: Any = None,
    basemap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a complete MapLibre style from validated layer references.

    Each dataset is loaded through the permission path before it contributes a
    source, so a render can never reach a layer its requester could not
    (`03` §6). This is the same `resolve_feature_object` the tile endpoint
    uses — one authorization code path, not two.

    `token_for` mints a scoped tile token for a dataset id. Supplied by the
    caller because minting needs the signing secret, which this module has no
    business holding.
    """
    style: dict[str, Any] = {
        "version": 8,
        "name": "webmap-render",
        # MapLibre renders no label at all without glyphs, and says nothing
        # about why — the labels are simply absent.
        "glyphs": f"{static_base}/glyphs/{{fontstack}}/{{range}}.pbf",
        "sprite": f"{static_base}/sprite",
        "sources": {},
        "layers": [],
    }

    if basemap:
        # Beneath everything. Not a layer in the caller's list, so it cannot be
        # reordered above the data by asking.
        style["sources"].update(basemap.get("sources", {}))
        style["layers"].extend(basemap.get("layers", []))

    for index, ref in enumerate(layers):
        dataset_id = _dataset_id(ref, index)
        detail = await dataset_service.get_dataset(conn, principal, dataset_id)
        _add_layer(
            style,
            detail,
            ref,
            index=index,
            tiles_base=tiles_base,
            titiler_base=titiler_base,
            palettes=palettes or {},
            token=token_for(dataset_id) if token_for else None,
        )

    if not style["layers"]:
        raise StyleAssemblyError(
            "No layers could be added to this style. Every dataset referenced "
            "was either invisible to you or has no rendered representation — "
            "check the dataset ids with webmap_search_datasets."
        )
    return style


def _dataset_id(ref: dict[str, Any], index: int) -> UUID:
    raw = ref.get("dataset_id")
    if raw is None:
        raise StyleAssemblyError(
            f"Layer {index} has no dataset_id. A style is assembled from "
            f"registered datasets, never from URLs — every layer needs the id of "
            f"a dataset that already exists."
        )
    try:
        return UUID(str(raw))
    except ValueError as exc:
        raise StyleAssemblyError(
            f"Layer {index} has dataset_id {raw!r}, which is not a UUID. Use the "
            f"id from webmap_search_datasets, not the dataset's name."
        ) from exc


def _add_layer(
    style: dict[str, Any],
    detail: dict[str, Any],
    ref: dict[str, Any],
    *,
    index: int,
    tiles_base: str,
    titiler_base: str,
    palettes: dict[str, Palette],
    token: str | None,
) -> None:
    dataset_id = str(detail["id"])
    source_id = f"src-{dataset_id}"
    kind = str(detail["kind"])
    opacity = float(ref.get("opacity", 1.0))

    if kind == "grid":
        _add_grid(
            style,
            detail,
            ref,
            source_id=source_id,
            titiler_base=titiler_base,
            token=token,
            opacity=opacity,
        )
        return

    if kind not in VECTOR_KINDS:
        raise StyleAssemblyError(
            f"Dataset '{detail.get('name')}' is of kind '{kind}', which has no "
            f"map representation. Renderable kinds are: grid, "
            f"{', '.join(sorted(VECTOR_KINDS))}."
        )

    query = f"?token={token}" if token else ""
    style["sources"][source_id] = {
        "type": "vector",
        "tiles": [f"{tiles_base}/tiles/{dataset_id}/{{z}}/{{x}}/{{y}}.mvt{query}"],
        "minzoom": 0,
        "maxzoom": 16,
    }
    if detail.get("bbox_4326"):
        style["sources"][source_id]["bounds"] = detail["bbox_4326"]

    symbology = ref.get("symbology") or _default_symbology(detail)
    compiled = compile_symbology(
        symbology,
        source_id=source_id,
        source_layer="features",
        palettes=palettes,
        id_prefix=f"layer-{index}-{dataset_id[:8]}",
    )
    for layer in compiled:
        _apply_opacity(layer, opacity)
        style["layers"].append(layer)


def _add_grid(
    style: dict[str, Any],
    detail: dict[str, Any],
    ref: dict[str, Any],
    *,
    source_id: str,
    titiler_base: str,
    token: str | None,
    opacity: float,
) -> None:
    """A COG through the TiTiler proxy.

    **The COG + TiTiler payoff** (`01-architecture.md` §2.5): changing a colour
    ramp is a URL parameter change, not a regrid. The rescale defaults to the
    dataset's own range — without it TiTiler stretches to each tile's local
    range and the map becomes a patchwork.
    """
    dataset_id = str(detail["id"])
    colormap = str(ref.get("colormap") or "viridis")
    params = [f"colormap_name={colormap}"]

    if detail.get("value_min") is not None and detail.get("value_max") is not None:
        params.append(f"rescale={detail['value_min']},{detail['value_max']}")
    if token:
        params.append(f"token={token}")

    style["sources"][source_id] = {
        "type": "raster",
        "tiles": [f"{titiler_base}/cog/{dataset_id}/{{z}}/{{x}}/{{y}}.png?{'&'.join(params)}"],
        # 256, not 512: the proxy renders WebMercatorQuad tiles, and declaring
        # 512 stretches every tile to double size — which reads as a blurry
        # grid rather than as a configuration mistake.
        "tileSize": 256,
    }
    if detail.get("bbox_4326"):
        style["sources"][source_id]["bounds"] = detail["bbox_4326"]

    style["layers"].append(
        {
            "id": f"grid-{dataset_id[:8]}",
            "type": "raster",
            "source": source_id,
            "paint": {"raster-opacity": opacity, "raster-resampling": "linear"},
        }
    )


def _apply_opacity(layer: dict[str, Any], opacity: float) -> None:
    """Multiply, never replace.

    A polygon styled at 0.8 fill in a layer requested at 0.5 lands at 0.4.
    Setting it outright would make a layer opacity erase a deliberate styling
    choice — the same rule `compileStyle` follows in the browser, so a render
    and the interactive map agree.
    """
    if opacity >= 1.0:
        return
    property_name = {
        "fill": "fill-opacity",
        "line": "line-opacity",
        "circle": "circle-opacity",
        "symbol": "text-opacity",
        "raster": "raster-opacity",
    }.get(str(layer.get("type")))
    if property_name is None:
        return

    paint = dict(layer.get("paint") or {})
    existing = paint.get(property_name)
    if isinstance(existing, int | float):
        paint[property_name] = float(existing) * opacity
    elif existing is None:
        paint[property_name] = opacity
    else:
        paint[property_name] = ["*", existing, opacity]
    layer["paint"] = paint


def _default_symbology(detail: dict[str, Any]) -> dict[str, Any]:
    """Something drawable for a layer with no stored styling.

    A layer with no symbology compiles to no MapLibre layers and is invisible,
    which reads as a broken render rather than as an unstyled layer.
    """
    geometry = str(detail.get("geometry_kind") or "").lower()

    if "polygon" in geometry:
        return {
            "type": "single",
            "symbol": {
                "geometry": "polygon",
                "fillColor": "#88a0c8",
                "fillOpacity": 0.5,
                "outlineColor": "#213547",
                "outlineWidth": 1,
            },
        }
    if "line" in geometry:
        return {
            "type": "single",
            "symbol": {
                "geometry": "line",
                "color": "#c0392b",
                "width": 1.5,
                "opacity": 1,
                "cap": "round",
                "join": "round",
            },
        }
    return {
        "type": "single",
        "symbol": {
            "geometry": "point",
            "marker": "circle",
            "size": 4,
            "color": "#2b93b3",
            "strokeColor": "#ffffff",
            "strokeWidth": 1,
            "opacity": 1,
        },
    }


def bounds_of(details: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    """The union of several datasets' WGS84 bounds.

    Returns None when nothing has bounds, so the caller can decide — inventing
    a world view for a render nobody asked to be global would produce a map of
    the Atlantic with a dot on it.
    """
    boxes = [d["bbox_4326"] for d in details if d.get("bbox_4326")]
    if not boxes:
        return None
    return (
        min(float(b[0]) for b in boxes),
        min(float(b[1]) for b in boxes),
        max(float(b[2]) for b in boxes),
        max(float(b[3]) for b in boxes),
    )


__all__ = ["VECTOR_KINDS", "StyleAssemblyError", "bounds_of", "build_style"]
