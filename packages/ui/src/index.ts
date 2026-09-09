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
 * Components land in Phase 2 (legend, scale bar) and Phase 5 (ramp editor,
 * property editor).
 */
export { webmapTheme } from './theme.js';
