"""Python style compilation. Phase 2 — `08-styling-palettes.md` §3.

The counterpart to `packages/style-model`. Two implementations of one
compiler, kept honest by the shared test vectors in
`packages/style-model/test-vectors/` — the frontend needs synchronous
compilation for live preview while dragging a ramp stop, and the backend
needs it without a JS runtime.

Style compilation is appearance, not geoprocessing: it operates on colours,
class breaks and palette stops, never on coordinates. That is the carve-out
in `adr/0004-geoprocessing-owns-geometry.md`, and it is why this lives here
rather than in `webmap_geo`.
"""
