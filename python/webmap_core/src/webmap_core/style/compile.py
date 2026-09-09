"""Compile a symbology model into MapLibre layers. `08-styling-palettes.md` §3.

The Python half of the two-implementation compiler in §3.1. **This file and
`packages/style-model/src/compile.ts` must produce identical output**, key for
key and colour for colour; the shared vectors in
`packages/style-model/test-vectors/` are what keeps them honest.

One symbology may produce **several** MapLibre layers — a polygon with an
outline is a fill layer plus a line layer, a line with casing is two line
layers in order. Callers must not assume a 1:1 mapping.

Symbology arrives as the JSON the frontend wrote, so the keys here are the
TypeScript spelling (`fillColor`, `classCount`, `baseSymbol`). That is the
stored shape; renaming it at this boundary would create a second vocabulary
for the same document, and a translation layer that could drift on its own.
"""

from __future__ import annotations

from typing import Any

from webmap_core.exceptions import WebMapError
from webmap_core.style.palette import Palette, sample_ramp

#: A compiled MapLibre layer. Deliberately a plain dict: the authority on this
#: shape is the MapLibre style specification, and mirroring it in a model here
#: would create a second definition to keep in step.
CompiledLayer = dict[str, Any]


class InvalidSymbology(WebMapError):
    """A symbology cannot be compiled as given."""


def compile_symbology(
    symbology: dict[str, Any],
    *,
    source_id: str,
    palettes: dict[str, Palette],
    source_layer: str | None = None,
    id_prefix: str | None = None,
) -> list[CompiledLayer]:
    options = _Options(
        source_id=source_id,
        source_layer=source_layer,
        palettes=palettes,
        prefix=id_prefix or source_id,
    )
    kind = symbology.get("type")
    if kind == "single":
        return _layers_for(symbology["symbol"], options, options.prefix, {})
    if kind == "categorized":
        return _compile_categorized(symbology, options)
    if kind == "graduated":
        return _compile_graduated(symbology, options)
    if kind == "rules":
        return _compile_rules(symbology, options)
    if kind == "continuous_raster":
        return _compile_raster(symbology, options)
    raise InvalidSymbology(
        f"Unknown symbology type {kind!r}. Expected one of: single, categorized, "
        f"graduated, rules, continuous_raster (08-styling-palettes.md §2)."
    )


class _Options:
    __slots__ = ("palettes", "prefix", "source_id", "source_layer")

    def __init__(
        self,
        *,
        source_id: str,
        source_layer: str | None,
        palettes: dict[str, Palette],
        prefix: str,
    ) -> None:
        self.source_id = source_id
        self.source_layer = source_layer
        self.palettes = palettes
        self.prefix = prefix


# --- per type ---------------------------------------------------------------


def _compile_categorized(s: dict[str, Any], o: _Options) -> list[CompiledLayer]:
    # `match` rather than a chain of `case`: it is a lookup rather than a
    # sequence of comparisons, and MapLibre evaluates it as one.
    categories: list[dict[str, Any]] = s.get("categories", [])
    base = categories[0]["symbol"] if categories else s.get("other")
    if base is None:
        raise InvalidSymbology(
            f"Categorized symbology on '{s.get('field')}' has no categories and no "
            f"fallback symbol, so it would render nothing."
        )

    overrides: dict[str, Any] = {}
    for prop in _variable_properties(base):
        expression: list[Any] = ["match", ["get", s["field"]]]
        for category in categories:
            # A null category matches the absence of the attribute, which
            # `match` cannot express — those fall through to the fallback.
            if category["value"] is None:
                continue
            expression.append(category["value"])
            expression.append(_read_property(category["symbol"], prop))
        expression.append(_read_property(s.get("other") or base, prop))
        overrides[prop] = expression

    return _layers_for(base, o, o.prefix, overrides)


