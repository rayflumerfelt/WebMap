# 0014 — Terra Draw draws; it does not own selection, vertices, or the store

## Status

Accepted — 2026-09-10

## Context

`09-editing.md` §2 gave Terra Draw the whole interactive surface. Its `TerraDrawSelectMode` was
configured with draggable features, draggable and deletable coordinates, and midpoints on lines
and polygons — meaning Terra Draw owned selection, vertex editing, and by extension the feature
store those operate on.

That was a reasonable default when the editing spec described drawing, vertex dragging and
snapping. It does not survive the operations catalog in `09` §11, and three specific properties
of the library are why:

1. **Its select mode is single-feature.** Multi-select is an open upstream request
   (JamesLMilner/terra-draw#432) and is not implemented. `09` §9's Edit menu — Combine,
   Dissolve, Explode, Clip, Erase, Intersect, bulk Delete, bulk attribute edit — operates on a
   multi-feature selection set. Every one of those commands needs a selection Terra Draw cannot
   express.
2. **Its snapping targets only its own internal store.** `09` §6 snaps against arbitrary
   MapLibre vector layers, which is the entire point of snapping while digitising against an
   existing boundary in another layer. A snap engine that can only see features Terra Draw
   already holds cannot do that.
3. **Its store would be a fourth representation of geometry.** `09` §3.3 names three — tile,
   exact, and dirty — and says confusing them is the primary source of bugs in this subsystem.
   Adding a fourth, owned by a library, with its own lifecycle and its own idea of feature
   identity, makes the exact-coordinate protocol in §6.6 substantially harder to reason about.

None of these is a defect in Terra Draw. They are the boundary of what it was built for.

## Decision

**Terra Draw is used for new geometry creation only** — polygon, linestring, point, rectangle
and freehand modes. It does not own selection state, vertex editing of existing features, or the
feature store.

Our snap engine feeds it through `snapping.toCustom`, which takes a function returning a position
synchronously, so drawn geometry snaps by the same rules as everything else.

**The exit condition is part of the decision.** If implementation finds it needs custom Terra
Draw modes for selection, splitting, or vertex editing, **drop Terra Draw entirely**. At that
point the library is being reimplemented through an adapter, and the adapter is providing
negative value — a layer of indirection around code we wrote anyway.

## Consequences

**More code than the previous design.** Selection, hit testing, vertex handles, rubber-band
selection and drag handling are ours now. That is roughly phases 2 and 5 of `09` §20, and it is
the price of the multi-feature Edit menu — which is not optional, because Combine, Dissolve and
Explode are the standard triad users arrive expecting.

**Vertex handles are ours to render**, from a dedicated GeoJSON source with a distinct colour for
selected vertices. This turns out to be an advantage rather than a cost: `09` §3.2's vertex
selection scope and `VertexRef` addressing (ring, index, part) have no equivalent in Terra Draw's
model, and the exact-coordinate resolution protocol in §6.6 needs vertex identity that survives
tile simplification.

**Terra Draw's dependency footprint stays small and its upgrade risk stays low.** We use one
documented hook and the creation modes, which are the stable, well-covered part of its surface.

**If the exit condition fires, the cost is bounded.** Creation modes are the smallest part of the
subsystem, and by the time they would be rewritten, selection, vertex editing and snapping — the
hard parts — already exist and are ours.

**`09` §2's code example is superseded.** The `TerraDrawSelectMode` configuration with draggable
and deletable coordinates does not describe what gets built.
