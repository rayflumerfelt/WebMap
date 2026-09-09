/**
 * Scale bar arithmetic. `06-rendering.md` §9.
 *
 * **Computed from the actual projection at map centre, not from zoom level.**
 * That distinction is the whole content of this file. Web Mercator's scale
 * varies with latitude by 1/cos(φ), so a bar derived from zoom alone is
 * correct only at the equator and overstates distance by 18% in the Midland
 * Basin at 32°N — on a map whose entire purpose is measuring things.
 *
 * The failure is silent and plausible: the bar looks right, the numbers are
 * round, and a well spacing read off it is wrong by a fifth.
 *
 * Kept separate from the component so it can be tested as arithmetic, and so
 * the render service can compute a bar without mounting React.
 */

/** Earth's circumference at the equator, WGS84. */
const EQUATORIAL_CIRCUMFERENCE_M = 40_075_016.686;

/** MapLibre's zoom is defined against 512px tiles: 512 = 2^9. */
const TILE_SIZE_EXPONENT = 9;

const M_PER_FT = 0.3048;
const FT_PER_MILE = 5280;

/** Round distances a reader can hold in their head, per decade. */
const NICE = [1, 2, 3, 5] as const;

export type ScaleUnit = 'metric' | 'imperial';

export interface ScaleBarSpec {
  /** Rendered width in CSS pixels. Always ≤ the maximum asked for. */
  widthPx: number;
  /** e.g. "5 km", "2,000 ft". Pre-formatted, because the unit choice and the
   *  rounding are the same decision. */
  label: string;
  /** The distance the bar spans, in `unit`. Exposed for tests and for a
   *  caller that wants to format it differently. */
  distance: number;
  unit: 'm' | 'km' | 'ft' | 'mi';
}

/**
 * Ground resolution at a given latitude and MapLibre zoom.
 *
 * The `cos(latitude)` term is the part that gets left out. Without it this
 * function is a pure function of zoom, which is exactly the bug.
 */
export function metersPerPixel(latitude: number, zoom: number): number {
  const clamped = Math.max(-85.051129, Math.min(85.051129, latitude));
  const radians = (clamped * Math.PI) / 180;
  return (EQUATORIAL_CIRCUMFERENCE_M * Math.cos(radians)) / 2 ** (zoom + TILE_SIZE_EXPONENT);
}

/**
 * The largest round distance whose bar fits within `maxWidthPx`.
 *
 * Fits *within*, never overflows: a scale bar wider than the box it was given
 * is clipped by the map frame, and a clipped bar is a bar that lies about the
 * distance it spans.
 */
export function scaleBar(
  latitude: number,
  zoom: number,
  maxWidthPx: number,
  unit: ScaleUnit = 'metric',
): ScaleBarSpec {
  const mpp = metersPerPixel(latitude, zoom);
  const maxMeters = mpp * maxWidthPx;

  if (!(maxMeters > 0) || !Number.isFinite(maxMeters)) {
    // A zero-width container or a degenerate zoom. Returning a zero bar keeps
    // the overlay renderable; throwing would take the whole map down over a
    // decoration.
    return { widthPx: 0, label: '', distance: 0, unit: unit === 'metric' ? 'm' : 'ft' };
  }

  return unit === 'metric' ? metricBar(maxMeters, mpp) : imperialBar(maxMeters, mpp);
}

function metricBar(maxMeters: number, mpp: number): ScaleBarSpec {
  const meters = largestNiceAtMost(maxMeters);
  const useKm = meters >= 1000;
  return {
    widthPx: meters / mpp,
    distance: useKm ? meters / 1000 : meters,
    unit: useKm ? 'km' : 'm',
    label: `${format(useKm ? meters / 1000 : meters)} ${useKm ? 'km' : 'm'}`,
  };
}

function imperialBar(maxMeters: number, mpp: number): ScaleBarSpec {
  const maxFeet = maxMeters / M_PER_FT;

  // Miles only once a whole one fits. Below that, feet — a Permian well
  // spacing is quoted in feet and "0.19 mi" helps nobody.
  if (maxFeet >= FT_PER_MILE) {
    const miles = largestNiceAtMost(maxFeet / FT_PER_MILE);
    return {
      widthPx: (miles * FT_PER_MILE * M_PER_FT) / mpp,
      distance: miles,
      unit: 'mi',
      label: `${format(miles)} mi`,
    };
  }

  const feet = largestNiceAtMost(maxFeet);
  return {
    widthPx: (feet * M_PER_FT) / mpp,
    distance: feet,
    unit: 'ft',
    label: `${format(feet)} ft`,
  };
}

/**
 * The largest value of the form {1,2,3,5} × 10^n that is ≤ `limit`.
 *
 * Largest, so the bar uses the space it has: a bar occupying a quarter of its
 * allowance is harder to read off and looks like a rendering fault.
 */
function largestNiceAtMost(limit: number): number {
  const decade = 10 ** Math.floor(Math.log10(limit));
  let best = decade;
  for (const mantissa of NICE) {
    const candidate = mantissa * decade;
    if (candidate <= limit) best = candidate;
  }
  return best;
}

function format(value: number): string {
  // Thousands separators: "2,000 ft" is read at a glance and "2000 ft" is
  // counted. Locale-independent so a render and the SPA agree.
  const rounded = Number(value.toPrecision(3));
  return rounded.toLocaleString('en-US');
}
