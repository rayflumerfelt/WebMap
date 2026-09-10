"""Generate the six visual-regression fixtures. `06-rendering.md` §10.

Run with `uv run python tests/visual/fixtures/generate.py`. Checked in beside
the fixtures it writes, because a fixture nobody can regenerate is one nobody
can change: the first time a case needs a sixth contour or a different section
grid, the alternative is editing 250 kB of coordinates by hand.

Synthetic and **self-contained**: every source is inline GeoJSON, so a case
renders with no API, no tile server and no object store in reach. That is not
only convenience — the render service refuses hosts it does not allow, and a
fixture that fetched anything would make the harness a test of the network as
much as of the renderer.

`tests/fixtures/` is synthetic only (`CLAUDE.md` §7.5), and this holds to it:
the geometry is generated from arithmetic over a made-up township in the
Midland Basin.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent
OUT.mkdir(parents=True, exist_ok=True)

#: A made-up township, in degrees. Small enough that the plate-carrée error
#: over it is irrelevant to a picture, and placed where a Permian map would be
#: so the case looks like the thing it protects.
WEST, SOUTH = -102.20, 31.90
EAST, NORTH = -102.00, 32.05

BOUNDS = [WEST, SOUTH, EAST, NORTH]


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def structure(x: float, y: float) -> float:
    """A plausible dipping surface with one closure. Arithmetic, not data."""
    dip = -9000.0 - 4000.0 * (x - WEST) / (EAST - WEST)
    closure = 260.0 * math.exp(
        -(
            ((x - lerp(WEST, EAST, 0.62)) / 0.028) ** 2
            + ((y - lerp(SOUTH, NORTH, 0.45)) / 0.020) ** 2
        )
    )
    return dip + closure


def contour_lines() -> list[dict[str, Any]]:
    """Contours as polylines sampled along the surface's own level sets.

    Traced coarsely on purpose — this is a picture to compare against itself,
    not a contouring test, and `webmap_geo.contour` has its own.
    """
    features: list[dict[str, Any]] = []
    for index, level in enumerate(range(-12800, -8800, 200)):
        points = []
        for step in range(121):
            y = lerp(SOUTH, NORTH, step / 120)
            # Solve for x along this row by scanning; coarse is fine.
            best_x, best_error = None, 1e9
            for column in range(241):
                x = lerp(WEST, EAST, column / 240)
                error = abs(structure(x, y) - level)
                if error < best_error:
                    best_x, best_error = x, error
            if best_x is not None and best_error < 40.0:
                points.append([round(best_x, 6), round(y, 6)])
        if len(points) >= 2:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": points},
                    "properties": {
                        "value": level,
                        # Every fifth contour is an index contour — the rule
                        # `08` §6.2 calls the most important piece of contour
                        # formatting, and the one this case protects.
                        "is_index": index % 5 == 0,
                        "label": f"{level:,}",
                    },
                }
            )
    return features


def fault_lines() -> list[dict[str, Any]]:
    """Two normal faults, drawn as the ticked polylines a structure map uses."""
    features = []
    for index, offset in enumerate((0.30, 0.72)):
        x = lerp(WEST, EAST, offset)
        points = [
            [
                round(x + 0.004 * math.sin(step / 6.0), 6),
                round(lerp(SOUTH, NORTH, step / 20), 6),
            ]
            for step in range(21)
        ]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": points},
                "properties": {"name": f"Fault {index + 1}", "throw": 120 + 60 * index},
            }
        )
    return features


def wells(count: int = 1400) -> list[dict[str, Any]]:
    """A dense posting on a jittered grid, the way a developed field looks.

    Deterministic: a linear congruential generator written out rather than
    `random`, so the fixture is byte-identical on every machine that
    regenerates it. A golden compared against a differently-jittered posting is
    a diff nobody can read.
    """
    features = []
    seed = 20260910
    side = int(math.sqrt(count))
    for index in range(count):
        seed = (1103515245 * seed + 12345) % (2**31)
        jitter_x = (seed / (2**31) - 0.5) * 0.004
        seed = (1103515245 * seed + 12345) % (2**31)
        jitter_y = (seed / (2**31) - 0.5) * 0.004

        x = lerp(WEST, EAST, (index % side) / max(side - 1, 1)) + jitter_x
        y = lerp(SOUTH, NORTH, (index // side) / max(side - 1, 1)) + jitter_y
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(x, 6), round(y, 6)]},
                "properties": {
                    "api": f"42-{index:06d}",
                    "porosity": round(6.0 + 8.0 * abs(math.sin(index * 0.37)), 2),
                },
            }
        )
    return features


def sections() -> list[dict[str, Any]]:
    """A 6×6 section grid, for the graduated-polygon and label cases."""
    features = []
    for row in range(6):
        for column in range(6):
            x0 = lerp(WEST, EAST, column / 6)
            x1 = lerp(WEST, EAST, (column + 1) / 6)
            y0 = lerp(SOUTH, NORTH, row / 6)
            y1 = lerp(SOUTH, NORTH, (row + 1) / 6)
            number = row * 6 + column + 1
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [round(x0, 6), round(y0, 6)],
                                [round(x1, 6), round(y0, 6)],
                                [round(x1, 6), round(y1, 6)],
                                [round(x0, 6), round(y1, 6)],
                                [round(x0, 6), round(y0, 6)],
                            ]
                        ],
                    },
                    "properties": {
                        "section": number,
                        "name": f"Sec {number}",
                        # A five-class spread, so the graduated case exercises
                        # every class boundary rather than two of them.
                        "net_pay": round(20.0 + 90.0 * ((number * 7) % 36) / 35.0, 1),
                    },
                }
            )
    return features


def geojson(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "geojson", "data": {"type": "FeatureCollection", "features": features}}


def base_style(sources: dict[str, Any], layers: list[dict[str, Any]]) -> dict[str, Any]:
    """A style with no sprite and no external glyphs.

    `glyphs` points at the deployment's own endpoint, which the render service
    allows; the label cases need it, and the ones without labels omit it so
    they render with nothing fetched at all.
    """
    return {"version": 8, "sources": sources, "layers": layers}


def write(name: str, spec: dict[str, Any]) -> None:
    """Compact JSON, not indented.

    A dense posting is 1,400 features, and pretty-printing it is a third of a
    megabyte of whitespace in the repository for a file no human reads. The
    fixture is regenerated by this script and reviewed by rendering it, which
    is the only review that would catch anything anyway.
    """
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(spec, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"  {name}.json  ({path.stat().st_size / 1024:.0f} kB)")


GLYPHS = "http://api:8000/static/glyphs/{fontstack}/{range}.pbf"


print("Writing visual fixtures:")

# 1. The label-placement canary.
write(
    "grid_with_contours_labeled",
    {
        "style": {
            **base_style(
                {"contours": geojson(contour_lines())},
                [
                    {
                        "id": "background",
                        "type": "background",
                        "paint": {"background-color": "#f4f1ea"},
                    },
                    {
                        "id": "contours",
                        "type": "line",
                        "source": "contours",
                        "paint": {
                            "line-color": "#6b4f2a",
                            "line-width": ["case", ["get", "is_index"], 1.8, 0.8],
                        },
                    },
                    {
                        "id": "contour-labels",
                        "type": "symbol",
                        "source": "contours",
                        "filter": ["get", "is_index"],
                        "layout": {
                            "text-field": ["get", "label"],
                            "text-font": ["Inter Regular"],
                            "text-size": 12,
                            # `08` §2.4: line-center, never point. Point
                            # placement anchors at the line's first vertex, so
                            # every label ends up on the edge of the map — the
                            # exact failure this case exists to catch.
                            "symbol-placement": "line-center",
                            "text-allow-overlap": True,
                            "text-ignore-placement": True,
                        },
                        "paint": {"text-color": "#3b2b15"},
                    },
                ],
            ),
            "glyphs": GLYPHS,
        },
        "bounds": BOUNDS,
        "size_preset": "slide_full",
    },
)

# 2. Draw order across source types.
write(
    "faults_over_grid",
    {
        "style": base_style(
            {"contours": geojson(contour_lines()), "faults": geojson(fault_lines())},
            [
                {
                    "id": "background",
                    "type": "background",
                    "paint": {"background-color": "#eef2f6"},
                },
                {
                    "id": "contours",
                    "type": "line",
                    "source": "contours",
                    "paint": {"line-color": "#9aa7b4", "line-width": 0.9},
                },
                {
                    "id": "faults",
                    "type": "line",
                    "source": "faults",
                    "paint": {"line-color": "#b3261e", "line-width": 2.4},
                },
            ],
        ),
        "bounds": BOUNDS,
        "size_preset": "slide_half",
    },
)

# 3. Symbol batching and overlap.
write(
    "dense_point_posting",
    {
        "style": base_style(
            {"wells": geojson(wells())},
            [
                {
                    "id": "background",
                    "type": "background",
                    "paint": {"background-color": "#ffffff"},
                },
                {
                    "id": "wells",
                    "type": "circle",
                    "source": "wells",
                    "paint": {
                        "circle-radius": 2.5,
                        "circle-color": "#1f4e79",
                        "circle-stroke-color": "#ffffff",
                        "circle-stroke-width": 0.5,
                    },
                },
            ],
        ),
        "bounds": BOUNDS,
        "size_preset": "slide_full",
    },
)

# 4. Class-boundary colours, byte for byte.
write(
    "graduated_polygons",
    {
        "style": base_style(
            {"sections": geojson(sections())},
            [
                {
                    "id": "background",
                    "type": "background",
                    "paint": {"background-color": "#ffffff"},
                },
                {
                    "id": "sections-fill",
                    "type": "fill",
                    "source": "sections",
                    "paint": {
                        "fill-color": [
                            "step",
                            ["get", "net_pay"],
                            "#fee5d9",
                            40,
                            "#fcae91",
                            60,
                            "#fb6a4a",
                            80,
                            "#de2d26",
                            100,
                            "#a50f15",
                        ],
                        "fill-outline-color": "#4a4f57",
                    },
                },
            ],
        ),
        "bounds": BOUNDS,
        "size_preset": "square",
    },
)

# 5. The legend overlay, which is HTML above the canvas.
write(
    "continuous_ramp_legend",
    {
        "style": base_style(
            {"sections": geojson(sections())},
            [
                {
                    "id": "background",
                    "type": "background",
                    "paint": {"background-color": "#ffffff"},
                },
                {
                    "id": "sections-fill",
                    "type": "fill",
                    "source": "sections",
                    "paint": {
                        "fill-color": [
                            "interpolate",
                            ["linear"],
                            ["get", "net_pay"],
                            20,
                            "#440154",
                            65,
                            "#21918c",
                            110,
                            "#fde725",
                        ]
                    },
                },
            ],
        ),
        "bounds": BOUNDS,
        "size_preset": "slide_half",
        "overlay": {
            "legend": {
                "kind": "colorbar",
                "title": "Net pay (ft)",
                "palette": {
                    "id": "viridis",
                    "name": "Viridis",
                    "isContinuous": True,
                    "interpolation": "linear",
                    "stops": [
                        {"position": 0.0, "color": "#440154"},
                        {"position": 0.5, "color": "#21918c"},
                        {"position": 1.0, "color": "#fde725"},
                    ],
                },
                "min": 20,
                "max": 110,
                "ticks": [20, 50, 80, 110],
            },
            "scaleBar": True,
            "northArrow": True,
        },
    },
)

# 6. Alpha survives the encode.
write(
    "transparent_background",
    {
        "style": base_style(
            {"faults": geojson(fault_lines())},
            [
                {
                    "id": "faults",
                    "type": "line",
                    "source": "faults",
                    "paint": {"line-color": "#b3261e", "line-width": 3.0},
                }
            ],
        ),
        "bounds": BOUNDS,
        "size_preset": "slide_quarter",
        "transparent": True,
    },
)

print(f"Wrote {len(list(OUT.glob('*.json')))} fixtures to {OUT}")
