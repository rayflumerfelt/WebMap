"""Reading and writing palette files. `08-styling-palettes.md` §5.1.

**Geologists have existing palettes and will insist on using them.** A tool
that supports only hand-built ramps gets rejected, so four formats are read:

| Format | Extension | Source |
|---|---|---|
| Surfer colour spec | `.clr` | Golden Software Surfer |
| GMT colour palette | `.cpt` | GMT, cpt-city |
| QGIS colour ramp | `.xml` | QGIS style exports |
| WebMap native | `.json` | Round-trip |

**One implementation, on the server.** The browser control uploads the text
rather than parsing it: a `.cpt` with hard breaks, named colours and B/F/N
lines is exactly the kind of format where two parsers disagree quietly, and the
disagreement surfaces as a map that looks slightly wrong rather than as an
error anybody can act on.

Every reader returns the same `Palette` — positions normalised to 0–1,
ascending, hex colours. Normalising here rather than storing each format's own
range is what lets `colour_at` be one function; the alternative is a
`source_format` field that every consumer has to branch on.
"""

from __future__ import annotations

import json
from itertools import pairwise
from typing import Any
from xml.etree import ElementTree

from webmap_core.style.palette import InvalidPalette, Palette, Stop, to_hex

#: Formats `read_palette` dispatches on, by extension.
FORMATS = ("clr", "cpt", "xml", "json")

#: A palette with more stops than this is a lookup table, not a ramp. GMT ships
#: 256-slice files routinely and they are legitimate; past this the file is
#: usually a mislabelled image export, and sampling it into a UI is hopeless.
MAX_STOPS = 1024

#: The GMT colour names that appear often enough to be worth handling. Not the
#: full X11 table: matching that would mean shipping a colour list here *and*
#: in TypeScript and keeping the two in step, and a `.cpt` using `papayawhip`
#: is a file whose author will not mind editing it.
GMT_COLOURS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "yellow": (255, 255, 0),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "orange": (255, 165, 0),
    "brown": (165, 42, 42),
    "purple": (128, 0, 128),
    "pink": (255, 192, 203),
}


def read_palette(text: str, fmt: str, *, name: str, palette_id: str) -> Palette:
    """Parse `text` in `fmt` into a palette.

    The format is passed rather than sniffed. Sniffing a `.clr` against a `.cpt`
    means guessing from the number of columns on the first data line, and the
    two overlap — a wrong guess produces a palette with plausible colours in
    the wrong places, which nobody spots until the map is printed.
    """
    readers = {
        "clr": read_clr,
        "cpt": read_cpt,
        "xml": read_qgis_xml,
        "json": read_json,
    }
    reader = readers.get(fmt.lower().lstrip("."))
    if reader is None:
        raise InvalidPalette(
            f"'{fmt}' is not a palette format. WebMap reads .clr (Surfer), "
            f".cpt (GMT), .xml (QGIS colour ramp) and .json (WebMap's own)."
        )
    palette = reader(text)
    return _finish(palette, name=name, palette_id=palette_id)


# --- Surfer .clr ----------------------------------------------------------------


def read_clr(text: str) -> Palette:
    """Surfer `.clr`.

    Lines of `position red green blue [alpha]`, **positions 0–100**, with a
    `ColorMap` header. Surfer writes the header, cpt-city conversions often do
    not, and both are accepted — the header carries nothing this reader needs.

    Alpha is read and **discarded**, deliberately. A palette's job is the colour
    ramp; per-stop transparency belongs to the layer's opacity, which the
    formatting dialog owns and which a palette imported into three layers must
    not silently override.
    """
    stops: list[Stop] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", "//")) or line.lower().startswith("colormap"):
            continue

        parts = line.replace(",", " ").split()
        if len(parts) < 4:
            raise InvalidPalette(
                f"Line {number} of this .clr file has {len(parts)} value(s): "
                f"'{line}'. Surfer colour specs are 'position red green blue' "
                f"with positions from 0 to 100."
            )
        try:
            position, red, green, blue = (float(value) for value in parts[:4])
        except ValueError as error:
            raise InvalidPalette(
                f"Line {number} of this .clr file is not numeric: '{line}'."
            ) from error

        stops.append({"position": position / 100.0, "color": _rgb_to_hex(red, green, blue)})

    if not stops:
        raise InvalidPalette(
            "This .clr file has no colour lines. A Surfer colour spec is one "
            "'position red green blue' line per stop, positions 0 to 100."
        )
    return {"stops": stops, "interpolation": "linear", "isContinuous": True}


