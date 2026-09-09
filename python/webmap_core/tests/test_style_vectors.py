"""The Python half of the cross-language suite. `08-styling-palettes.md` §3.1.

Every vector in `packages/style-model/test-vectors/` runs here and in
`packages/style-model/src/compile.test.ts` against the same hand-written
expected output. A divergence fails CI in both languages, which is the
mechanism that keeps two compilers from drifting.

The vectors live under `packages/` rather than being copied here on purpose:
two copies of the expected output would let the two suites pass while
disagreeing, which is the exact failure the parity job exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from webmap_core.style.compile import InvalidSymbology, compile_symbology
from webmap_core.style.palette import InvalidPalette, Palette, colour_at, sample_ramp

VECTOR_DIR = Path(__file__).resolve().parents[3] / "packages" / "style-model" / "test-vectors"


def load_vectors() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(VECTOR_DIR.glob("*.json"))
    ]


VECTORS = load_vectors()


def compile_vector(vector: dict[str, Any]) -> list[dict[str, Any]]:
    return compile_symbology(
        vector["symbology"],
        source_id=vector["source_id"],
        source_layer=vector.get("source_layer"),
        palettes=vector["palettes"],
    )


def test_the_shared_vectors_are_found() -> None:
    """A suite that silently runs zero cases passes.

    This is the guard against the vector directory moving — the path crosses
    from `python/` into `packages/`, so it is more fragile than most.
    """
    assert VECTOR_DIR.is_dir(), f"No vectors at {VECTOR_DIR}"
    assert len(VECTORS) >= 9


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: str(v["name"]))
def test_vector_compiles_to_the_expected_layers(vector: dict[str, Any]) -> None:
    assert compile_vector(vector) == vector["expected_layers"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: str(v["name"]))
def test_vector_produces_layers_in_a_stable_order(vector: dict[str, Any]) -> None:
    """Order is load-bearing: a polygon's outline must paint over its fill, and
    a line's casing must paint under its line. Comparing the id list separately
    makes an ordering regression report as ordering rather than as a wall of
    paint-property diffs."""
    compiled = [layer["id"] for layer in compile_vector(vector)]

    assert compiled == [layer["id"] for layer in vector["expected_layers"]]


# --- palette sampling -------------------------------------------------------


VIRIDIS: Palette = {
    "id": "viridis",
    "name": "Viridis",
    "isContinuous": True,
    "interpolation": "linear",
    "stops": [
        {"position": 0.0, "color": "#440154"},
        {"position": 0.5, "color": "#21918c"},
        {"position": 1.0, "color": "#fde725"},
    ],
}


def test_the_first_and_last_classes_get_the_ends_of_the_ramp() -> None:
    colours = sample_ramp(VIRIDIS, 5)

    assert colours[0] == "#440154"
    assert colours[4] == "#fde725"
    assert len(colours) == 5


def test_a_single_class_takes_the_midpoint_not_an_extreme() -> None:
    """The ends of a diverging ramp are its extremes. One class coloured
    "extreme low" would read as a value judgement the data does not support."""
    assert sample_ramp(VIRIDIS, 1) == ["#21918c"]


def test_positions_outside_the_range_are_clamped_not_extrapolated() -> None:
    assert colour_at(VIRIDIS, -1.0) == "#440154"
    assert colour_at(VIRIDIS, 2.0) == "#fde725"


def test_a_discrete_palette_steps_at_stop_boundaries() -> None:
    """A class boundary is a step. Blurring it would make the legend a lie in
    the same way interpolating a graduated ramp would."""
    discrete: Palette = {**VIRIDIS, "interpolation": "discrete"}

    assert colour_at(discrete, 0.25) == "#440154"
    assert colour_at(discrete, 0.75) == "#21918c"
    assert colour_at(discrete, 1.0) == "#fde725"


def test_three_digit_hex_is_accepted_and_normalised() -> None:
    """One spelling, lowercase, so string comparison against the TypeScript
    implementation is meaningful."""
    short: Palette = {
        **VIRIDIS,
        "stops": [
            {"position": 0.0, "color": "#F00"},
            {"position": 1.0, "color": "#00F"},
        ],
    }

    assert colour_at(short, 0.0) == "#ff0000"
    assert colour_at(short, 1.0) == "#0000ff"


def test_coincident_stops_take_the_upper_colour() -> None:
    """Coincident stops are how a hard break is expressed in a continuous ramp.

    Taking the last stop at the position makes the break sharp, and matches the
    discrete rule rather than contradicting it.
    """
    hard_break: Palette = {
        **VIRIDIS,
        "stops": [
            {"position": 0.0, "color": "#000000"},
            {"position": 0.5, "color": "#ffffff"},
            {"position": 0.5, "color": "#ff0000"},
            {"position": 1.0, "color": "#00ff00"},
        ],
    }

    assert colour_at(hard_break, 0.5) == "#ff0000"


def test_a_colour_the_other_language_cannot_reproduce_is_refused() -> None:
    named: Palette = {
        **VIRIDIS,
        "stops": [
            {"position": 0.0, "color": "rebeccapurple"},
            {"position": 1.0, "color": "#ffffff"},
        ],
    }

    with pytest.raises(InvalidPalette, match="not a hex colour"):
        colour_at(named, 0.0)


def test_channel_ties_round_half_away_from_zero() -> None:
    """**The bug the vectors caught on their first run.**

    Sampling viridis at 0.25 puts the red channel at exactly 50.5. Python's
    built-in `round` is half-to-even and would give 50 (`#32...`) where
    JavaScript's `Math.round` gives 51 (`#33...`). Every graduated layer would
    then differ by one bit between the interactive map and a render.

    Asserted directly, not only through a vector, so that the reason survives
    if the viridis vector is ever rewritten.
    """
    assert round(50.5) == 50, "Python's round is half-to-even — the trap this guards"

    assert sample_ramp(VIRIDIS, 5)[1] == "#334970"
    assert sample_ramp(VIRIDIS, 5)[3] == "#8fbc59"


# --- graduated guards -------------------------------------------------------


FLAT: Palette = {
    "id": "p",
    "name": "p",
    "isContinuous": True,
    "interpolation": "linear",
    "stops": [
        {"position": 0.0, "color": "#000000"},
        {"position": 1.0, "color": "#ffffff"},
    ],
}

POLYGON = {
    "geometry": "polygon",
    "fillColor": "#cccccc",
    "fillOpacity": 1.0,
    "outlineColor": "#000000",
    "outlineWidth": 0,
}


def test_a_break_count_disagreeing_with_the_class_count_is_refused() -> None:
    """The off-by-one 08 §8 calls out: classify() returns n-1 interior breaks
    for n classes, and a mismatch renders a 5-class map with a 4-entry legend.
    Caught at compile rather than discovered on a slide."""
    symbology = {
        "type": "graduated",
        "field": "porosity",
        "method": "pretty",
        "classCount": 5,
        "breaks": [5, 10, 15],
        "paletteId": "p",
        "vary": "color",
        "baseSymbol": POLYGON,
    }

    with pytest.raises(InvalidSymbology, match="5 classes but 3 breaks"):
        compile_symbology(symbology, source_id="s", palettes={"p": FLAT})


def test_a_missing_palette_is_named_rather_than_rendered_grey() -> None:
    symbology = {
        "type": "graduated",
        "field": "porosity",
        "method": "pretty",
        "classCount": 2,
        "breaks": [5],
        "paletteId": "absent",
        "vary": "color",
        "baseSymbol": POLYGON,
    }

    with pytest.raises(InvalidSymbology, match="palette 'absent'"):
        compile_symbology(symbology, source_id="s", palettes={})
