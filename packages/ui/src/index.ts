/**
 * @webmap/ui — theme, legend, scale bar, ramp editor, property editor.
 *
 * **Imports no MapLibre GL.** These components render in the headless render
 * shell, which has no map instance in scope for overlays — that is what makes
 * one legend implementation identical in the SPA and in a render
 * (`06-rendering.md` §9). `@maplibre/maplibre-gl-style-spec` is permitted: it
 * is the specification as data, and the schema-driven property editor is
 * generated from it (`08-styling-palettes.md` §6).
 *
 * The ramp editor and the schema-driven property editor land in Phase 5.
 */
export { webmapTheme } from './theme.js';
export { Legend } from './Legend/Legend.js';
export type { LegendProps } from './Legend/Legend.js';
export { ScaleBar } from './ScaleBar/ScaleBar.js';
export type { ScaleBarProps } from './ScaleBar/ScaleBar.js';
export { metersPerPixel, scaleBar } from './ScaleBar/scale.js';
export type { ScaleBarSpec, ScaleUnit } from './ScaleBar/scale.js';
export { NorthArrow } from './NorthArrow/NorthArrow.js';
export type { NorthArrowProps } from './NorthArrow/NorthArrow.js';