# --- GMT .cpt -------------------------------------------------------------------

#: A `.cpt` slice line: two z-values with a colour each. The colour is one
#: token (`#rrggbb`, a name, or `r/g/b`) or three whitespace-separated numbers,
#: which is why this is parsed by token count rather than by regex.
_CPT_SPECIAL = ("B", "F", "N")


def read_cpt(text: str) -> Palette:
    """GMT `.cpt`.

    Each data line is a **slice**: `z0 colour0 z1 colour1`. Slices are adjacent,
    so consecutive ones share a boundary and the palette is the sequence of
    slice ends with the duplicates collapsed.

    Three forms of colour appear in the wild and all three are read: `r g b` as
    three tokens, `r/g/b` as one, and `#rrggbb`, plus the named colours in
    `GMT_COLOURS`.

    **Hard breaks are honoured.** A line ending in `;` — or a file whose slices
    name the same colour at both ends — is discrete, and importing it as a
    continuous ramp would smooth away the very boundaries the author drew.

    The `B`, `F` and `N` lines are background, foreground and NaN. They are
    read and **kept out of the stops**: they are clamp and nodata colours, not
    positions on the ramp, and folding them in would add a stop at each end
    that the file never had.
    """
    stops: list[Stop] = []
    discrete = False
    saw_slice = False

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.endswith(";"):
            # A trailing `;` introduces a label, and in GMT's own convention
            # marks the slice as a hard break rather than a blend.
            discrete = True
            line = line[:-1].strip()

        tokens = line.split()
        if tokens[0] in _CPT_SPECIAL:
            continue  # background / foreground / NaN — see the docstring.

        low, low_colour, rest = _cpt_side(tokens, number)
        high, high_colour, _ = _cpt_side(rest, number)
        saw_slice = True

        if low_colour == high_colour:
            discrete = True

        stops.append({"position": low, "color": low_colour})
        stops.append({"position": high, "color": high_colour})

    if not saw_slice:
        raise InvalidPalette(
            "This .cpt file has no slice lines. A GMT colour palette is "
            "'z0 colour0 z1 colour1' per line, with optional B/F/N lines for "
            "background, foreground and NaN."
        )

    return {
        "stops": _collapse(stops),
        "interpolation": "discrete" if discrete else "linear",
        "isContinuous": not discrete,
    }


def _cpt_side(tokens: list[str], line_number: int) -> tuple[float, str, list[str]]:
    """One `z colour` pair off the front of a `.cpt` line, and what is left."""
    if not tokens:
        raise InvalidPalette(
            f"Line {line_number} of this .cpt file ends after a z value; each "
            f"slice needs 'z0 colour0 z1 colour1'."
        )
    try:
        z = float(tokens[0])
    except ValueError as error:
        raise InvalidPalette(
            f"Line {line_number} of this .cpt file starts with '{tokens[0]}', "
            f"which is not a z value."
        ) from error

    rest = tokens[1:]
    if not rest:
        raise InvalidPalette(
            f"Line {line_number} of this .cpt file has a z value with no colour."
        )

    # Three numeric tokens is `r g b`; anything else is a single-token colour.
    if len(rest) >= 3 and all(_is_number(token) for token in rest[:3]):
        colour = _rgb_to_hex(float(rest[0]), float(rest[1]), float(rest[2]))
        return z, colour, rest[3:]
    return z, _cpt_colour(rest[0], line_number), rest[1:]


