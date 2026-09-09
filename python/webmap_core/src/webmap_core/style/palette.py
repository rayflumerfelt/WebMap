"""Palette sampling. `08-styling-palettes.md` §5.

**This file and `packages/style-model/src/palette.ts` must agree exactly.**
They are the lowest layer of the two-implementation compiler in §3.1, so a
one-bit difference here shows up as every graduated layer differing between
the interactive map and a render — precisely the drift the shared vectors
exist to catch.

Everything below is specified rather than idiomatic, because "idiomatic in
Python" and "idiomatic in TypeScript" round differently. The most important
instance is in `to_hex`: this module never calls the built-in ``round``.
"""

from __future__ import annotations

import math
import re
from typing import TypedDict

from webmap_core.exceptions import WebMapError


class InvalidPalette(WebMapError):
    """A palette cannot be sampled as given."""


class Stop(TypedDict):
    position: float
    color: str


class Palette(TypedDict, total=False):
    """The `Palette` of `08-styling-palettes.md` §2, as it arrives on the wire.

    Keys are the TypeScript spelling because this *is* the stored shape — the
    symbology JSON the frontend writes is what the backend reads. Renaming it
    to snake_case at the boundary would create a second vocabulary for the same
    document and a translation layer that could itself drift.
    """

    id: str
    name: str
    isContinuous: bool
    stops: list[Stop]
    interpolation: str  # "linear" | "discrete"


#: `#rgb` or `#rrggbb`. Named colours and `rgb()` are refused: matching them
#: would mean shipping a colour table in both languages and keeping the two in
#: step, for a spelling nobody needs.
_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

Rgb = tuple[float, float, float]


def parse_hex(value: str) -> Rgb:
    match = _HEX.match(value.strip())
    if match is None:
        raise InvalidPalette(
            f"'{value}' is not a hex colour. Palette stops are #rgb or #rrggbb; "
            f"named colours and rgb() are not accepted because the TypeScript "
            f"compiler would have to reproduce a colour table to match."
        )
    digits = match.group(1).lower()
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return (
        float(int(digits[0:2], 16)),
        float(int(digits[2:4], 16)),
        float(int(digits[4:6], 16)),
    )


def to_hex(rgb: Rgb) -> str:
    """`#rrggbb`, lowercase, rounding **half away from zero**.

    `math.floor(v + 0.5)`, never the built-in `round`. Python's `round` is
    half-to-even, so `round(50.5)` is `50` where JavaScript's `Math.round(50.5)`
    is `51` — and ties are not rare: sampling a ramp at a class-boundary
    midpoint produces one whenever two stop channels differ by an odd number.
    Two of the five classes in the `graduated_polygons` vector do exactly that,
    which is how this was found.
    """
    return "#" + "".join(f"{math.floor(_clamp(c, 0.0, 255.0) + 0.5):02x}" for c in rgb)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def colour_at(palette: Palette, position: float) -> str:
    """The colour at `position` (0..1) along a palette.

    Interpolates in sRGB, not a perceptual space. That is the wrong choice for
    *designing* a ramp and the right one here: the ramps geologists import from
    Surfer and GMT are defined as sRGB stops, and interpolating them anywhere
    else would render them differently from the tool they came from.

    `discrete` palettes take the colour of the stop at or below the position —
    a class boundary is a step, and blurring it would make the legend a lie.
    """
    stops = _sorted_stops(palette)
    target = _clamp(position, 0.0, 1.0)

    if not stops:
        raise InvalidPalette(
            f"Palette '{palette.get('id', '?')}' has no stops, so it defines no "
            f"colours. Add at least one stop, or pick a different palette."
        )
    if len(stops) == 1:
        return to_hex(parse_hex(stops[0]["color"]))

    if palette.get("interpolation") == "discrete":
        chosen = stops[0]
        for stop in stops:
            if stop["position"] <= target:
                chosen = stop
        return to_hex(parse_hex(chosen["color"]))

    # A stop sitting exactly on the requested position wins outright, and the
    # *last* such stop wins. That is what makes a coincident pair read as a hard
    # break in an otherwise continuous ramp: values at and above the break take
    # the upper colour, matching the `discrete` rule above rather than
    # contradicting it. It also means the interpolation below always has a
    # strictly positive span, so there is no divide-by-zero case to handle.
    for stop in reversed(stops):
        if stop["position"] == target:
            return to_hex(parse_hex(stop["color"]))

    upper_index = next((i for i, s in enumerate(stops) if s["position"] > target), -1)
    if upper_index <= 0:
        # Outside the stop range entirely — clamped to an end.
        return to_hex(parse_hex(stops[0 if upper_index == 0 else -1]["color"]))

    lower = stops[upper_index - 1]
    upper = stops[upper_index]
    t = (target - lower["position"]) / (upper["position"] - lower["position"])

    a = parse_hex(lower["color"])
    b = parse_hex(upper["color"])
    return to_hex(
        (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)
    )


def sample_ramp(palette: Palette, count: int) -> list[str]:
    """`count` colours spanning the palette, for a graduated or categorized layer.

    Positions are `i / (count - 1)`, so the first and last classes get the ends
    of the ramp. A single class takes the midpoint: the ends of a diverging ramp
    are its extremes, and one class coloured "extreme low" would read as a value
    judgement the data does not support.
    """
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise InvalidPalette(f"Class count must be a positive integer; got {count!r}.")
    if count == 1:
        return [colour_at(palette, 0.5)]
    return [colour_at(palette, i / (count - 1)) for i in range(count)]


def _sorted_stops(palette: Palette) -> list[Stop]:
    # Sorted by position, ties broken by original order so a palette with
    # coincident stops compiles the same way in both languages. Python's sort
    # is stable and JavaScript's has been since ES2019, so "stable sort by
    # position" means the same thing on both sides.
    return sorted(palette.get("stops", []), key=lambda s: s["position"])


__all__ = [
    "InvalidPalette",
    "Palette",
    "Stop",
    "colour_at",
    "parse_hex",
    "sample_ramp",
    "to_hex",
]