def _compile_graduated(s: dict[str, Any], o: _Options) -> list[CompiledLayer]:
    palette = o.palettes.get(s["paletteId"])
    if palette is None:
        raise InvalidSymbology(
            f"Graduated symbology references palette '{s['paletteId']}', which was "
            f"not supplied. Pass every palette a layer uses; compilation cannot "
            f"fetch one."
        )
    breaks: list[float] = s["breaks"]
    class_count: int = s["classCount"]
    if len(breaks) != class_count - 1:
        raise InvalidSymbology(
            f"Graduated symbology has {class_count} classes but {len(breaks)} "
            f"breaks. classify() returns classCount - 1 interior breaks; a "
            f"mismatch renders a map and a legend that disagree "
            f"(08-styling-palettes.md §8)."
        )

    out_of_order = next((i for i in range(1, len(breaks)) if breaks[i] <= breaks[i - 1]), 0)
    if out_of_order:
        # Breaks reach here from `classify()`, which already guarantees this —
        # but also from a geologist typing them into the class table, which
        # does not. A `step` expression with non-ascending stops is rejected by
        # MapLibre outright, so the layer disappears instead of rendering
        # wrongly, and nothing on screen says why.
        raise InvalidSymbology(
            f"Graduated breaks must ascend strictly; break {out_of_order} "
            f"({breaks[out_of_order]:g}) is not above the one before it "
            f"({breaks[out_of_order - 1]:g}). Edit the class table so each break "
            f"is larger than the last."
        )

    base = s["baseSymbol"]
    overrides: dict[str, Any] = {}
    vary = s.get("vary", "color")

    if vary in ("color", "both"):
        colours = sample_ramp(palette, class_count)
        # `step`, not `interpolate`: graduated classification is discrete by
        # definition, and interpolating would blur class boundaries and make
        # the legend a lie.
        expression: list[Any] = ["step", ["get", s["field"]], colours[0]]
        for i, brk in enumerate(breaks):
            expression.append(brk)
            expression.append(colours[i + 1])
        overrides[_colour_property(base)] = expression

    size_range = s.get("sizeRange")
    if vary in ("size", "both") and size_range:
        low, high = size_range
        sizes = [
            low if class_count == 1 else low + (high - low) * i / (class_count - 1)
            for i in range(class_count)
        ]
        size_expression: list[Any] = ["step", ["get", s["field"]], sizes[0]]
        for i, brk in enumerate(breaks):
            size_expression.append(brk)
            size_expression.append(sizes[i + 1])
        overrides[_size_property(base)] = size_expression

    return _layers_for(base, o, o.prefix, overrides)


def _compile_rules(s: dict[str, Any], o: _Options) -> list[CompiledLayer]:
    # One layer set per rule, in order, so later rules paint over earlier ones —
    # which is what a rule list means to anyone coming from QGIS.
    compiled: list[CompiledLayer] = []
    for index, rule in enumerate(s["rules"]):
        for layer in _layers_for(rule["symbol"], o, f"{o.prefix}-rule-{index}", {}):
            layer["filter"] = rule["filter"]
            if rule.get("minZoom") is not None:
                layer["minzoom"] = rule["minZoom"]
            if rule.get("maxZoom") is not None:
                layer["maxzoom"] = rule["maxZoom"]
            compiled.append(layer)
    return compiled


def _compile_raster(s: dict[str, Any], o: _Options) -> list[CompiledLayer]:
    # The colour ramp is *not* compiled into the style: it is a TiTiler URL
    # parameter on the source, which is what makes changing a palette a
    # parameter change rather than a regrid (01-architecture.md §2.5).
    return [
        {
            "id": f"{o.prefix}-raster",
            "type": "raster",
            "source": o.source_id,
            "paint": {"raster-opacity": s["opacity"], "raster-resampling": "linear"},
        }
    ]


# --- symbol to layers -------------------------------------------------------


def _layers_for(
    symbol: dict[str, Any], o: _Options, layer_id: str, overrides: dict[str, Any]
) -> list[CompiledLayer]:
    geometry = symbol["geometry"]
    if geometry == "point":
        return [
            _with_source(
                {
                    "id": f"{layer_id}-circle",
                    "type": "circle",
                    "paint": {
                        "circle-radius": symbol["size"],
                        "circle-color": symbol["color"],
                        "circle-stroke-color": symbol["strokeColor"],
                        "circle-stroke-width": symbol["strokeWidth"],
                        "circle-opacity": symbol["opacity"],
                        **overrides,
                    },
                },
                o,
            )
        ]
    if geometry == "line":
        return _line_layers(symbol, o, layer_id, overrides)
    if geometry == "polygon":
        return _polygon_layers(symbol, o, layer_id, overrides)
    if geometry == "label":
        return [
            _with_source(
                {
                    "id": f"{layer_id}-label",
                    "type": "symbol",
                    "layout": {
                        "text-field": ["get", symbol["field"]],
                        "text-size": symbol["size"],
                        "text-font": symbol["font"],
                        "symbol-placement": symbol["placement"],
                        "text-allow-overlap": symbol["allowOverlap"],
                    },
                    "paint": {
                        "text-color": symbol["color"],
                        "text-halo-color": symbol["haloColor"],
                        "text-halo-width": symbol["haloWidth"],
                        **overrides,
                    },
                },
                o,
            )
        ]
    raise InvalidSymbology(
        f"Unknown symbol geometry {geometry!r}. Expected point, line, polygon or label."
    )


