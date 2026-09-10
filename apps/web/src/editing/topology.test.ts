/**
 * Topological editing. `09-editing.md` §7, `adr/0013`.
 *
 * The two things worth protecting are the two ways this feature goes wrong in
 * other tools: reusing the snap tolerance, which moves an unrelated vertex
 * twelve feet away, and skipping vertex-add, which opens a sliver a week later.
 */

import { describe, expect, it } from 'vitest';

import {
  DEFAULT_TOLERANCE_FT,
  buildIndex,
  coincident,
  key,
  nonTopologicalReason,
  others,
  propagates,
  sharedEdges,
} from './topology.js';
import type { IndexedFeature, Position, VertexRef } from './topology.js';

/**
 * Two squares sharing their vertical boundary at x = 10 — adjacent leases, the
 * case §7 exists for.
 */
const WEST: IndexedFeature = {
  featureId: 'west',
  closed: true,
  rings: [
    [
      [0, 0],
      [10, 0],
      [10, 10],
      [0, 10],
      [0, 0],
    ],
  ],
};

const EAST: IndexedFeature = {
  featureId: 'east',
  closed: true,
  rings: [
    [
      [10, 0],
      [20, 0],
      [20, 10],
      [10, 10],
      [10, 0],
    ],
  ],
};

describe('the coincidence index', () => {
  it('finds both features at a shared corner', () => {
    const index = buildIndex([WEST, EAST]);
    const refs = coincident(index, [10, 0]);

    expect(refs.map((ref) => ref.featureId).sort()).toEqual(['east', 'west']);
  });

  it('does not index a closed ring twice at its first coordinate', () => {
    // The repeated closing coordinate is not a separate vertex. Indexed, every
    // ring reports a coincidence with itself and every drag propagates to a
    // phantom.
    const index = buildIndex([WEST]);
    expect(coincident(index, [0, 0])).toHaveLength(1);
  });

  it('indexes the last coordinate of an open line', () => {
    const line: IndexedFeature = {
      featureId: 'fault',
      rings: [
        [
          [0, 0],
          [5, 5],
        ],
      ],
    };
    expect(coincident(buildIndex([line]), [5, 5])).toHaveLength(1);
  });

  it('treats coordinates that differ below the key precision as coincident', () => {
    // The tolerance is near-exact by design. This is the *equality* end of it:
    // a coordinate written back through a round trip differs in the last bit,
    // and it is still the same vertex.
    const a: Position = [10, 0];
    const b: Position = [10.000000004, 0];
    expect(key(a)).toBe(key(b));
  });

  it('treats a coordinate a foot away as a different vertex', () => {
    // **The failure this precision prevents.** Reusing the snap tolerance —
    // tens of feet — would make a drag move an unrelated vertex, which gets
    // reported as "the editor corrupted my layer".
    expect(key([10, 0])).not.toBe(key([10.5, 0]));
    expect(DEFAULT_TOLERANCE_FT).toBeLessThan(0.1);
  });

  it('finds nothing where nothing is', () => {
    expect(coincident(buildIndex([WEST]), [99, 99])).toEqual([]);
  });
});

describe('propagation targets', () => {
  it('includes the dragged vertex, and `others` removes it', () => {
    // Included on purpose: a caller applies the same new position to every
    // ref, and special-casing the origin means writing the move twice.
    const index = buildIndex([WEST, EAST]);
    const refs = coincident(index, [10, 10]);
    const origin = refs.find((ref) => ref.featureId === 'west')!;

    expect(refs).toHaveLength(2);
    expect(others(refs, origin).map((ref) => ref.featureId)).toEqual(['east']);
  });

  it('distinguishes two vertices of the same feature', () => {
    const refs: VertexRef[] = [
      { featureId: 'west', ring: 0, ordinal: 1, ringLength: 4, closed: true },
      { featureId: 'west', ring: 0, ordinal: 2, ringLength: 4, closed: true },
    ];
    expect(others(refs, refs[0]!)).toEqual([refs[1]]);
  });
});

describe('which operations propagate', () => {
  it('propagates a vertex add', () => {
    // **The classic half-implementation.** Inserting on a shared edge in one
    // polygon does not open a gap immediately — it guarantees one on the next
    // drag, invisible until somebody runs Validate a week later.
    expect(propagates('vertex.add', true)).toBe(true);
    expect(propagates('vertex.move', true)).toBe(true);
    expect(propagates('vertex.delete', true)).toBe(true);
    expect(propagates('edit.split', true)).toBe(true);
    expect(propagates('edit.reshape', true)).toBe(true);
  });

  it('does not propagate a feature move, rotate or scale', () => {
    // Ambiguous: does the neighbour stretch to follow, or translate with it?
    expect(propagates('transform.move', true)).toBe(false);
    expect(propagates('transform.rotate', true)).toBe(false);
    expect(propagates('transform.scale', true)).toBe(false);
  });

  it('propagates nothing while the toggle is off', () => {
    expect(propagates('vertex.move', false)).toBe(false);
  });

  it('gives the two non-topological operations a tooltip that says why', () => {
    // `09` §7.3 requires it. Without one, a user with the toggle on watches a
    // neighbour not follow and concludes the toggle is broken.
    expect(nonTopologicalReason('transform.move')).toMatch(/ambiguous/);
    expect(nonTopologicalReason('vertex.move')).toBeNull();
    expect(nonTopologicalReason('edit.copy')).toBeNull();
  });
});

describe('shared edges', () => {
  it('finds the edge two neighbours share', () => {
    const index = buildIndex([WEST, EAST]);
    const edges = sharedEdges(index, [10, 0], [10, 10]);

    expect(edges.map((edge) => edge.featureId).sort()).toEqual(['east', 'west']);
  });

  it('finds a neighbour that winds the other way', () => {
    // Ordinary for two polygons sharing a boundary: one runs the edge north
    // and the other south. An implementation that only accepted an increasing
    // ordinal pair would insert into one of the two.
    const index = buildIndex([WEST, EAST]);
    const forward = sharedEdges(index, [10, 0], [10, 10]);
    const backward = sharedEdges(index, [10, 10], [10, 0]);

    expect(backward.map((e) => e.featureId).sort()).toEqual(
      forward.map((e) => e.featureId).sort(),
    );
  });

  it('does not treat a shared corner as a shared edge', () => {
    // **Both endpoints must coincide.** Two polygons meeting at one corner
    // share a vertex and no edge, and inserting into both would move a
    // boundary that is not there.
    const north: IndexedFeature = {
      featureId: 'north',
      closed: true,
      rings: [
        [
          [10, 10],
          [20, 10],
          [20, 20],
          [10, 20],
          [10, 10],
        ],
      ],
    };
    const index = buildIndex([WEST, north]);
    const edges = sharedEdges(index, [10, 10], [0, 10]);

    expect(edges.map((edge) => edge.featureId)).toEqual(['west']);
  });

  it('reports where in the ring to insert', () => {
    const index = buildIndex([WEST]);
    const [edge] = sharedEdges(index, [10, 0], [10, 10]);

    expect(edge).toEqual({ featureId: 'west', ring: 0, afterOrdinal: 1 });
  });

  it('finds nothing between features that touch nowhere', () => {
    const far: IndexedFeature = {
      featureId: 'far',
      closed: true,
      rings: [
        [
          [100, 100],
          [110, 100],
          [110, 110],
          [100, 100],
        ],
      ],
    };
    expect(sharedEdges(buildIndex([WEST, far]), [10, 0], [10, 10])).toHaveLength(1);
  });
});
