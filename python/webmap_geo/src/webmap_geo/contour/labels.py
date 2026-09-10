"""Contour labels, and the gaps they sit in. `adr/0015`, `05-geoprocessing.md` §7.

A contour that runs through its own value is harder to read at exactly the
moment somebody is reading values off the map, so every tool a geologist has
used breaks the line for a character or two either side of the label. MapLibre
cannot: nothing in the style spec expresses it. So the break is cut here, into
the geometry, and the label is written beside it as a point with a bearing.

Three rules, each of which is a way this goes wrong:

**A line too short to hold a label is left whole.** Cutting a gap out of a short
contour leaves two stubs or nothing at all. Below the threshold the geometry is
untouched and no label is emitted — the caller gets a line and no anchor, which
is exactly what it should draw.

**A closed contour stays closed where it can.** A ring cut at one gap is *one*
open line whose two ends are the gap. Returning two pieces with a seam at the
ring's arbitrary start vertex would put a visible break somewhere nobody chose.

**A label never reads upside down.** The bearing is normalised into a half-open
half-turn, so the same physical contour digitised in either direction labels the
same way. Without that, two adjacent contours traced in opposite directions read
in opposite directions, which looks like a bug in the renderer.

Everything here is in the **analysis frame's units** and never transforms
(`adr/0003`). The gap length arrives in those units too, because a gap is a
distance on the ground and the caller is the only party that knows the scale the
map will be read at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import LineString, Point
from shapely.ops import substring

#: Character width as a fraction of text size, for a proportional sans at the
#: sizes labels are drawn in. Digits, a comma and a minus sign are what a
#: contour label is made of and they sit close to this; measuring the real
#: glyph advances would mean carrying font metrics into geoprocessing for a
#: number that only has to be right to a character's width.
CHARACTER_WIDTH_RATIO = 0.58

#: Blank space either side of the text, in characters. Two is what reads as a
#: deliberate break rather than as a line that failed to draw.
PADDING_CHARACTERS = 1.0

#: A contour must be this many gap-lengths long before it is worth cutting.
#: Three leaves at least a gap's worth of line on each side of the label.
MIN_LENGTH_IN_GAPS = 3.0

#: Within this many degrees of vertical, a label is drawn vertical.
#:
#: **The reason is a flip, not a tilt.** Any way of normalising a direction into
#: half a turn has a discontinuity somewhere, and on a structure map the
#: contours are near-parallel and mostly steep — so the discontinuity lands
#: exactly where the data is. Two contours a fifth of a degree apart came out at
#: +89.9 and -89.9: both perfectly readable, one reading up the line and the
#: other down it, on the same map. Snapping the band to one convention costs at
#: most ten degrees of tilt, which nobody sees, and removes the disagreement,
#: which everybody does.
VERTICAL_BAND_DEG = 80.0

#: How far along the line the tangent is measured, as a fraction of the gap.
#: Measured across the gap rather than between adjacent vertices: a contour
#: from a noisy grid wiggles vertex to vertex, and a bearing taken from one
#: short segment makes the label wander while the line does not.
TANGENT_WINDOW = 0.5


@dataclass(frozen=True)
class LineLabel:
    """Where a contour label goes, and which way up."""

    point: Point
    #: Degrees clockwise, for MapLibre's `text-rotate`, in (-90, 90]. Text at
    #: this rotation follows the line and stays readable.
    bearing: float
    value: float


@dataclass(frozen=True)
class LabelledContour:
    """One contour after labelling: its geometry, cut, and its labels."""

    #: The line, split at each gap. One element when nothing was cut.
    pieces: list[LineString]
    labels: list[LineLabel]


def gap_length(text: str, metres_per_pixel: float, text_size_px: float = 12.0) -> float:
    """How long a gap the text needs, in the frame's own units.

    `metres_per_pixel` is the reference scale, and it is required rather than
    defaulted: a label is a fixed number of pixels wide and a gap is a fixed
    number of feet, so the two agree at exactly one scale and only the caller
    knows which (`adr/0015`).

    The name says metres because that is the usual unit, but the arithmetic is
    unit-agnostic — pass feet per pixel for a State Plane frame and get feet.
    """
    if metres_per_pixel <= 0:
        raise ValueError(
            f"metres_per_pixel must be positive, got {metres_per_pixel}. It is "
            f"the reference scale the gap is cut for; there is no sensible "
            f"default, so the caller has to name one."
        )
    characters = len(text) + 2 * PADDING_CHARACTERS
    return characters * text_size_px * CHARACTER_WIDTH_RATIO * metres_per_pixel


def label_contour(
    line: LineString,
    value: float,
    *,
    gap: float,
    spacing: float,
    text: str | None = None,
) -> LabelledContour:
    """Cut gaps into one contour and place a label in each.

    `spacing` is the distance between labels along the line. The first label
    sits half a spacing in, so a contour long enough for exactly one label gets
    it in the middle rather than at an end.

    A line shorter than `MIN_LENGTH_IN_GAPS` gaps is returned whole with no
    labels: cutting it would leave stubs.
    """
    del text  # Only the length matters, and the caller has already used it.
    if gap <= 0:
        raise ValueError(f"gap must be positive, got {gap}.")
    if spacing <= 0:
        raise ValueError(f"spacing must be positive, got {spacing}.")
    if spacing <= gap:
        raise ValueError(
            f"Labels spaced {spacing} apart cannot each sit in a gap {gap} long: "
            f"the gaps would overlap and the contour would be more break than "
            f"line. Widen the spacing, or cut the gap for a larger scale."
        )

    length = line.length
    if length < MIN_LENGTH_IN_GAPS * gap:
        return LabelledContour(pieces=[line], labels=[])

    closed = _is_closed(line)
    anchors = _anchor_distances(length, gap=gap, spacing=spacing, closed=closed)
    if not anchors:
        return LabelledContour(pieces=[line], labels=[])

    labels = [
        LineLabel(
            point=line.interpolate(distance),
            bearing=_bearing_at(line, distance, window=gap * TANGENT_WINDOW),
            value=value,
        )
        for distance in anchors
    ]
    return LabelledContour(pieces=_cut(line, anchors, gap, closed), labels=labels)


#: How close the ends must be for a line to count as a ring, as a fraction of
#: its length. A contour traced from a grid closes to floating point rather
#: than exactly — `sin(2π)` is 2.4e-16, not zero — and an exact comparison
#: therefore calls almost every closed contour open, cuts it as one, and puts
#: a break at the tracer's start vertex. Which is the whole thing `adr/0015`
#: says not to do.
RING_TOLERANCE = 1e-9


def _is_closed(line: LineString) -> bool:
    if line.is_ring:
        return True
    first, last = line.coords[0], line.coords[-1]
    separation = math.dist(first[:2], last[:2])
    return separation <= RING_TOLERANCE * line.length


def _anchor_distances(
    length: float, *, gap: float, spacing: float, closed: bool
) -> list[float]:
    """Where the labels go, as distances along the line.

    On an open line the first anchor is half a spacing in and every anchor
    keeps a gap's length clear of both ends — a gap cut at the very end is not
    a break in the line, it is a shortened line.

    On a closed line there are no ends to keep clear, so anchors start at zero
    and wrap with the ring.
    """
    anchors: list[float] = []
    distance = 0.0 if closed else spacing / 2.0
    # Strictly less than the length on a ring: 0 and L are the same point, and
    # an anchor at each would cut one gap twice and label it twice.
    limit = length - gap
    while distance < length if closed else distance <= limit:
        if closed or distance >= gap:
            anchors.append(distance)
        distance += spacing
    return anchors


def _bearing_at(line: LineString, distance: float, *, window: float) -> float:
    """The line's direction at `distance`, as `text-rotate` degrees.

    Measured across a window rather than between adjacent vertices: a contour
    from a noisy grid wiggles, and a bearing from one short segment makes the
    label wander while the line does not.

    Clockwise, because that is what MapLibre's `text-rotate` is, and against a
    screen whose y grows downward while the frame's grows up — hence the
    negated Δy. Normalised into (-90, 90] so the text never reads upside down
    and so a contour digitised in either direction labels the same way.
    """
    length = line.length
    behind = line.interpolate(max(distance - window, 0.0))
    ahead = line.interpolate(min(distance + window, length))

    dx = ahead.x - behind.x
    dy = ahead.y - behind.y
    if dx == 0.0 and dy == 0.0:
        return 0.0

    degrees = math.degrees(math.atan2(-dy, dx))
    # Half-turns leave the line's direction on screen unchanged, so this only
    # decides which way the reader tilts their head. The interval is half-open
    # at +90 so a vertical contour always reads bottom-to-top — the spine
    # convention — whichever direction it was digitised in.
    while degrees >= 90.0:
        degrees -= 180.0
    while degrees < -90.0:
        degrees += 180.0

    # Steeply-leaning-one-way and steeply-leaning-the-other are the same line to
    # a reader, so they get the same label: up it.
    if degrees >= VERTICAL_BAND_DEG:
        return -90.0
    return degrees


def _cut(line: LineString, anchors: list[float], gap: float, closed: bool) -> list[LineString]:
    """The line with `gap` removed around each anchor.

    A closed line is treated as the cycle it is: the kept pieces run from one
    gap to the next *the long way round*, so the piece spanning the ring's
    start vertex comes back whole. Cutting a ring as though it were an open
    line puts a second break at whatever coordinate the tracer happened to
    start at, which is a break nobody chose and which moves when the grid is
    re-contoured.
    """
    length = line.length
    half = gap / 2.0

    if closed:
        pieces = []
        for index, anchor in enumerate(anchors):
            following = anchors[index + 1] if index + 1 < len(anchors) else anchors[0] + length
            span = following - anchor - gap
            piece = _wrapped(line, anchor + half, span, length)
            if piece is not None:
                pieces.append(piece)
        return pieces or [line]

    pieces = []
    position = 0.0
    for anchor in anchors:
        piece = _substring(line, position, anchor - half)
        if piece is not None:
            pieces.append(piece)
        position = anchor + half
    tail = _substring(line, position, length)
    if tail is not None:
        pieces.append(tail)
    return pieces or [line]


def _wrapped(line: LineString, start: float, span: float, length: float) -> LineString | None:
    """A piece of a ring that may run past its start vertex and continue."""
    if span <= 0:
        return None
    start %= length
    if start + span <= length:
        return _substring(line, start, start + span)

    head = _substring(line, start, length)
    tail = _substring(line, 0.0, start + span - length)
    if head is None:
        return tail
    if tail is None:
        return head
    return LineString(list(head.coords) + list(tail.coords)[1:])


def _substring(line: LineString, start: float, end: float) -> LineString | None:
    """A piece of the line, or None when there is no piece to take."""
    if end - start <= 0:
        return None
    piece = substring(line, start, end)
    if piece.geom_type != "LineString" or len(piece.coords) < 2:
        return None
    return piece