def _line_layers(
    s: dict[str, Any], o: _Options, layer_id: str, overrides: dict[str, Any]
) -> list[CompiledLayer]:
    layers: list[CompiledLayer] = []
    casing = s.get("casing")
    if casing:
        # Emitted first so it paints beneath. A casing above its line is a
        # hairline outline instead of a road edge.
        layers.append(
            _with_source(
                {
                    "id": f"{layer_id}-casing",
                    "type": "line",
                    "layout": {"line-cap": s["cap"], "line-join": s["join"]},
                    "paint": {"line-color": casing["color"], "line-width": casing["width"]},
                },
                o,
            )
        )
    paint: dict[str, Any] = {
        "line-color": s["color"],
        "line-width": s["width"],
        "line-opacity": s["opacity"],
    }
    if s.get("dashArray"):
        paint["line-dasharray"] = s["dashArray"]
    paint.update(overrides)
    layers.append(
        _with_source(
            {
                "id": f"{layer_id}-line",
                "type": "line",
                "layout": {"line-cap": s["cap"], "line-join": s["join"]},
                "paint": paint,
            },
            o,
        )
    )
    return layers


def _polygon_layers(
    s: dict[str, Any], o: _Options, layer_id: str, overrides: dict[str, Any]
) -> list[CompiledLayer]:
    fill: dict[str, Any] = {"fill-color": s["fillColor"], "fill-opacity": s["fillOpacity"]}
    if s.get("fillPattern"):
        fill["fill-pattern"] = s["fillPattern"]
    fill.update(overrides)
    layers: list[CompiledLayer] = [
        _with_source({"id": f"{layer_id}-fill", "type": "fill", "paint": fill}, o)
    ]
    if s["outlineWidth"] > 0:
        # A separate line layer rather than `fill-outline-color`, which MapLibre
        # renders at exactly 1px and ignores width and dashes on.
        outline: dict[str, Any] = {
            "line-color": s["outlineColor"],
            "line-width": s["outlineWidth"],
        }
        if s.get("outlineDashArray"):
            outline["line-dasharray"] = s["outlineDashArray"]
        layers.append(
            _with_source({"id": f"{layer_id}-outline", "type": "line", "paint": outline}, o)
        )
    return layers


def _with_source(layer: CompiledLayer, o: _Options) -> CompiledLayer:
    layer["source"] = o.source_id
    if o.source_layer:
        layer["source-layer"] = o.source_layer
    return layer


# --- property naming --------------------------------------------------------


def _colour_property(symbol: dict[str, Any]) -> str:
    """Which paint property a data-driven colour writes to, per geometry."""
    return {
        "point": "circle-color",
        "line": "line-color",
        "polygon": "fill-color",
        "label": "text-color",
    }[symbol["geometry"]]


def _size_property(symbol: dict[str, Any]) -> str:
    return {
        "point": "circle-radius",
        "line": "line-width",
        # Polygons have no size. Varying one by a value means the outline.
        "polygon": "line-width",
        "label": "text-size",
    }[symbol["geometry"]]


def _variable_properties(symbol: dict[str, Any]) -> list[str]:
    """The properties a categorized symbology varies across its categories."""
    return [_colour_property(symbol)]


def _read_property(symbol: dict[str, Any], prop: str) -> Any:
    if prop == "circle-color" or prop == "line-color":
        return symbol["color"]
    if prop == "fill-color":
        return symbol["fillColor"]
    if prop == "text-color":
        return symbol["color"]
    raise InvalidSymbology(f"No reader for varied property '{prop}'.")


__all__ = ["CompiledLayer", "InvalidSymbology", "compile_symbology"]
