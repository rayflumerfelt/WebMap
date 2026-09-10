# 0015 — A contour label gaps its line, and the gap is cut server-side

## Status
Accepted — 2026-09-10

## Context

`08-styling-palettes.md` says of line labels: *"For lines,
`symbol-placement: 'line-center'` rather than a precomputed point. MapLibre anchors a
`point`-placed line label at the line's **first vertex** — an end, not the middle — which is why
contour labels drawn that way all cluster at the edge of the map."*

That reasoning is sound and the conclusion is incomplete. `line-center` draws the label **on top
of the contour**, and the line runs through the digits. Every contouring tool a geologist has
used — Surfer, Petra, ArcGIS — breaks the line for a character or two either side of the label
instead. It is not decoration: a contour that runs through its own value is harder to read at
exactly the moment somebody is reading values off the map.

The obvious alternative is a text halo, and it is wrong here. A halo is a fill of one colour,
and a structure map's contours sit over grid colour, over filled bands, over a raster. The halo
either hides what it covers or does not match it, and which of the two happens depends on the
symbology the user picked afterwards.

MapLibre cannot break a line under a label. Nothing in the style spec expresses it, and nothing
in the renderer computes it.

## Decision

**Contour geometry is written with the gaps already in it**, and the labels are written beside
it as point features carrying the bearing to draw them at.

A contour dataset therefore holds two kinds of feature, distinguished by a `kind` property:

- `kind = 'contour'` — the line, split into pieces with a gap where each label goes.
- `kind = 'label'` — a point at the centre of each gap, with `value` and `bearing`.

The label layer is `symbol-placement: 'point'` over the label features. `08`'s objection does
not apply, because MapLibre is no longer choosing the point: the anchor is a geometry we
computed, and it is at the middle of the gap by construction.

**The gap is sized in ground units from an explicit reference scale.** A label is a fixed number
of screen pixels wide and a gap is a fixed number of feet, so the two can only agree at one
scale. The caller names that scale — `webmap_geo` never infers one — and the contour service
defaults it to **the whole grid across a map pane**, taken as 1,000 px.

Half the grid's cell size per pixel was the first default and it is badly wrong, which is worth
recording because it is the sort of number that looks principled. On the seed grid — 4,600 ft
cells — it puts the entire surface in about 200 pixels and asks for a gap 144,000 ft long,
wider than the spacing between labels; every contour came back unlabelled. A gap is only
meaningful at the scale somebody actually looks at the map, and that is the extent, not the
cell.

## Consequences

**The gap is only correct near the reference scale.** Zoom well in and it reads as a wide break;
zoom well out and the text overflows it. This is inherent to cutting geometry rather than
drawing a mask, and it is the cost of a label that never covers what is under it. The mitigation
that exists today is the label layer's zoom window (`08` §6), which keeps the label near the
scale its gap was cut for. Regenerating contours at a second reference scale is a second
dataset, and a legitimate thing to want.

**Labels are never packed tighter than three gaps.** A spacing narrower than the gap leaves a
contour that is more break than line, so `label_contour` refuses one outright and the service
widens its derived default rather than refusing — the caller did not choose that number.

**A contour too short to hold a label is left whole.** Cutting a gap out of a short contour can
leave two stubs, or nothing. Below the threshold the line keeps its geometry and gets no label,
which is what every one of those tools does too.

**A closed contour stays closed where it can.** Splitting a ring at one gap yields one open line
whose ends are the gap, not two pieces with a seam at the ring's arbitrary start vertex.

**The style compilers do not know about this yet.** `packages/style-model` and
`webmap_core.style` still emit `line-center` for a labelled line layer, so today the gapped
geometry is drawn correctly and the label layer has to be written by hand — which the visual
fixture does. Teaching both compilers to emit a point-placed, bearing-rotated label layer for a
source that carries `kind = 'label'` is the next step, and it is a shared-vector change because
the two compilers must agree.

**`is_index` still decides what gets labelled.** Only index contours are labelled and therefore
only index contours are cut; the intermediates are untouched, which is what `05-geoprocessing.md`
§7 already says.
