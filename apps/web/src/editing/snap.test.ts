/**
 * The snapping engine. `09-editing.md` §6.
 *
 * The rules are where the bugs are, and every one of them is checkable without
 * a map because the engine takes pixels. That is the reason it takes pixels.
 */

import { describe, expect, it } from 'vitest';

import {
  MAX_PX,
  MIN_PX,
  defaultConfig,
  dragExclusion,
  perpendicularFoot,
  pixelTolerance,
  segmentIntersection,
  snap,
} from './snap.js';
import type { SnapCandidate, SnapConfig } from './snap.js';

/** A square, closed — the first coordinate repeated at the end. */
const SQUARE: SnapCandidate = {
  featureId: 'lease-1',
  layerId: 'leases',
  rings: [
    [
      { x: 100, y: 100 },
      { x: 200, y: 100 },
      { x: 200, y: 200 },
      { x: 100, y: 200 },
      { x: 100, y: 100 },
    ],
  ],
};

function config(overrides: Partial<SnapConfig> = {}): SnapConfig {
  return { ...defaultConfig(8, 8), ...overrides };
}

describe('tolerance', () => {
  it('converts feet to pixels at the current latitude and zoom', () => {
    // A geologist says "snap within 50 feet"; the engine works in pixels. At
    // z16 over the Permian that is about 7.5 px — comfortably inside both
    // clamps, which is the range where the configured distance is what
    // actually applies.
    const { px, clamped } = pixelTolerance(50, 32, 16);
    expect(px).toBeGreaterThan(MIN_PX);
    expect(px).toBeLessThan(MAX_PX);
    expect(clamped).toBeNull();
  });

  it('caps a 50 ft tolerance above about z17.4', () => {
    // Worth stating rather than discovering. Over the Permian a 50 ft
    // tolerance measures 7.5 px at z16, 15 px at z17 and 30 px at z18, so the
    // 20 px ceiling binds from roughly z17.4 — well inside ordinary editing
    // zooms. Above it the radius is 20 px of screen rather than 50 ft of
    // ground, which is the right behaviour (a 30 px radius grabs things the
    // cursor is nowhere near) and a behaviour change the badge has to report.
    expect(pixelTolerance(50, 32, 17).clamped).toBeNull();
    expect(pixelTolerance(50, 32, 18).clamped).toBe('ceiling');
  });

  it('floors the tolerance and says it did', () => {
    // 10 ft is roughly 6 px at z18 and well under 1 px at z15, at which point
    // snapping silently stops working. A silent behaviour change generates bug
    // reports; a badge generates none — so the caller is told.
    const { px, clamped } = pixelTolerance(10, 32, 14);
    expect(px).toBe(MIN_PX);
    expect(clamped).toBe('floor');
  });

  it('caps a tolerance that would swallow the screen', () => {
    const { px, clamped } = pixelTolerance(5000, 32, 20);
    expect(px).toBe(MAX_PX);
    expect(clamped).toBe('ceiling');
  });

  it('gives more pixels per foot as the map zooms in', () => {
    const near = pixelTolerance(50, 32, 19).px;
    const far = pixelTolerance(50, 32, 16).px;
    expect(near).toBeGreaterThan(far);
  });
});

describe('priority', () => {
  it('lets a vertex win over a nearer edge', () => {
    // **The rule that falls out of checking vertices first.** Comparing the two
    // distances against each other snaps to an edge 2 px away instead of the
    // vertex 3 px away, which is never what anybody wants.
    const pointer = { x: 150, y: 102 }; // 2 px from the top edge…
    const nearVertex: SnapCandidate = {
      featureId: 'lease-2',
      layerId: 'leases',
      rings: [[{ x: 153, y: 102 }]], // …and 3 px from this vertex.
    };

    const result = snap(pointer, [SQUARE, nearVertex], config());
    expect(result?.type).toBe('vertex');
    expect(result?.featureId).toBe('lease-2');
  });

  it('falls to an edge when no vertex is in range', () => {
    const result = snap({ x: 150, y: 104 }, [SQUARE], config());
    expect(result?.type).toBe('edge');
    expect(result?.pixel).toEqual({ x: 150, y: 100 });
    expect(result?.segmentIndex).toBe(0);
  });

  it('prefers an intersection to an edge', () => {
    // A geologist tying a fault into another fault needs the crossing, and
    // neither trace has a vertex there until they make one.
    const a: SnapCandidate = {
      featureId: 'fault-a',
      layerId: 'faults',
      rings: [
        [
          { x: 0, y: 50 },
          { x: 100, y: 50 },
        ],
      ],
    };
    const b: SnapCandidate = {
      featureId: 'fault-b',
      layerId: 'faults',
      rings: [
        [
          { x: 50, y: 0 },
          { x: 50, y: 100 },
        ],
      ],
    };

    const result = snap({ x: 52, y: 52 }, [a, b], config({ intersection: true }));
    expect(result?.type).toBe('intersection');
    expect(result?.pixel).toEqual({ x: 50, y: 50 });
  });

  it('prefers a midpoint to an edge', () => {
    const result = snap({ x: 150, y: 103 }, [SQUARE], config({ midpoint: true }));
    expect(result?.type).toBe('midpoint');
    expect(result?.pixel).toEqual({ x: 150, y: 100 });
  });

  it('returns nothing when everything is out of range', () => {
    expect(snap({ x: 500, y: 500 }, [SQUARE], config())).toBeNull();
  });

  it('respects a disabled pass', () => {
    const off = snap({ x: 150, y: 104 }, [SQUARE], config({ edge: false }));
    expect(off).toBeNull();
  });
});

