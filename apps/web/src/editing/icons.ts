/**
 * The editor's map glyphs, rasterised in plain arithmetic. `09-editing.md`
 * §6.7, §11.6.
 *
 * Two things in the editor have to be **squares**: the vertex handles (§11.6)
 * and the vertex snap indicator (§6.7). MapLibre draws circles natively and
 * nothing else — a square, a triangle or an X has to arrive as an image. The
 * obvious route is a `<canvas>`, and the obvious route is wrong here for two
 * reasons: the render service runs headless, and a canvas is untestable in
 * jsdom, so every rule below would go unverified.
 *
 * So these are signed-distance fields evaluated per pixel into an RGBA buffer.
 * `map.addImage` takes exactly that shape, the maths is a dozen lines, and the
 * result is a pure function of its arguments — which means the glyph a
 * geologist sees on a snap is something a test can assert about.
 *
 * **Non-premultiplied RGBA**, which is what `addImage` expects for a raw data
 * image. Premultiplying would show as a dark fringe around every glyph.
 */

/** The four shapes §6.7 assigns to the four snap types. */
export type IconShape = 'square' | 'circle' | 'x' | 'triangle';

export type Rgb = readonly [number, number, number];

/** What `map.addImage` takes. `pixelRatio` is why the glyph is sharp on a
 *  high-DPI display without being twice the size on an ordinary one. */
export interface IconImage {
  width: number;
  height: number;
  data: Uint8Array;
  pixelRatio: number;
}

export interface IconOptions {
  /** The glyph box in CSS pixels, square. */
  size: number;
  /** Centre to boundary in CSS pixels. Smaller than `size / 2` so the stroke
   *  and its antialiasing have somewhere to land. */
  radius: number;
  stroke: Rgb;
  /** `null` leaves the interior transparent — the hollow indicator of §6.6. */
  fill: Rgb | null;
  strokeWidth: number;
  pixelRatio: number;
}

/**
 * Signed distance from a point to the shape's boundary, in device pixels.
 *
 * Negative inside for the three closed shapes. An X has no inside: its
 * distance is to the centreline, so the stroke coverage below draws the whole
 * glyph and its fill is never consulted.
 */
function distanceTo(shape: IconShape, x: number, y: number, radius: number): number {
  switch (shape) {
    case 'square':
      return Math.max(Math.abs(x), Math.abs(y)) - radius;
    case 'circle':
      return Math.hypot(x, y) - radius;
    case 'x': {
      // Two segments through the centre at ±45°, each of half-length `radius`.
      // The rotated coordinates are the perpendicular offsets from each.
      const along = (x + y) / Math.SQRT2;
      const across = (x - y) / Math.SQRT2;
      return Math.min(
        segmentDistance(across, along, radius),
        segmentDistance(along, across, radius),
      );
    }
    case 'triangle':
      return triangleDistance(x, y, radius);
  }
}

/** Distance to a centred segment: `across` from it, `along` it. */
function segmentDistance(across: number, along: number, half: number): number {
  const overshoot = Math.abs(along) - half;
  return overshoot <= 0 ? Math.abs(across) : Math.hypot(across, overshoot);
}

/**
 * Signed distance to an equilateral triangle pointing up.
 *
 * Inigo Quilez's formulation, with two adjustments. `y` is negated, because a
 * raster's y grows downward and a triangle that points down is a different
 * glyph. And its parameter is scaled by √3/2, so that `radius` means centre to
 * apex here as it means centre to boundary for the other three shapes — the
 * unscaled form takes half the side length, which would silently draw the
 * midpoint indicator 15% larger than the vertex one.
 */
function triangleDistance(x: number, y: number, radius: number): number {
  const k = Math.sqrt(3);
  const side = (radius * k) / 2;
  let px = Math.abs(x) - side;
  let py = -y + side / k;

  if (px + k * py > 0) {
    const rotatedX = (px - k * py) / 2;
    const rotatedY = (-k * px - py) / 2;
    px = rotatedX;
    py = rotatedY;
  }
  px -= Math.min(Math.max(px, -2 * side), 0);
  return -Math.hypot(px, py) * Math.sign(py);
}

/** Antialiasing: one pixel of falloff across the boundary. */
function coverage(distance: number): number {
  return Math.min(Math.max(0.5 - distance, 0), 1);
}

/**
 * One glyph as an RGBA buffer.
 *
 * The stroke is composited over the fill rather than replacing it, so a filled
 * glyph keeps a clean edge instead of showing the fill's own antialiasing
 * through the stroke's.
 */
