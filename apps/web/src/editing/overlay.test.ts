/**
 * What the editor draws. `09-editing.md` §3.3, §6.7, §11.6.
 *
 * The assertions worth having are the ones about *absence*: the handle that
 * must not exist on a closed ring's repeated coordinate, the geometry a
 * deleted feature must not contribute, and the exclusion filter that must not
 * be applied to a layer with nothing to hide.
 */

import { describe, expect, it } from 'vitest';

import { EDIT_LAYERS, ROLE, buildOverlay, handleId, handlesFor } from './overlay.js';
import type { OverlayInput } from './overlay.js';
import type { Feature } from './session.js';

const SQUARE: Feature = {
  id: 'lease',
  geometry: {
    type: 'Polygon',
    coordinates: [
      [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1],
        [0, 0],
      ],
    ],
  },
  properties: { name: 'Section 14' },
};

const FAULT: Feature = {
  id: 'fault',
  geometry: {
    type: 'LineString',
    coordinates: [
      [0, 0],
      [1, 1],
    ],
  },
  properties: {},
};

function input(overrides: Partial<OverlayInput> = {}): OverlayInput {
  return {
    dirty: new Map(),
    handleFeatures: [],
    selectedVertices: [],
    snap: null,
    baseLayerIds: ['leases'],
    ...overrides,
  };
}

describe('handlesFor', () => {
  it('gives a closed ring one handle per distinct vertex', () => {
    // Four handles for a square drawn with five coordinates. Two stacked on
    // the same corner means a drag that moves one of them and leaves the
    // polygon open.
    const handles = handlesFor(SQUARE, new Set());

    expect(handles).toHaveLength(4);
    expect(handles.map((handle) => handle.id)).toEqual([
      'lease:0:0',
      'lease:0:1',
      'lease:0:2',
      'lease:0:3',
    ]);
  });

  it('gives an open ring a handle on every vertex including the last', () => {
    expect(handlesFor(FAULT, new Set())).toHaveLength(2);
  });

  it('carries the addressing a drag needs', () => {
    // Ring and ordinal, never a flat index: §6.6, because simplification means
    // index i in the tile is not index i in the source.
    const handle = handlesFor(SQUARE, new Set())[2]!;

    expect(handle.properties).toMatchObject({ featureId: 'lease', ring: 0, ordinal: 2 });
    expect(handle.geometry.coordinates).toEqual([1, 1]);
  });

  it('marks the selected vertices and only those', () => {
    const handles = handlesFor(SQUARE, new Set([handleId({ featureId: 'lease', ring: 0, ordinal: 1 })]));

    expect(handles.map((handle) => handle.properties!['selected'])).toEqual([
      false,
      true,
      false,
      false,
    ]);
  });

  it('gives a geometry it cannot ring no handles rather than throwing', () => {
    const odd: Feature = {
      id: 'odd',
      geometry: { type: 'GeometryCollection', geometries: [] },
      properties: {},
    };

    expect(handlesFor(odd, new Set())).toEqual([]);
  });
});

describe('buildOverlay', () => {
  it('draws a dirty feature and hides its tile copy', () => {
    const overlay = buildOverlay(input({ dirty: new Map([['lease', SQUARE]]) }));

    expect(overlay.features).toHaveLength(1);
    expect(overlay.features[0]!.properties).toMatchObject({
      [ROLE]: 'feature',
      name: 'Section 14',
    });
    expect(overlay.hideFromLayers).toEqual(['leases']);
  });

  it('contributes no geometry for a pending delete but still hides it', () => {
    // The tile copy filtered out and nothing drawn in its place is what makes
    // a delete look like a delete before the save lands.
    const overlay = buildOverlay(
      input({ dirty: new Map<string, Feature | null>([['lease', null]]) }),
    );

    expect(overlay.features).toEqual([]);
    expect(overlay.hideFromLayers).toEqual(['leases']);
  });

  it('applies no exclusion filter while the session is clean', () => {
    // A filter on a layer with nothing to hide costs an evaluation per feature
    // per frame for no effect.
    expect(buildOverlay(input()).hideFromLayers).toBeUndefined();
  });

  it('draws handles above features and the indicator above both', () => {
    // Draw order is source order within a layer, and layer order between them.
    // An indicator drawn under a handle is an indicator the user cannot see at
    // exactly the moment it matters.
    const overlay = buildOverlay(
      input({
        dirty: new Map([['fault', FAULT]]),
        handleFeatures: [FAULT],
        snap: { type: 'vertex', isExact: false, lngLat: [1, 1] },
      }),
    );

    expect(overlay.features.map((feature) => feature.properties![ROLE])).toEqual([
      'feature',
      'handle',
      'handle',
      'indicator',
    ]);
    expect(EDIT_LAYERS.map((layer) => layer.id)).toEqual([
      'edit-fill',
      'edit-line',
      'edit-point',
      'edit-handles',
      'edit-snap-indicator',
    ]);
  });

  it('names the hollow glyph for a tile-derived snap', () => {
    const overlay = buildOverlay(
      input({ snap: { type: 'edge', isExact: false, lngLat: [1, 1] } }),
    );

    expect(overlay.features[0]!.properties!['icon']).toBe('edit-snap-edge-tile');
  });

  it('puts the indicator where the snap landed, not under the cursor', () => {
    // The whole point of the indicator is to show that the two differ.
    const overlay = buildOverlay(
      input({ snap: { type: 'vertex', isExact: true, lngLat: [-102.08, 31.99] } }),
    );

    expect(overlay.features[0]!.geometry).toEqual({
      type: 'Point',
      coordinates: [-102.08, 31.99],
    });
  });

  it('draws no handles for a feature that is not selected', () => {
    // Handles on an unselected feature invite a drag no mode supports.
    const overlay = buildOverlay(input({ dirty: new Map([['lease', SQUARE]]) }));

    expect(overlay.features.filter((feature) => feature.properties![ROLE] === 'handle')).toEqual(
      [],
    );
  });
});

describe('EDIT_LAYERS', () => {
  it('lets handles overlap, or dense geometry loses them silently', () => {
    // MapLibre's collision detection drops colliding icons. A vertex whose
    // handle did not render is a vertex the user cannot edit and cannot see is
    // missing.
    for (const id of ['edit-handles', 'edit-snap-indicator']) {
      const layer = EDIT_LAYERS.find((candidate) => candidate.id === id)!;
      const layout = (layer as { layout: Record<string, unknown> }).layout;

      expect(layout['icon-allow-overlap']).toBe(true);
      expect(layout['icon-ignore-placement']).toBe(true);
    }
  });

  it('separates fills from lines by geometry type', () => {
    // A fill layer given a linestring renders nothing, and a line layer given
    // a polygon renders its boundary — so the filters are what make one
    // source hold both without either looking wrong.
    const filterOf = (id: string) =>
      JSON.stringify((EDIT_LAYERS.find((layer) => layer.id === id) as { filter?: unknown }).filter);

    expect(filterOf('edit-fill')).toContain('Polygon');
    expect(filterOf('edit-line')).toContain('Point');
  });
});
