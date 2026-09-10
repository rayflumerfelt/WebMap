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
 * The §6.3 shared controls live under `controls/`. Each takes a value and an
 * `onChange` and knows nothing about layers, maps or this application, so a
 * formatting dialog composes them rather than reimplementing them and they can
 * be lifted into another application unchanged.
 *
 * The schema-driven property editor — `08` §6's escape hatch, generated from the
 * MapLibre style spec — is the one piece of Phase 5's UI still owed here.
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
export { LayerTree } from './LayerTree/LayerTree.js';
export type { LayerTreeProps, SessionLayerView } from './LayerTree/LayerTree.js';

// --- `07-frontend.md` §6.3 shared controls ---------------------------------
export { ColorPicker, expandHex } from './controls/ColorPicker.js';
export type { ColorPickerProps } from './controls/ColorPicker.js';
export { RampEditor } from './controls/RampEditor.js';
export type { RampEditorProps } from './controls/RampEditor.js';
export { IntervalEditor } from './controls/IntervalEditor.js';
export type { IntervalBand, IntervalEditorProps } from './controls/IntervalEditor.js';
export { CategoryTable, OTHER_DEFAULT } from './controls/CategoryTable.js';
export type { CategoryRow, CategoryTableProps } from './controls/CategoryTable.js';
export { DASH_PATTERNS, LinePicker, patternName } from './controls/LinePicker.js';
export type { LinePickerProps, LineValue } from './controls/LinePicker.js';
export { MarkerPicker } from './controls/MarkerPicker.js';
export type { MarkerPickerProps, MarkerShape, MarkerValue } from './controls/MarkerPicker.js';
export { composeStack, FontPicker, splitStack } from './controls/FontPicker.js';
export type { FontFamily, FontPickerProps } from './controls/FontPicker.js';
export { formatOf, PaletteIO } from './controls/PaletteIO.js';
export type { PaletteFormat, PaletteIOProps } from './controls/PaletteIO.js';
export { LegendPreview } from './controls/LegendPreview.js';
export type { LegendPreviewProps } from './controls/LegendPreview.js';