def _cpt_colour(token: str, line_number: int) -> str:
    if token.startswith("#"):
        return token.lower()
    if "/" in token:
        parts = token.split("/")
        if len(parts) == 3 and all(_is_number(part) for part in parts):
            return _rgb_to_hex(float(parts[0]), float(parts[1]), float(parts[2]))
    named = GMT_COLOURS.get(token.lower())
    if named is not None:
        return _rgb_to_hex(*named)
    if _is_number(token):
        # A single number is a grey level in GMT.
        value = float(token)
        return _rgb_to_hex(value, value, value)
    raise InvalidPalette(
        f"Line {line_number} of this .cpt file names the colour '{token}', "
        f"which WebMap does not recognise. Use #rrggbb, 'r/g/b', three numbers, "
        f"or one of: {', '.join(sorted(GMT_COLOURS))}."
    )


# --- QGIS .xml -------------------------------------------------------------------


def read_qgis_xml(text: str) -> Palette:
    """A QGIS colour-ramp export.

    QGIS stores a gradient ramp as `<colorramp>` with `color1`, `color2` and a
    `stops` property — `"0.25;255,0,0,255:0.5;0,255,0,255"` — where each entry
    is `position;r,g,b,a` and entries are colon-separated. The endpoints live in
    their own properties rather than in `stops`, which is the part that catches
    people: a reader that only walks `stops` produces a ramp missing both ends.

    A style file can hold several ramps. The **first** is taken, because the
    export a user drags in is normally the one ramp they were looking at, and
    picking silently from several would import something they did not choose.
    """
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise InvalidPalette(
            f"This file is not valid XML ({error}). A QGIS colour ramp is "
            f"exported from the Style Manager as an .xml file."
        ) from error

    ramp = root.find(".//colorramp") if root.tag != "colorramp" else root
    if ramp is None:
        raise InvalidPalette(
            "No <colorramp> in this XML. WebMap reads a QGIS colour-ramp "
            "export; a full .qml layer style is a different document and "
            "carries a whole symbology rather than a ramp."
        )

    properties = _qgis_properties(ramp)
    first = properties.get("color1")
    last = properties.get("color2")
    if first is None or last is None:
        raise InvalidPalette(
            "This colour ramp has no color1/color2 endpoints. Only gradient "
            "ramps can be imported; a 'random' or 'catalog' ramp has no fixed "
            "colours to read."
        )

    stops: list[Stop] = [{"position": 0.0, "color": _qgis_colour(first)}]
    for entry in (properties.get("stops") or "").split(":"):
        if not entry.strip():
            continue
        position, _, colour = entry.partition(";")
        try:
            stops.append({"position": float(position), "color": _qgis_colour(colour)})
        except ValueError as error:
            raise InvalidPalette(
                f"'{entry}' is not a QGIS ramp stop; each is 'position;r,g,b,a'."
            ) from error
    stops.append({"position": 1.0, "color": _qgis_colour(last)})

    discrete = properties.get("discrete", "0") in {"1", "true"}
    return {
        "stops": stops,
        "interpolation": "discrete" if discrete else "linear",
        "isContinuous": not discrete,
    }


def _qgis_properties(ramp: Any) -> dict[str, str]:
    """QGIS has written properties two ways across versions; read both.

    Older files use `<prop k="..." v="..."/>`, newer ones
    `<Option name="..." value="..."/>`. A reader that knows only one silently
    finds no endpoints on half the files people have.
    """
    properties: dict[str, str] = {}
    for element in ramp.iter():
        if element.tag == "prop":
            key, value = element.get("k"), element.get("v")
        elif element.tag == "Option":
            key, value = element.get("name"), element.get("value")
        else:
            continue
        if key is not None and value is not None:
            properties[key] = value
    return properties


def _qgis_colour(value: str) -> str:
    """`r,g,b,a` — or `r,g,b` — as QGIS writes it. Alpha discarded, as in .clr."""
    parts = [part for part in value.strip().split(",") if part != ""]
    if len(parts) < 3 or not all(_is_number(part) for part in parts[:3]):
        raise InvalidPalette(
            f"'{value}' is not a QGIS colour; they are written 'r,g,b,a' with "
            f"each channel from 0 to 255."
        )
    return _rgb_to_hex(float(parts[0]), float(parts[1]), float(parts[2]))


