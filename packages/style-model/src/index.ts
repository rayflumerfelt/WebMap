/**
 * @webmap/style-model — Symbology types and the compilers to MapLibre
 * Style JSON. `07-frontend.md` §1, `08-styling-palettes.md`.
 *
 * No React, no MapLibre GL. `compileSymbology` arrives in Phase 2 alongside
 * the shared test vectors that keep it in step with the Python compiler in
 * `webmap_core.style` — two implementations, one test suite.
 */
export type {
  Categorized,
  ClassificationMethod,
  ContinuousRaster,
  FieldRef,
  Graduated,
  LabelSymbol,
  LineSymbol,
  Palette,
  PointSymbol,
  PolygonSymbol,
  RuleBased,
  SingleSymbol,
  SymbolSpec,
  Symbology,
} from './symbology.js';
export { entryCount } from './symbology.js';
