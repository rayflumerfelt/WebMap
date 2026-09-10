/**
 * Vertex operations. `09-editing.md` §11.6.
 *
 * Three rules carry these tests: a closed ring's first and last coordinates
 * move together, Z survives an edit, and a ring has a minimum below which the
 * delete is refused rather than cascading.
 */

import { describe, expect, it } from 'vitest';

import {
  insertVertex,
  minimumRingSize,
  removeVertex,
  setVertex,
} from './geometryEdits.js';
import type { Geometry } from 'geojson';

const SQUARE: Geometry = {
  type: 'Polygon',
  coordinates: [
    [
      [0, 0],
      [10, 0],
      [10, 10],
      [0, 10],
      [0, 0],
    ],
  ],
};

const LINE: Geometry = {
  type: 'LineString',
  coordinates: [
    [0, 0],
    [10, 0],
    [20, 0],
  ],
};

/** A digitised breakline: two dimensions to edit in, three to interpolate on. */
const BREAKLINE: Geometry = {
  type: 'LineString',
  coordinates: [
    [0, 0, -8000],
    [10, 0, -8100],
  ],
};

describe('setVertex', () => {
  it('moves the vertex it was given', () => {
    const moved = setVertex(LINE, 0, 1, [5, 5]);

    expect(moved).toEqual({
      type: 'LineString',
      coordinates: [
        [0, 0],
        [5, 5],
        [20, 0],
      ],
    });
  });

  it('moves a closed ring’s closing coordinate with vertex 0', () => {
    // They are one vertex drawn twice. Move one without the other and the
    // polygon opens — which no renderer complains about and every area
    // calculation is then quietly wrong.
    const moved = setVertex(SQUARE, 0, 0, [-1, -1]) as { coordinates: number[][][] };

    expect(moved.coordinates[0]![0]).toEqual([-1, -1]);
    expect(moved.coordinates[0]!.at(-1)).toEqual([-1, -1]);
  });

  it('keeps the Z of the vertex it moved', () => {
    // The editor addresses x and y; the interpolation worker reads the third
    // ordinate. Dropping it here would flatten a breakline on the first drag.
    const moved = setVertex(BREAKLINE, 0, 0, [1, 1]) as { coordinates: number[][] };

    expect(moved.coordinates[0]).toEqual([1, 1, -8000]);
  });

  it('leaves the original geometry untouched', () => {
    // Copy-on-write: the undo stack holds the `before`, and mutating in place
    // would make undo restore the state it was undoing.
    const before = JSON.stringify(SQUARE);
    setVertex(SQUARE, 0, 2, [99, 99]);

    expect(JSON.stringify(SQUARE)).toBe(before);
  });

  it('addresses a multipolygon through the flat ring index', () => {
    // The same addressing the snap engine and the coincidence index use. A
    // part index would have to be threaded through both.
    const multi: Geometry = {
      type: 'MultiPolygon',
      coordinates: [
        [
          [
            [0, 0],
            [1, 0],
            [1, 1],
            [0, 0],
          ],
        ],
        [
          [
            [5, 5],
            [6, 5],
            [6, 6],
            [5, 5],
          ],
        ],
      ],
    };

    const moved = setVertex(multi, 1, 1, [9, 9]) as { coordinates: number[][][][] };

    expect(moved.coordinates[1]![0]![1]).toEqual([9, 9]);
    expect(moved.coordinates[0]![0]![1]).toEqual([1, 0]);
    expect(moved.coordinates).toHaveLength(2);
  });

  it('refuses the closing coordinate, which is not separately addressable', () => {
    expect(() => setVertex(SQUARE, 0, 4, [1, 1])).toThrow(/moves with vertex 0/);
  });

  it('names the ring count when the ring does not exist', () => {
    expect(() => setVertex(SQUARE, 3, 0, [1, 1])).toThrow(/this geometry has 1/);
  });
});

