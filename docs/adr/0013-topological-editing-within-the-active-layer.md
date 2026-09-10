# 0013 — Topological editing, scoped to the active layer

## Status

Accepted — 2026-09-10

## Context

`09-editing.md` §1 deferred planar topology outright:

> **Deferred, deliberately:** Full planar topology (shared-boundary editing where moving a
> boundary updates both polygons automatically)

with a rationale worth restating because it was correct:

> A polygon editor that silently permits slivers and gaps is worse than no editor for coverage
> work — the geologist trusts the output and the errors surface downstream in area
> calculations. We ship validation that *detects* slivers and refuses to save them, rather than
> topology that prevents them.

That reasoning holds. What it does not settle is whether there is anything useful between "no
topology" and "a topological data model", and there is.

The map-editing specification proposed a middle path: a coordinate coincidence index over one
layer, consulted on vertex edits, behind a toggle that is off by default. It is not a topology
model. It stores no node/edge graph, maintains nothing between sessions, and knows nothing about
faces. It answers exactly one question — *which other vertices are at this coordinate right now*
— and propagates an edit to them.

The practical case is unambiguous. A geologist adjusting a lease boundary shared by two parcels
currently has to drag two vertices and get them to land on the same coordinate, using a snap
tolerance clamped to 4–20 pixels. They will miss, by inches. Validate will find the sliver later,
by which time the surrounding work has moved on.

## Decision

**Adopt coincidence-based topological editing, scoped to the active layer, off by default.**
`09` §7 specifies it. Four properties make this narrower than what §1 deferred, and all four are
load-bearing:

1. **Active layer only.** Cross-layer propagation would mean editing a layer the user did not
   make active, which breaks the single-editable-layer invariant in `09` §3.1. Cross-layer
   coincidence is created by Align (`09` §11.2) instead. If cross-layer propagation is ever
   wanted, it needs an explicit "editable set" concept, not a quiet relaxation.
2. **A much tighter tolerance — 0.01 ft by default, against 4–20 px for snapping.** Snap
   tolerance is a UI affordance; topological coincidence is a property of the data. Reusing the
   snap tolerance would make a vertex drag move an unrelated vertex twelve feet away, which
   users would report as the editor corrupting their layer.
3. **Off by default, per project.** A user who has not asked for propagation does not get it.
4. **Vertex operations only.** Feature move, rotate and scale are explicitly non-topological
   because the intent is ambiguous — does the neighbour stretch, or translate? Guessing produces
   a confident wrong answer, so the tooltips say the operation is non-topological instead.

**What stays deferred**, and is now recorded as such in `09` §1: cross-layer propagation, a true
topological data model, and automatic gap or sliver removal across a coverage.

## Consequences

**Validation does not relax.** It still detects slivers and still blocks a save that would
introduce one. Topological editing reduces how often they are created; it does not become the
reason to stop checking. If those two ever conflict — a propagation producing a geometry
validation rejects — validation wins and the operation is refused.

**Vertex add must propagate, and this is the failure mode to watch.** Inserting a vertex on a
shared edge in only one polygon creates no gap immediately; it guarantees one on the next drag.
It is the most common way tools implement this halfway, and the resulting slivers appear a week
later with no obvious cause. `09` §19 carries a test asserting the neighbour gained the vertex.

**Atomicity is already handled.** `Command.deltas` is a list (`09` §3.4), so one drag mutating N
features is one undo entry and one transaction with no structural change. The risk is a
vertex-drag handler that collects a single feature out of habit.

**Users will report the toggle as broken.** Data that has never been aligned has no coincidence
to preserve, so nothing propagates. `09` §7.7 requires this to be stated in the toggle's tooltip
and in the Align dialog. It is the single most likely misunderstanding in the subsystem.

**The coincidence index earns its keep twice** — it is also the gap detector for Validate
Topology (`09` §12.2), so the cost is shared.

**Trigger to reconsider the deferred half:** a coverage that spans layers and must stay
topologically consistent across them — a lease grid against a unit outline, say. That is when the
"editable set" concept becomes worth its complexity, and not before.
