/**
 * The bridge between MapLibre's feature shapes and the editing model's.
 *
 * These are the conversions where a plausible-looking mistake stays invisible
 * until it has corrupted a layer: losing a ring's closedness, inventing an id
 * for a feature that has none, or hashing pixels where the model expects
 * coordinates. Each is asserted rather than assumed.
 */

import { describe, expect, it } from 'vitest';

import {
  featureId,
  hitFeatureIds,
  overlayFeatures,
  queryBox,
  ringsOf,
  toCandidates,
  toIndexedFeatures,
} from './mapBridge.js';
import type { Project, QueriedFeature } from './mapBridge.js';
import type { Feature } from './session.js';

/** A hundred pixels per degree, so a projected value is arithmetic a reader
 *  can check rather than a Mercator number they have to trust. */
const project: Project = ([lng, lat]) => [lng * 100, lat * 100];

function queried(
  id: string | number | undefined,
  geometry: QueriedFeature['geometry'],
  layerId = 'leases',
): QueriedFeature {
  return { id, layer: { id: layerId }, geometry };
}

describe('ringsOf', () => {
  it('gives a point one ring of one vertex', () => {
    expect(ringsOf({ type: 'Point', coordinates: [1, 2] })).toEqual({
      rings: [[[1, 2]]],
      closed: false,
    });
  });

  it('gives a multipoint one ring per point', () => {
    // Not one ring of many: a multipoint's members are not connected, and a
    // single ring would let the edge pass snap to a segment between two wells.
    expect(
      ringsOf({
        type: 'MultiPoint',
        coordinates: [
          [1, 2],
          [3, 4],
        ],
      }),
    ).toEqual({
      rings: [[[1, 2]], [[3, 4]]],
      closed: false,
    });
  });

  it('marks a line open and a polygon closed', () => {
    const line = ringsOf({
      type: 'LineString',
      coordinates: [
        [0, 0],
        [1, 1],
      ],
    });
    const polygon = ringsOf({
      type: 'Polygon',
      coordinates: [
        [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 0],
        ],
      ],
    });

    expect(line.closed).toBe(false);
    expect(polygon.closed).toBe(true);
  });

  it('carries a polygon hole as a ring of its own', () => {
    const { rings } = ringsOf({
      type: 'Polygon',
      coordinates: [
        [
          [0, 0],
          [10, 0],
          [10, 10],
          [0, 0],
        ],
        [
          [2, 2],
          [4, 2],
          [4, 4],
          [2, 2],
        ],
      ],
    });

    expect(rings).toHaveLength(2);
    expect(rings[1]![0]).toEqual([2, 2]);
  });

  it('flattens a multipolygon rather than nesting parts', () => {
    // The model addresses a vertex as (ring, ordinal). A third level would
    // mean threading a part index through the snap engine and every delta.
    const { rings, closed } = ringsOf({
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
    });

    expect(rings).toHaveLength(2);
    expect(rings[1]![0]).toEqual([5, 5]);
    expect(closed).toBe(true);
  });

  it('drops the Z of a 3D breakline', () => {
    // Vertex addressing is 2D by construction. The elevation is not lost from
    // the feature — this module never touches the session's geometry — only
    // from the coordinates snapping and coincidence reason about.
    expect(
      ringsOf({
        type: 'LineString',
        coordinates: [
          [1, 2, -8450],
          [3, 4, -8460],
        ],
      }).rings,
    ).toEqual([
      [
        [1, 2],
        [3, 4],
      ],
    ]);
  });

  it('refuses a GeometryCollection, naming the fix', () => {
    expect(() => ringsOf({ type: 'GeometryCollection', geometries: [] })).toThrow(
      /Explode it first/,
    );
  });

  it('refuses a coordinate with one ordinate', () => {
    expect(() => ringsOf({ type: 'Point', coordinates: [1] })).toThrow(
      /at least an x and a y/,
    );
  });
});

describe('featureId', () => {
  it('renders a numeric id as a string', () => {
    expect(featureId(queried(42, { type: 'Point', coordinates: [0, 0] }))).toBe('42');
  });

  it('keeps the id 0', () => {
    // A truthiness check here would reject the first feature of any layer
    // whose ids start at zero, which is most of them.
    expect(featureId(queried(0, { type: 'Point', coordinates: [0, 0] }))).toBe('0');
  });

  it('refuses a feature with no id, naming promoteId', () => {
    // Defaulting to an index would put the edit on whichever feature happened
    // to be drawn in that position.
    expect(() =>
      featureId(queried(undefined, { type: 'Point', coordinates: [0, 0] })),
    ).toThrow(/promoteId/);
  });
});