export function rasterise(shape: IconShape, options: IconOptions): IconImage {
  const side = Math.round(options.size * options.pixelRatio);
  const radius = options.radius * options.pixelRatio;
  const halfStroke = (options.strokeWidth * options.pixelRatio) / 2;
  const data = new Uint8Array(side * side * 4);
  const centre = side / 2;

  for (let row = 0; row < side; row += 1) {
    for (let column = 0; column < side; column += 1) {
      // Pixel centres, not corners: sampling at the corner shifts the glyph
      // half a pixel and makes a 2 px stroke visibly lopsided.
      const x = column + 0.5 - centre;
      const y = row + 0.5 - centre;
      const distance = distanceTo(shape, x, y, radius);

      const strokeAlpha = coverage(Math.abs(distance) - halfStroke);
      const fillAlpha = options.fill && shape !== 'x' ? coverage(distance) : 0;

      const alpha = strokeAlpha + fillAlpha * (1 - strokeAlpha);
      if (alpha <= 0) continue;

      const offset = (row * side + column) * 4;
      for (let channel = 0; channel < 3; channel += 1) {
        const strokeChannel = options.stroke[channel] ?? 0;
        const fillChannel = options.fill?.[channel] ?? 0;
        const blended =
          (strokeChannel * strokeAlpha + fillChannel * fillAlpha * (1 - strokeAlpha)) / alpha;
        data[offset + channel] = Math.round(blended);
      }
      data[offset + 3] = Math.round(alpha * 255);
    }
  }

  return { width: side, height: side, data, pixelRatio: options.pixelRatio };
}

/** An image under the id a layer's `icon-image` refers to. */
export interface NamedIcon {
  id: string;
  image: IconImage;
}

/** Ink. Named rather than inlined because the handle and indicator colours are
 *  meant to be told apart at a glance and drifting them apart by accident is
 *  the easiest way to make the editor look broken. */
const OUTLINE: Rgb = [31, 41, 51];
const HANDLE_FILL: Rgb = [255, 255, 255];
const SELECTED_FILL: Rgb = [255, 140, 0];
const SNAP: Rgb = [0, 176, 194];

const PIXEL_RATIO = 2;

/** §11.6: handles are squares. Small — a handle large enough to be a
 *  comfortable target is large enough to hide the geometry under it, and the
 *  hit tolerance does that job instead. */
export const HANDLE_SIZE = 11;
/** The indicator is deliberately larger than a handle, so a snap is legible
 *  where handles are dense (§6.7). */
export const INDICATOR_SIZE = 15;

function handle(fill: Rgb): IconImage {
  return rasterise('square', {
    size: HANDLE_SIZE,
    radius: 3.5,
    stroke: OUTLINE,
    fill,
    strokeWidth: 1.5,
    pixelRatio: PIXEL_RATIO,
  });
}

function indicator(shape: IconShape, filled: boolean): IconImage {
  return rasterise(shape, {
    size: INDICATOR_SIZE,
    radius: 5,
    stroke: SNAP,
    fill: filled ? SNAP : null,
    strokeWidth: 2,
    pixelRatio: PIXEL_RATIO,
  });
}

/** The id an `icon-image` expression resolves to for a snap of this type. */
export function indicatorIconId(type: string, isExact: boolean): string {
  // An X has no interior, so hollow and filled cannot be told apart for an
  // intersection — and an intersection is computed from two edges rather than
  // looked up, so there is no tile-versus-exact distinction to show (§6.6).
  if (type === 'intersection') return 'edit-snap-intersection';
  return `edit-snap-${type}-${isExact ? 'exact' : 'tile'}`;
}

/**
 * Every image the editing layers refer to, added once on map load.
 *
 * Built eagerly: nine small buffers cost well under a millisecond, and a lazy
 * scheme would have to handle the frame where a layer's `icon-image` names an
 * image that does not exist yet — which MapLibre renders as nothing, silently.
 */
export const EDIT_ICONS: NamedIcon[] = [
  { id: 'edit-handle', image: handle(HANDLE_FILL) },
  { id: 'edit-handle-selected', image: handle(SELECTED_FILL) },
  { id: 'edit-snap-vertex-tile', image: indicator('square', false) },
  { id: 'edit-snap-vertex-exact', image: indicator('square', true) },
  { id: 'edit-snap-edge-tile', image: indicator('circle', false) },
  { id: 'edit-snap-edge-exact', image: indicator('circle', true) },
  { id: 'edit-snap-midpoint-tile', image: indicator('triangle', false) },
  { id: 'edit-snap-midpoint-exact', image: indicator('triangle', true) },
  { id: 'edit-snap-intersection', image: indicator('x', false) },
];