describe('insertVertex', () => {
  it('splices into the segment it was given', () => {
    const added = insertVertex(LINE, 0, 0, [5, 0]) as { coordinates: number[][] };

    expect(added.coordinates).toEqual([
      [0, 0],
      [5, 0],
      [10, 0],
      [20, 0],
    ]);
  });

  it('interpolates Z along the segment', () => {
    // The elevation the segment already implied at that point, so adding a
    // vertex to a breakline changes the handles and not the surface.
    const added = insertVertex(BREAKLINE, 0, 0, [2.5, 0]) as { coordinates: number[][] };

    expect(added.coordinates[1]).toEqual([2.5, 0, -8025]);
  });

  it('invents no Z when the segment has none', () => {
    const added = insertVertex(LINE, 0, 0, [5, 0]) as { coordinates: number[][] };

    expect(added.coordinates[1]).toHaveLength(2);
  });

  it('clamps to the segment for a point that projected past its end', () => {
    // The perpendicular foot is clamped in the snap engine; a rounding
    // difference here must not extrapolate the elevation beyond the segment.
    const added = insertVertex(BREAKLINE, 0, 0, [20, 0]) as { coordinates: number[][] };

    expect(added.coordinates[1]![2]).toBe(-8100);
  });

  it('adds into a closed ring without disturbing the closure', () => {
    const added = insertVertex(SQUARE, 0, 3, [0, 5]) as { coordinates: number[][][] };

    expect(added.coordinates[0]).toHaveLength(6);
    expect(added.coordinates[0]!.at(-1)).toEqual(added.coordinates[0]![0]);
  });

  it('names the segment count when there is no such segment', () => {
    expect(() => insertVertex(LINE, 0, 5, [1, 1])).toThrow(/has 2 segments/);
  });
});

describe('removeVertex', () => {
  it('removes the vertex it was given', () => {
    const removed = removeVertex(LINE, 0, 1) as { coordinates: number[][] };

    expect(removed.coordinates).toEqual([
      [0, 0],
      [20, 0],
    ]);
  });

  it('keeps a closed ring closed when vertex 0 goes', () => {
    // The ring now starts at what was vertex 1, and the closing coordinate has
    // to follow it or the polygon is open.
    const removed = removeVertex(SQUARE, 0, 0) as { coordinates: number[][][] };

    expect(removed.coordinates[0]![0]).toEqual([10, 0]);
    expect(removed.coordinates[0]!.at(-1)).toEqual([10, 0]);
    expect(removed.coordinates[0]).toHaveLength(4);
  });

  it('refuses to take a polygon ring below three vertices', () => {
    // §11.6: a rejection, not a cascade into deleting the ring. A user
    // pressing Delete on a vertex did not ask to delete a feature.
    const triangle: Geometry = {
      type: 'Polygon',
      coordinates: [
        [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 0],
        ],
      ],
    };

    expect(() => removeVertex(triangle, 0, 0)).toThrow(/needs at least 3/);
  });

  it('refuses to take a line below two vertices', () => {
    expect(() => removeVertex(BREAKLINE, 0, 0)).toThrow(/needs at least 2/);
  });

  it('says what to do instead', () => {
    expect(() => removeVertex(BREAKLINE, 0, 0)).toThrow(/Delete the whole feature/);
  });
});

describe('add then remove', () => {
  it('restores the geometry it started from', () => {
    // The round trip the undo stack relies on. Asserted on a closed ring
    // because that is where the closing coordinate can be left behind.
    const added = insertVertex(SQUARE, 0, 1, [10, 5]);
    const removed = removeVertex(added, 0, 2);

    expect(removed).toEqual(SQUARE);
  });

  it('restores a breakline including its elevations', () => {
    const added = insertVertex(BREAKLINE, 0, 0, [5, 0]);

    expect(removeVertex(added, 0, 1)).toEqual(BREAKLINE);
  });
});

describe('minimumRingSize', () => {
  it('is three for polygons and two for lines', () => {
    expect(minimumRingSize(SQUARE)).toBe(3);
    expect(minimumRingSize(LINE)).toBe(2);
  });
});