describe('toCandidates', () => {
  it('projects to pixels and never claims to be exact', () => {
    // These come from queryRenderedFeatures — tile geometry, simplified and
    // clipped (§6.6). The indicator stays hollow until exact geometry lands.
    const candidates = toCandidates(
      [
        queried('a', {
          type: 'LineString',
          coordinates: [
            [1, 2],
            [3, 4],
          ],
        }),
      ],
      project,
    );

    expect(candidates).toEqual([
      {
        featureId: 'a',
        layerId: 'leases',
        rings: [
          [
            { x: 100, y: 200 },
            { x: 300, y: 400 },
          ],
        ],
        exact: false,
      },
    ]);
  });

  it('skips a feature it cannot ring rather than failing the pass', () => {
    // A query over a box legitimately returns a raster or an empty geometry.
    // Refusing the whole pass over one would take snapping down for a layer
    // that merely happens to be underneath.
    const candidates = toCandidates(
      [
        queried('collection', { type: 'GeometryCollection', geometries: [] }),
        queried('empty', { type: 'MultiLineString', coordinates: [] }),
        queried('good', { type: 'Point', coordinates: [1, 1] }),
      ],
      project,
    );

    expect(candidates.map((candidate) => candidate.featureId)).toEqual(['good']);
  });
});

describe('toIndexedFeatures', () => {
  it('leaves coordinates unprojected', () => {
    // topology.ts hashes these to decide whether two vertices are the same.
    // Hashing pixels would make coincidence a function of zoom — two vertices
    // a foot apart would be "the same vertex" when zoomed out.
    const [indexed] = toIndexedFeatures([
      queried('a', {
        type: 'Polygon',
        coordinates: [
          [
            [1, 2],
            [3, 4],
            [3, 2],
            [1, 2],
          ],
        ],
      }),
    ]);

    expect(indexed!.rings[0]![0]).toEqual([1, 2]);
    expect(indexed!.closed).toBe(true);
  });
});

describe('hitFeatureIds', () => {
  it('keeps render order, topmost first', () => {
    // Clicking where two leases overlap selects the one the user can see.
    expect(
      hitFeatureIds([
        queried('top', { type: 'Point', coordinates: [0, 0] }),
        queried('under', { type: 'Point', coordinates: [0, 0] }),
      ]),
    ).toEqual(['top', 'under']);
  });

  it('collapses a multipolygon returned once per part', () => {
    expect(
      hitFeatureIds([
        queried('field', { type: 'Point', coordinates: [0, 0] }),
        queried('field', { type: 'Point', coordinates: [0, 0] }),
      ]),
    ).toEqual(['field']);
  });

  it('drops an id-less feature instead of throwing', () => {
    // Unlike featureId: a hit test crosses layers that were never set up for
    // editing, and a basemap road with no id should not abort the click.
    expect(
      hitFeatureIds([
        queried(undefined, { type: 'Point', coordinates: [0, 0] }),
        queried('lease', { type: 'Point', coordinates: [0, 0] }),
      ]),
    ).toEqual(['lease']);
  });
});

describe('queryBox', () => {
  it('grows the pointer by the tolerance in both directions', () => {
    expect(queryBox({ x: 100, y: 200 }, 12)).toEqual([
      [88, 188],
      [112, 212],
    ]);
  });
});

describe('overlayFeatures', () => {
  const lease = (id: string): Feature => ({
    id,
    geometry: { type: 'Point', coordinates: [1, 2] },
    properties: { name: id },
  });

  it('carries an edited feature through with its id', () => {
    const { features, hiddenIds } = overlayFeatures(new Map([['a', lease('a')]]));

    expect(hiddenIds).toEqual(['a']);
    expect(features[0]).toEqual({
      type: 'Feature',
      id: 'a',
      geometry: { type: 'Point', coordinates: [1, 2] },
      properties: { name: 'a' },
    });
  });

  it('hides a pending delete without drawing anything in its place', () => {
    // Omitting the id from hiddenIds instead would leave the tile copy on
    // screen until the save landed, so the delete would appear not to work.
    const { features, hiddenIds } = overlayFeatures(
      new Map<string, Feature | null>([
        ['a', lease('a')],
        ['gone', null],
      ]),
    );

    expect(hiddenIds).toEqual(['a', 'gone']);
    expect(features.map((feature) => feature.id)).toEqual(['a']);
  });

  it('is empty for a clean session', () => {
    expect(overlayFeatures(new Map())).toEqual({ features: [], hiddenIds: [] });
  });
});
