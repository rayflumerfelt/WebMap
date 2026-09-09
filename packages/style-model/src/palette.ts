/**
 * Palette sampling. `08-styling-palettes.md` §5.
 *
 * **This file and `webmap_core/style/palette.py` must agree exactly.** They
 * are the lowest layer of the two-implementation compiler in §3.1, so a
 * one-bit difference here shows up as every graduated layer differing between
 * the interactive map and a render — which is precisely the drift the shared
 * test vectors exist to catch.
 *
 * Everything below is specified rather than idiomatic, because "idiomatic in
 * TypeScript" and "idiomatic in Python" round differently.
 */

import type { Palette } from './symbology.js';

/** `#rrggbb`, lowercase. One spelling, so string comparison is meaningful. */
export type Hex = string;

interface Rgb {
  r: number;
  g: number;
  b: number;
}

const HEX_PATTERN = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;

export function parseHex(value: string): Rgb {
  const match = HEX_PATTERN.exec(value.trim());
  if (!match) {
    throw new Error(
      `'${value}' is not a hex colour. Palette stops are #rgb or #rrggbb; ` +
        `named colours and rgb() are not accepted because the Python compiler ` +
        `would have to reproduce a colour table to match.`,
    );
  }
  const digits = match[1]!;
  const full =
    digits.length === 3
      ? digits
          .split('')
          .map((c) => c + c)
          .join('')
      : digits;
  return {
    r: parseInt(full.slice(0, 2), 16),
    g: parseInt(full.slice(2, 4), 16),
    b: parseInt(full.slice(4, 6), 16),
  };
}

/**
 * **Rounds half away from zero, not half to even.**
 *
 * Ties are not rare: sampling any ramp at a class-boundary midpoint hits one
 * whenever two stop channels differ by an odd number, which happened in two of
 * the five classes the first time this was run. Python's built-in `round` is
 * half-to-even, so leaving the rule unstated would have made the two compilers
 * disagree by one bit on exactly those samples. `webmap_core/style/palette.py`
 * spells it `math.floor(v + 0.5)`, which is what `Math.round` is defined as for
 * the non-negative, finite values a colour channel can hold.
 */
export function toHex({ r, g, b }: Rgb): Hex {
  const part = (v: number) => Math.round(clamp(v, 0, 255)).toString(16).padStart(2, '0');
  return `#${part(r)}${part(g)}${part(b)}`;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/**
 * The colour at `position` (0..1) along a palette.
 *
 * Interpolates in sRGB, not a perceptual space. That is the wrong choice for
 * *designing* a ramp and the right one here: the ramps geologists import from
 * Surfer and GMT are defined as sRGB stops, and interpolating them anywhere
 * else would render them differently from the tool they came from.
 *
 * `discrete` palettes take the colour of the stop at or below the position —
 * a class boundary is a step, and blurring it would make the legend a lie.
 */
export function colourAt(palette: Palette, position: number): Hex {
  const stops = sortedStops(palette);
  const target = clamp(position, 0, 1);

  if (stops.length === 0) {
    throw new Error(`Palette '${palette.id}' has no stops.`);
  }
  if (stops.length === 1) return toHex(parseHex(stops[0]!.color));

  if (palette.interpolation === 'discrete') {
    let chosen = stops[0]!;
    for (const stop of stops) {
      if (stop.position <= target) chosen = stop;
    }
    return toHex(parseHex(chosen.color));
  }

  // A stop sitting exactly on the requested position wins outright, and the
  // *last* such stop wins. That is what makes a coincident pair read as a hard
  // break in an otherwise continuous ramp: values at and above the break take
  // the upper colour, matching the `discrete` rule above rather than
  // contradicting it. It also means the interpolation below always has a
  // strictly positive span, so there is no divide-by-zero case to handle.
  for (let i = stops.length - 1; i >= 0; i -= 1) {
    if (stops[i]!.position === target) return toHex(parseHex(stops[i]!.color));
  }

  const upperIndex = stops.findIndex((s) => s.position > target);
  if (upperIndex <= 0) {
    // Outside the stop range entirely — clamped to an end.
    return toHex(parseHex(stops[upperIndex === 0 ? 0 : stops.length - 1]!.color));
  }

  const lower = stops[upperIndex - 1]!;
  const upper = stops[upperIndex]!;
  const t = (target - lower.position) / (upper.position - lower.position);

  const a = parseHex(lower.color);
  const b = parseHex(upper.color);
  return toHex({
    r: a.r + (b.r - a.r) * t,
    g: a.g + (b.g - a.g) * t,
    b: a.b + (b.b - a.b) * t,
  });
}

/**
 * `count` colours spanning the palette, for a graduated or categorized layer.
 *
 * Positions are `i / (count - 1)`, so the first and last classes get the ends
 * of the ramp. A single class takes the midpoint: the ends of a diverging ramp
 * are its extremes, and one class coloured "extreme low" would be misleading.
 */
export function sampleRamp(palette: Palette, count: number): Hex[] {
  if (!Number.isInteger(count) || count < 1) {
    throw new Error(`Class count must be a positive integer; got ${count}.`);
  }
  if (count === 1) return [colourAt(palette, 0.5)];
  return Array.from({ length: count }, (_, i) => colourAt(palette, i / (count - 1)));
}

function sortedStops(palette: Palette): Palette['stops'] {
  // Sorted by position, ties broken by original order so a palette with
  // coincident stops compiles the same way in both languages.
  return palette.stops
    .map((stop, index) => ({ stop, index }))
    .sort((a, b) => a.stop.position - b.stop.position || a.index - b.index)
    .map(({ stop }) => stop);
}
