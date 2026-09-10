/**
 * The editor's map glyphs. `09-editing.md` §6.7, §11.6.
 *
 * These are asserted rather than eyeballed because the failure mode is quiet:
 * a glyph rasterised wrong is still a glyph, and "the snap indicator looks
 * slightly off" is not a bug anyone files.
 */

import { describe, expect, it } from 'vitest';

import { EDIT_ICONS, indicatorIconId, rasterise } from './icons.js';
import type { IconImage, IconShape } from './icons.js';

const BLACK = [0, 0, 0] as const;
const WHITE = [255, 255, 255] as const;

function draw(shape: IconShape, fill: readonly [number, number, number] | null): IconImage {
  return rasterise(shape, {
    size: 16,
    radius: 5,
    stroke: BLACK,
    fill,
    strokeWidth: 2,
    pixelRatio: 1,
  });
}

/** Alpha at a point given in glyph coordinates from the centre. */
function alphaAt(image: IconImage, dx: number, dy: number): number {
  const centre = image.width / 2;
  const column = Math.floor(centre + dx);
  const row = Math.floor(centre + dy);
  return image.data[(row * image.width + column) * 4 + 3]!;
}

describe('rasterise', () => {
  it('sizes the buffer by the pixel ratio', () => {
    // A glyph authored at 11 CSS px must arrive at 22 device px with
    // pixelRatio 2, or MapLibre draws it at twice the intended size.
    const image = rasterise('square', {
      size: 11,
      radius: 3.5,
      stroke: BLACK,
      fill: WHITE,
      strokeWidth: 1.5,
      pixelRatio: 2,
    });

    expect(image.width).toBe(22);
    expect(image.height).toBe(22);
    expect(image.data).toHaveLength(22 * 22 * 4);
  });

  describe('a square', () => {
    it('is hollow in the middle with no fill', () => {
      // The hollow indicator of §6.6 — tile-derived, not yet resolved.
      expect(alphaAt(draw('square', null), 0, 0)).toBe(0);
    });

    it('is opaque in the middle when filled', () => {
      expect(alphaAt(draw('square', WHITE), 0, 0)).toBe(255);
    });

    it('is drawn on its boundary either way', () => {
      expect(alphaAt(draw('square', null), 0, -5)).toBe(255);
      expect(alphaAt(draw('square', null), 5, 0)).toBe(255);
    });

    it('reaches its corners, which is what makes it a square', () => {
      // The distinction from the edge indicator is the whole point: a circle
      // and a square that both look round are one glyph, not two.
      expect(alphaAt(draw('square', null), 5, 5)).toBeGreaterThan(0);
    });

    it('stops outside its radius', () => {
      expect(alphaAt(draw('square', WHITE), 7, 7)).toBe(0);
    });
  });

  describe('a circle', () => {
    it('is drawn on its boundary', () => {
      expect(alphaAt(draw('circle', null), 0, -5)).toBe(255);
    });

    it('leaves its corners empty, unlike the square', () => {
      expect(alphaAt(draw('circle', null), 5, 5)).toBe(0);
    });
  });

  describe('an X', () => {
    it('is inked at the centre where the strokes cross', () => {
      expect(alphaAt(draw('x', null), 0, 0)).toBe(255);
    });

    it('is inked along a diagonal and empty off it', () => {
      expect(alphaAt(draw('x', null), 3, 3)).toBe(255);
      expect(alphaAt(draw('x', null), 0, 4)).toBe(0);
    });

    it('ignores fill, having no interior to fill', () => {
      expect(draw('x', WHITE).data).toEqual(draw('x', null).data);
    });
  });

  describe('a triangle', () => {
    it('points up', () => {
      // Above the apex is empty; below the base is not — the asymmetry is the
      // glyph. A y-axis sign error gives a triangle that points down and
      // still looks deliberate.
      const image = draw('triangle', WHITE);

      expect(alphaAt(image, 0, -7)).toBe(0);
      expect(alphaAt(image, 0, 2)).toBeGreaterThan(0);
    });

    it('is empty in the middle when hollow and solid when filled', () => {
      expect(alphaAt(draw('triangle', null), 0, 0)).toBe(0);
      expect(alphaAt(draw('triangle', WHITE), 0, 0)).toBe(255);
    });
  });

  it('leaves colour unpremultiplied, as addImage expects', () => {
    // Premultiplied data shows as a dark fringe around every glyph. A white
    // fill must read 255 in all three channels wherever it is opaque.
    const image = draw('square', WHITE);
    const centre = image.width / 2;
    const offset = (centre * image.width + centre) * 4;

    expect([image.data[offset], image.data[offset + 1], image.data[offset + 2]]).toEqual([
      255, 255, 255,
    ]);
  });
});

describe('indicatorIconId', () => {
  it('names the hollow glyph while the snap is tile-derived', () => {
    expect(indicatorIconId('vertex', false)).toBe('edit-snap-vertex-tile');
    expect(indicatorIconId('vertex', true)).toBe('edit-snap-vertex-exact');
  });

  it('gives an intersection one glyph either way', () => {
    // An X has no interior to fill, and an intersection is computed rather
    // than looked up, so there is no tile-versus-exact distinction to draw.
    expect(indicatorIconId('intersection', false)).toBe(indicatorIconId('intersection', true));
  });

  it('names an image that exists for every snap type', () => {
    // A missing image renders as nothing, silently — the failure that would
    // otherwise reach a user as "snapping stopped showing".
    const ids = new Set(EDIT_ICONS.map((icon) => icon.id));

    for (const type of ['vertex', 'edge', 'midpoint', 'intersection']) {
      for (const exact of [true, false]) {
        expect(ids).toContain(indicatorIconId(type, exact));
      }
    }
  });
});

describe('EDIT_ICONS', () => {
  it('has unique ids', () => {
    const ids = EDIT_ICONS.map((icon) => icon.id);

    expect(new Set(ids).size).toBe(ids.length);
  });

  it('tells a selected handle from an unselected one', () => {
    // §11.6 asks for selected vertices in a distinct colour, and two handle
    // images that happened to be identical would silently drop that.
    const plain = EDIT_ICONS.find((icon) => icon.id === 'edit-handle')!;
    const selected = EDIT_ICONS.find((icon) => icon.id === 'edit-handle-selected')!;

    expect(plain.image.data).not.toEqual(selected.image.data);
  });
});
