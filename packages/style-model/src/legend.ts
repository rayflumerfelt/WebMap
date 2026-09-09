/**
 * Legend derivation. `08-styling-palettes.md` §8.
 *
 * **The legend derives from the symbology model, not from the compiled
 * style.** The model knows class labels, units and the classification method;
 * the compiled style knows only numbers. Deriving from the style would mean
 * reverse-engineering a `step` expression back into classes, and would lose
 * the unit — so a legend would read "12 – 18" where it should read
 * "12 – 18 %".
 *
 * It lives in `style-model` rather than in `@webmap/ui` because it is data
 * transformation with no React in it, which is what lets the render service
 * and the SPA share one answer. `@webmap/ui` draws the `LegendSpec`; this
 * decides what is in it.
 */

import { sampleRamp } from './palette.js';
import type { Palette, SymbolSpec, Symbology } from './symbology.js';
import { entryCount } from './symbology.js';

export interface LayerMetadata {
  name: string;
  valueRange?: { min: number; max: number; unit?: string };
}

export interface LegendEntry {
  swatch: string;
  label: string;
}

export interface ClassesLegend {
  kind: 'classes';
  title: string;
  entries: LegendEntry[];
}

export interface ColorbarLegend {
  kind: 'colorbar';
  title: string;
  unit?: string;
  range: [number, number];
  palette: Palette;
  ticks: number[];
}

export type LegendSpec = ClassesLegend | ColorbarLegend;

export function deriveLegend(
  symbology: Symbology,
  meta: LayerMetadata,
  palettes: Record<string, Palette>,
): LegendSpec {
  switch (symbology.type) {
    case 'single':
      return {
        kind: 'classes',
        title: meta.name,
        entries: [{ swatch: swatchOf(symbology.symbol), label: meta.name }],
      };

    case 'categorized': {
      const entries: LegendEntry[] = symbology.categories.map((category) => ({
        swatch: swatchOf(category.symbol),
        label: category.label,
      }));
      if (symbology.other) {
        // Labelled rather than left implicit: a reader seeing an unlabelled
        // colour on the map has no way to learn it means "everything else".
        entries.push({ swatch: swatchOf(symbology.other), label: 'Other' });
      }
      return { kind: 'classes', title: titleWithUnit(meta), entries };
    }

    case 'graduated': {
      const palette = requirePalette(palettes, symbology.paletteId);
      // **Iterate classCount, never breaks.** `classify()` returns n-1 interior
      // breaks for n classes and the compiler emits n colours; mapping over
      // `breaks` here is the specific bug §8 names, and it renders a 5-class
      // map with a 4-entry legend.
      const swatches = sampleRamp(palette, symbology.classCount);
      return {
        kind: 'classes',
        title: titleWithUnit(meta),
        entries: swatches.map((swatch, i) => ({
          swatch,
          label: classLabel(symbology.breaks, i, symbology.classCount, meta.valueRange?.unit),
        })),
      };
    }

    case 'rules':
      return {
        kind: 'classes',
        title: meta.name,
        entries: symbology.rules.map((rule) => ({
          swatch: swatchOf(rule.symbol),
          label: rule.label,
        })),
      };

    case 'continuous_raster': {
      const palette = requirePalette(palettes, symbology.paletteId);
      const range = symbology.range ?? rangeFromMeta(meta);
      return {
        kind: 'colorbar',
        title: meta.name,
        ...(meta.valueRange?.unit ? { unit: meta.valueRange.unit } : {}),
        range,
        palette,
        ticks: prettyTicks(range[0], range[1]),
      };
    }
  }
}

/**
 * How many entries a legend will have, without building one.
 *
 * Delegates to `entryCount` so the legend and the compiler cannot disagree
 * about the count — they read the same function, rather than two functions
 * that happen to return the same number today.
 */
export function legendEntryCount(symbology: Symbology): number {
  return entryCount(symbology);
}