# --- WebMap .json ----------------------------------------------------------------


def read_json(text: str) -> Palette:
    """WebMap's own format, for round-tripping.

    Validated rather than trusted: this path is reached by uploading a file,
    which is exactly where a hand-edited or truncated document arrives.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise InvalidPalette(f"This file is not valid JSON ({error}).") from error
    if not isinstance(document, dict) or not isinstance(document.get("stops"), list):
        raise InvalidPalette(
            "A WebMap palette is an object with a 'stops' array of {position, color} entries."
        )

    stops: list[Stop] = []
    for entry in document["stops"]:
        if not isinstance(entry, dict) or "position" not in entry or "color" not in entry:
            raise InvalidPalette(
                f"'{entry}' is not a palette stop; each is "
                f"{{'position': 0..1, 'color': '#rrggbb'}}."
            )
        stops.append(
            {"position": float(entry["position"]), "color": str(entry["color"]).lower()}
        )

    interpolation = str(document.get("interpolation", "linear"))
    return {
        "stops": stops,
        "interpolation": interpolation,
        "isContinuous": interpolation != "discrete",
    }


def write_json(palette: Palette) -> str:
    """The round-trip format. Sorted keys so a diff between two exports is
    about the colours rather than about dictionary order."""
    return json.dumps(palette, indent=2, sort_keys=True)


def write_clr(palette: Palette) -> str:
    """Surfer `.clr`, for handing a WebMap palette back to Surfer.

    Positions go back to 0–100 and alpha is written as 255: Surfer expects the
    column, and WebMap has no per-stop alpha to put there.
    """
    from webmap_core.style.palette import parse_hex

    lines = ["ColorMap 2 1"]
    for stop in _sorted(palette):
        # `parse_hex` returns 0–255 already; scaling again is the mistake this
        # module made once and the reason `_rgb_to_hex` says so out loud.
        red, green, blue = parse_hex(stop["color"])
        lines.append(
            f"{stop['position'] * 100:.6g} {round(red)} {round(green)} {round(blue)} 255"
        )
    return "\n".join(lines) + "\n"


def write_cpt(palette: Palette) -> str:
    """GMT `.cpt` over 0–1, one slice per adjacent pair of stops.

    Written over the normalised range rather than over any data range: a
    palette is a ramp, and a `.cpt` carrying somebody's structure values would
    be wrong for every other map it is used on. GMT's `makecpt -T` is how a
    range gets attached, and doing it here would guess one.
    """
    from webmap_core.style.palette import parse_hex

    def channels(colour: str) -> str:
        red, green, blue = parse_hex(colour)
        return f"{round(red)}/{round(green)}/{round(blue)}"

    stops = _sorted(palette)
    hard = ";" if palette.get("interpolation") == "discrete" else ""
    lines = ["# WebMap palette export"]
    for low, high in pairwise(stops):
        lines.append(
            f"{low['position']:.6g} {channels(low['color'])} "
            f"{high['position']:.6g} {channels(high['color'])}{hard}"
        )
    lines.append(f"B {channels(stops[0]['color'])}")
    lines.append(f"F {channels(stops[-1]['color'])}")
    lines.append("N 128/128/128")
    return "\n".join(lines) + "\n"


def write_palette(palette: Palette, fmt: str) -> str:
    writers = {"clr": write_clr, "cpt": write_cpt, "json": write_json}
    writer = writers.get(fmt.lower().lstrip("."))
    if writer is None:
        raise InvalidPalette(
            f"WebMap exports .clr, .cpt and .json; '{fmt}' is not one of them. "
            f"QGIS ramps are read but not written — a QGIS style file carries "
            f"more than a ramp, and producing a partial one would be worse "
            f"than producing none."
        )
    return writer(palette)


# --- shared ---------------------------------------------------------------------


def _finish(palette: Palette, *, name: str, palette_id: str) -> Palette:
    """Normalise positions, sort, and check the result is usable.

    **Positions are rescaled to 0–1 by their own range**, not divided by a
    constant. A `.cpt` written over depths of -10,000 to -7,500 is a perfectly
    good ramp and its z-values mean nothing to WebMap: what matters is the
    order and the spacing. Rescaling here is what makes one `colour_at` work
    for every source format.
    """
    stops = _collapse(_sorted(palette))
    if len(stops) < 2:
        raise InvalidPalette(
            f"This palette has {len(stops)} distinct stop(s). A ramp needs at "
            f"least two, or there is nothing to interpolate between."
        )
    if len(stops) > MAX_STOPS:
        raise InvalidPalette(
            f"This palette has {len(stops):,} stops (limit {MAX_STOPS:,}). Past "
            f"that it is a lookup table rather than a ramp, and usually a "
            f"mislabelled image export."
        )

    low = stops[0]["position"]
    high = stops[-1]["position"]
    span = high - low
    if span <= 0:
        # A backstop rather than a reachable path today: `_collapse` merges
        # stops at one position, so a file whose stops all sit together already
        # fails the "at least two" check above. Kept because the two guards
        # protect different invariants, and a future change to `_collapse` that
        # stopped merging would otherwise divide by zero here.
        raise InvalidPalette(
            "Every stop in this palette is at the same position, so it has no "
            "range to interpolate across."
        )

    return {
        "id": palette_id,
        "name": name,
        "isContinuous": bool(palette.get("isContinuous", True)),
        "interpolation": str(palette.get("interpolation", "linear")),
        "stops": [
            {"position": (stop["position"] - low) / span, "color": stop["color"]}
            for stop in stops
        ],
    }


def _sorted(palette: Palette) -> list[Stop]:
    return sorted(palette.get("stops", []), key=lambda stop: stop["position"])


def _collapse(stops: list[Stop]) -> list[Stop]:
    """Drop the duplicate boundary a slice format produces.

    A `.cpt` writes each slice's two ends, so consecutive slices repeat the
    shared boundary. Kept, the palette has two stops at one position and
    `colour_at` divides by a zero interval.
    """
    ordered = sorted(stops, key=lambda stop: stop["position"])
    collapsed: list[Stop] = []
    for stop in ordered:
        if collapsed and _close(collapsed[-1]["position"], stop["position"]):
            if collapsed[-1]["color"] == stop["color"]:
                continue
            # Two colours at one position is a hard break, and both are kept:
            # that is what makes a discrete ramp step rather than blend. The
            # later one wins for anything reading a single value there.
            collapsed[-1] = stop
            continue
        collapsed.append(stop)
    return collapsed


def _close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _rgb_to_hex(red: float, green: float, blue: float) -> str:
    """0–255 channels to `#rrggbb`, through `to_hex` so the rounding matches.

    **`Rgb` in `palette.py` is 0–255, not 0–1** — `parse_hex` returns byte
    channels and `to_hex` takes them. Scaling here would be wrong in both
    directions and wrong quietly: dividing by 255 first made every imported
    colour come out within one step of black, which reads as a broken file
    rather than as a broken reader.

    `to_hex` is shared with `packages/style-model/src/palette.ts` and rounds
    half away from zero rather than half-to-even; going through it is what keeps
    an imported palette identical in the browser and in a render.
    """
    for channel, value in (("red", red), ("green", green), ("blue", blue)):
        if not 0 <= value <= 255:
            raise InvalidPalette(
                f"A {channel} channel of {value:g} is outside 0–255. Palette "
                f"files write channels as bytes."
            )
    return to_hex((red, green, blue))


__all__ = [
    "FORMATS",
    "GMT_COLOURS",
    "MAX_STOPS",
    "read_clr",
    "read_cpt",
    "read_json",
    "read_palette",
    "read_qgis_xml",
    "write_clr",
    "write_cpt",
    "write_json",
    "write_palette",
]