describe('exclusions', () => {
  it('excludes the dragged vertex and both ring neighbours', () => {
    // Without the neighbours the handle sticks to the segment it is attached
    // to — the perpendicular foot of a point on its own segment is the point.
    // Without the vertex, it sticks to where it started.
    const exclusion = dragExclusion('lease-1', 0, 1, 5, true);
    expect(exclusion.refs).toEqual([
      { ring: 0, ordinal: 1 },
      { ring: 0, ordinal: 0 },
      { ring: 0, ordinal: 2 },
    ]);

    const result = snap({ x: 200, y: 100 }, [SQUARE], config(), exclusion);
    expect(result?.vertexRef).not.toEqual({ ring: 0, ordinal: 1 });
  });

  it('wraps the neighbours on a closed ring, skipping the duplicate', () => {
    // A closed ring repeats its first coordinate at the end. Wrapping onto the
    // duplicate would exclude a vertex that is already excluded and leave the
    // real neighbour live.
    const first = dragExclusion('lease-1', 0, 0, 5, true);
    expect(first.refs).toEqual([
      { ring: 0, ordinal: 0 },
      { ring: 0, ordinal: 3 },
      { ring: 0, ordinal: 1 },
    ]);
  });

  it('does not wrap on an open line', () => {
    const start = dragExclusion('fault-1', 0, 0, 4, false);
    expect(start.refs).toEqual([
      { ring: 0, ordinal: 0 },
      { ring: 0, ordinal: 1 },
    ]);
  });

  it('excludes only the named feature', () => {
    const other: SnapCandidate = {
      featureId: 'lease-9',
      layerId: 'leases',
      rings: [[{ x: 200, y: 100 }]],
    };
    const result = snap(
      { x: 200, y: 100 },
      [other],
      config(),
      dragExclusion('lease-1', 0, 1, 5, true),
    );
    expect(result?.featureId).toBe('lease-9');
  });
});

describe('geometry', () => {
  it('clamps the perpendicular foot to the segment', () => {
    // A pointer past the end lands on the endpoint, not on the infinite line —
    // which for a fault trace would put a snap kilometres off the end.
    expect(perpendicularFoot({ x: 300, y: 50 }, { x: 0, y: 0 }, { x: 100, y: 0 })).toEqual({
      x: 100,
      y: 0,
    });
    expect(perpendicularFoot({ x: -50, y: 50 }, { x: 0, y: 0 }, { x: 100, y: 0 })).toEqual({
      x: 0,
      y: 0,
    });
  });

  it('handles a zero-length segment without dividing by zero', () => {
    const point = { x: 5, y: 5 };
    expect(perpendicularFoot(point, { x: 1, y: 1 }, { x: 1, y: 1 })).toEqual({ x: 1, y: 1 });
  });

  it('finds a crossing and rejects a near miss', () => {
    expect(
      segmentIntersection({ x: 0, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }, { x: 10, y: 0 }),
    ).toEqual({ x: 5, y: 5 });

    // The segments' *lines* cross, but not within either segment.
    expect(
      segmentIntersection({ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 8, y: 10 }, { x: 10, y: 8 }),
    ).toBeNull();
  });

  it('gives no crossing for parallel or collinear segments', () => {
    // Collinear overlap has no single point to snap to.
    expect(
      segmentIntersection({ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 0, y: 5 }, { x: 10, y: 5 }),
    ).toBeNull();
    expect(
      segmentIntersection({ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 5, y: 0 }, { x: 15, y: 0 }),
    ).toBeNull();
  });

  it('does not offer a self-intersection', () => {
    // A self-intersection is a defect the validator reports; snapping to it
    // would be offering to build on it.
    const bowtie: SnapCandidate = {
      featureId: 'bad',
      layerId: 'leases',
      rings: [
        [
          { x: 0, y: 0 },
          { x: 10, y: 10 },
          { x: 0, y: 10 },
          { x: 10, y: 0 },
        ],
      ],
    };
    const result = snap(
      { x: 5, y: 5 },
      [bowtie],
      config({ intersection: true, vertex: false, edge: false }),
    );
    expect(result).toBeNull();
  });
});

describe('exactness', () => {
  it('reports tile-derived candidates as inexact', () => {
    // §6.6: the indicator renders hollow while tile-derived and filled once
    // exact, so a user can see which they have.
    const result = snap({ x: 100, y: 100 }, [SQUARE], config());
    expect(result?.isExact).toBe(false);
  });

  it('reports an exact candidate as exact', () => {
    const exact: SnapCandidate = { ...SQUARE, exact: true };
    expect(snap({ x: 100, y: 100 }, [exact], config())?.isExact).toBe(true);
  });

  it('calls an intersection exact only when both sides are', () => {
    const a: SnapCandidate = {
      featureId: 'a',
      layerId: 'faults',
      exact: true,
      rings: [
        [
          { x: 0, y: 50 },
          { x: 100, y: 50 },
        ],
      ],
    };
    const b: SnapCandidate = {
      featureId: 'b',
      layerId: 'faults',
      rings: [
        [
          { x: 50, y: 0 },
          { x: 50, y: 100 },
        ],
      ],
    };
    const result = snap({ x: 50, y: 50 }, [a, b], config({ intersection: true }));
    expect(result?.isExact).toBe(false);
  });
});