// --- labels -----------------------------------------------------------------

/**
 * The label for class `i` of `count`, given `count - 1` interior breaks.
 *
 * The first and last classes are open-ended, because they are: a feature above
 * the top break has no upper bound in the classification. Writing the data
 * maximum there instead would claim a precision the classification does not
 * have, and would be wrong the moment a new well is added.
 */
export function classLabel(
  breaks: number[],
  index: number,
  count: number,
  unit?: string,
): string {
  const suffix = unit ? ` ${unit}` : '';
  if (index === 0) return `< ${formatBreak(breaks[0]!)}${suffix}`;
  if (index === count - 1) return `≥ ${formatBreak(breaks[breaks.length - 1]!)}${suffix}`;
  return `${formatBreak(breaks[index - 1]!)} – ${formatBreak(breaks[index]!)}${suffix}`;
}

/**
 * Trims floating-point dust without inventing precision.
 *
 * A break of `12.300000000000001` reads as `12.3`; one of `12.34567` keeps its
 * digits, because rounding a break to fit a legend would make the legend
 * disagree with the map.
 */
function formatBreak(value: number): string {
  const rounded = Number(value.toPrecision(12));
  return String(rounded);
}

function titleWithUnit(meta: LayerMetadata): string {
  const unit = meta.valueRange?.unit;
  return unit ? `${meta.name} (${unit})` : meta.name;
}

// --- colour bar -------------------------------------------------------------

/**
 * Tick values at round numbers inside a range, for a colour bar.
 *
 * Round numbers, not even fractions of the range: a bar running 8,237 to
 * 9,614 ft gets ticks at 8,500 / 9,000 / 9,500, which a reader can locate on
 * a map. Even fifths would give 8,512.4, which nobody reads off a legend.
 *
 * Deliberately *not* `classify(..., 'pretty', n)`: that lives in Python
 * because it needs the values, and a colour bar needs only the range. The
 * shared thing is the nice-number family, which is small enough that two
 * copies is cheaper than a dependency in the wrong direction.
 */
export function prettyTicks(low: number, high: number, target = 5): number[] {
  if (!(high > low)) return [];
  const nice = [1, 2, 2.5, 5, 10];
  const ideal = (high - low) / target;
  const exponent = Math.floor(Math.log10(ideal));
  const step =
    [exponent, exponent + 1]
      .flatMap((e) => nice.map((m) => m * 10 ** e))
      .sort((a, b) => a - b)
      .find((candidate) => candidate >= ideal) ?? ideal;

  const ticks: number[] = [];
  const decimals = Math.max(0, -Math.floor(Math.log10(step)) + 2);
  for (let i = Math.ceil(low / step); i * step <= high; i += 1) {
    ticks.push(Number((i * step).toFixed(decimals)));
  }
  return ticks;
}

// --- helpers ----------------------------------------------------------------

/** The one colour that stands for a symbol in a legend swatch. */
function swatchOf(symbol: SymbolSpec): string {
  switch (symbol.geometry) {
    case 'point':
    case 'line':
    case 'label':
      return symbol.color;
    case 'polygon':
      return symbol.fillColor;
  }
}

function requirePalette(palettes: Record<string, Palette>, id: string): Palette {
  const palette = palettes[id];
  if (!palette) {
    throw new Error(
      `Legend derivation references palette '${id}', which was not supplied. ` +
        `Pass the same palettes used to compile the layer — a legend built from ` +
        `a different palette would disagree with the map it describes.`,
    );
  }
  return palette;
}

function rangeFromMeta(meta: LayerMetadata): [number, number] {
  if (!meta.valueRange) {
    throw new Error(
      `A continuous raster legend needs a value range: the symbology does not ` +
        `pin one, and the layer metadata carries no min/max. Set ` +
        `ContinuousRaster.range, or populate the dataset's value range on ingest.`,
    );
  }
  return [meta.valueRange.min, meta.valueRange.max];
}
