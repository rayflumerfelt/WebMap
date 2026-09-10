/**
 * The map-facing half of snapping. `09-editing.md` §6.4, §6.5.
 *
 * The rules worth protecting are the three that are invisible when wrong: a
 * stale tile copy shadowing an edit the user has already made, a projection
 * cache that outlives the camera position it was built for, and a tolerance
 * clamp that changes behaviour without saying so.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { QueriedFeature } from './mapBridge.js';
import type { Feature } from './session.js';
import { MAX_PX, MIN_PX } from './snap.js';
import type { SnapCandidate } from './snap.js';
import {
  DEFAULT_SNAP_SETTINGS,
  createSnapEngine,
  tolerancesFor,
  withLocal,
} from './snapPipeline.js';
import type { LocalGeometry, SnapDeps, SnapRequest, SnapSettings } from './snapPipeline.js';

/** A hundred pixels per degree, so every expected pixel is arithmetic. */
const project = ([lng, lat]: [number, number]): [number, number] => [lng * 100, lat * 100];
const unproject = ([x, y]: [number, number]): [number, number] => [x / 100, y / 100];

const MIDLAND = { latitude: 31.99, zoom: 18 };

/** The active layer's local geometry, empty unless a test fills it. */
function local(overrides: Partial<LocalGeometry> = {}): LocalGeometry {
  return {
    dirty: new Map(),
    exact: new Map(),
    layerIds: ['leases'],
    activeLayerId: 'leases',
    ...overrides,
  };
}

function line(id: string, coordinates: number[][], layerId = 'leases'): QueriedFeature {
  return { id, layer: { id: layerId }, geometry: { type: 'LineString', coordinates } };
}

function dirtyLine(id: string, coordinates: number[][]): Feature {
  return { id, geometry: { type: 'LineString', coordinates }, properties: {} };
}

function harness(
  features: QueriedFeature[],
  overrides: Partial<SnapSettings> = {},
): {
  deps: SnapDeps & { queryFeatures: ReturnType<typeof vi.fn> };
  settings: SnapSettings;
  request(partial?: Partial<SnapRequest>): SnapRequest;
} {
  const settings: SnapSettings = {
    ...DEFAULT_SNAP_SETTINGS,
    layerIds: ['leases'],
    // A tolerance in feet is meaningless in this harness's fake projection —
    // fix the pixel radius instead by asking for one the clamp will pin.
    vertexToleranceFt: 1_000_000,
    edgeToleranceFt: 1_000_000,
    ...overrides,
  };
  const deps = {
    queryFeatures: vi.fn(() => features),
    project,
    unproject,
  };
  return {
    deps,
    settings,
    request: (partial = {}) => ({
      pointer: { x: 0, y: 0 },
      camera: MIDLAND,
      local: local(),
      ...partial,
    }),
  };
}

describe('tolerancesFor', () => {
  it('reports the ceiling, which binds at ordinary editing zooms', () => {
    // §6.3, measured over the Permian: 50 ft is 30 px at z18, so the 20 px
    // ceiling takes over from about z17.4 — inside the range people edit at,
    // not an edge case.
    const report = tolerancesFor(
      { ...DEFAULT_SNAP_SETTINGS, vertexToleranceFt: 50 },
      { latitude: 31.99, zoom: 18 },
    );

    expect(report.vertexPx).toBe(MAX_PX);
    expect(report.clamped).toBe('ceiling');
  });

  it('reports the floor, where snapping would otherwise stop working', () => {
    const report = tolerancesFor(
      { ...DEFAULT_SNAP_SETTINGS, vertexToleranceFt: 10, edgeToleranceFt: 10 },
      { latitude: 31.99, zoom: 14 },
    );

    expect(report.vertexPx).toBe(MIN_PX);
    expect(report.clamped).toBe('floor');
  });

  it('reports nothing when the configured distance is what is in force', () => {
    const report = tolerancesFor(
      { ...DEFAULT_SNAP_SETTINGS, vertexToleranceFt: 50, edgeToleranceFt: 50 },
      { latitude: 31.99, zoom: 16 },
    );

    expect(report.clamped).toBeNull();
    expect(report.vertexPx).toBeGreaterThan(MIN_PX);
    expect(report.vertexPx).toBeLessThan(MAX_PX);
  });
});

describe('withLocal', () => {
  const dirty = new Map<string, Feature | null>([
    ['moved', dirtyLine('moved', [[5, 5], [6, 6]])],
  ]);

  const tile = (featureId: string, layerId = 'leases'): SnapCandidate => ({
    featureId,
    layerId,
    rings: [[{ x: 0, y: 0 }]],
  });

  it('replaces the tile copy of an edited feature', () => {
    // The tile copy is stale by definition. Snapping to it would put the new
    // boundary where the old one used to be — the sliver §6 exists to prevent.
    const candidates = withLocal(
      [tile('moved'), tile('still')],
      local({ dirty }),
      project,
    );

    expect(candidates.map((candidate) => candidate.featureId)).toEqual(['moved', 'still']);
    expect(candidates[0]!.rings[0]![0]).toEqual({ x: 500, y: 500 });
  });

  it('marks a dirty candidate exact', () => {
    // The local edit buffer is the exact geometry for a pending edit (§3.3);
    // there is nothing more authoritative to resolve it against.
    const [candidate] = withLocal([tile('moved')], local({ dirty }), project);

    expect(candidate!.exact).toBe(true);
  });

  it('replaces an untouched feature with the working set’s geometry', () => {
    // §6.6 by lookup rather than coordinate matching: the working set *is* the
    // exact geometry, so there is nothing to reconcile — and the indicator
    // fills in, which is what `isExact` means to the user.
    const candidates = withLocal(
      [tile('parcel')],
      local({ exact: new Map([['parcel', dirtyLine('parcel', [[7, 7], [8, 8]])]]) }),
      project,
    );

    expect(candidates[0]!.exact).toBe(true);
    expect(candidates[0]!.rings[0]![0]).toEqual({ x: 700, y: 700 });
  });

  it('leaves a feature outside the working set on its tile geometry', () => {
    // Which is what a layer past the 5,000-feature cap looks like: drawn from
    // tiles, snappable, and honestly reported as inexact.
    const candidates = withLocal([tile('far-away')], local(), project);

    expect(candidates[0]!.exact).toBeUndefined();
    expect(candidates[0]!.rings[0]![0]).toEqual({ x: 0, y: 0 });
  });

  it('removes a pending delete from the candidate set', () => {
    // Immediately, rather than when the save lands: a feature the user has
    // deleted must stop pulling their cursor.
    const candidates = withLocal(
      [tile('gone')],
      local({ dirty: new Map<string, Feature | null>([['gone', null]]) }),
      project,
    );

    expect(candidates).toEqual([]);
  });

  it('adds a dirty feature the tile query did not return', () => {
    // A feature dragged out of the query box is still being edited, and
    // losing it as a snap target mid-drag is the bug this covers.
    const candidates = withLocal([], local({ dirty }), project);

    expect(candidates.map((candidate) => candidate.featureId)).toEqual(['moved']);
  });

  it('leaves another layer’s feature 42 alone', () => {
    // Ids are assigned per dataset, so layer A's feature 42 and layer B's
    // feature 42 both exist. Substituting across layers would move a snap
    // target onto a different feature entirely.
    const candidates = withLocal(
      [tile('moved', 'faults')],
      local({ dirty }),
      project,
    );

    const other = candidates.find((candidate) => candidate.layerId === 'faults');
    expect(other!.rings[0]![0]).toEqual({ x: 0, y: 0 });
    expect(other!.exact).toBeUndefined();
  });

  it('substitutes through a second layer over the same source', () => {
    // A highlight layer drawn from the same source is one of the active
    // layer's own, so its copy is stale in exactly the same way.
    const candidates = withLocal(
      [tile('moved', 'leases-highlight')],
      local({ dirty, layerIds: ['leases', 'leases-highlight'] }),
      project,
    );

    expect(candidates).toHaveLength(1);
    expect(candidates[0]!.rings[0]![0]).toEqual({ x: 500, y: 500 });
  });

  it('keeps going past a local geometry it cannot ring', () => {
    const candidates = withLocal(
      [],
      local({
        dirty: new Map<string, Feature | null>([
          [
            'odd',
            { id: 'odd', geometry: { type: 'GeometryCollection', geometries: [] }, properties: {} },
          ],
          ['fine', dirtyLine('fine', [[1, 1], [2, 2]])],
        ]),
      }),
      project,
    );

    expect(candidates.map((candidate) => candidate.featureId)).toEqual(['fine']);
  });
});

describe('createSnapEngine', () => {
  it('snaps the pointer to a nearby vertex and unprojects the winner', () => {
    const { deps, settings, request } = harness([line('a', [[1, 1], [2, 2]])]);
    const engine = createSnapEngine(deps, () => settings);

    const outcome = engine.snapAt(request({ pointer: { x: 103, y: 98 } }));

    expect(outcome.result?.type).toBe('vertex');
    expect(outcome.result?.featureId).toBe('a');
    expect(outcome.lngLat).toEqual([1, 1]);
  });

  it('hands back the raw pointer position when nothing snaps', () => {
    // The caller always has a coordinate to place — a click in empty space is
    // a vertex at the cursor, not a no-op.
    const { deps, settings, request } = harness([]);
    const engine = createSnapEngine(deps, () => settings);

    const outcome = engine.snapAt(request({ pointer: { x: 250, y: 400 } }));

    expect(outcome.result).toBeNull();
    expect(outcome.lngLat).toEqual([2.5, 4]);
  });

  it('queries nothing at all when the master toggle is off', () => {
    const { deps, settings, request } = harness([line('a', [[1, 1], [2, 2]])], {
      enabled: false,
    });
    const engine = createSnapEngine(deps, () => settings);

    const outcome = engine.snapAt(request({ pointer: { x: 100, y: 100 } }));

    expect(outcome.result).toBeNull();
    expect(deps.queryFeatures).not.toHaveBeenCalled();
  });

  it('still reports the tolerance while snapping is off', () => {
    // The badge is about the configured distance, not about whether a snap
    // happened; hiding it when the toggle is off would hide it exactly when
    // the user is deciding whether to turn snapping back on.
    const { deps, settings, request } = harness([], {
      enabled: false,
      vertexToleranceFt: 50,
      edgeToleranceFt: 50,
    });
    const engine = createSnapEngine(deps, () => settings);

    expect(engine.snapAt(request()).tolerance.clamped).toBe('ceiling');
  });

  it('does nothing when no layer is snappable', () => {
    const { deps, settings, request } = harness([line('a', [[1, 1], [2, 2]])], {
      layerIds: [],
    });
    const engine = createSnapEngine(deps, () => settings);

    expect(engine.snapAt(request({ pointer: { x: 100, y: 100 } })).result).toBeNull();
    expect(deps.queryFeatures).not.toHaveBeenCalled();
  });

  it('reads settings per call, so a toolbar toggle takes effect immediately', () => {
    const { deps, request } = harness([line('a', [[1, 1], [2, 2]])]);
    let settings: SnapSettings = {
      ...DEFAULT_SNAP_SETTINGS,
      layerIds: ['leases'],
      vertexToleranceFt: 1_000_000,
      edgeToleranceFt: 1_000_000,
    };
    const engine = createSnapEngine(deps, () => settings);

    expect(engine.snapAt(request({ pointer: { x: 100, y: 100 } })).result).not.toBeNull();
    settings = { ...settings, enabled: false };
    expect(engine.snapAt(request({ pointer: { x: 100, y: 100 } })).result).toBeNull();
  });

  describe('the pointer box', () => {
    it('expands by the larger of the two tolerances', () => {
      // An edge candidate just outside the vertex radius would otherwise never
      // be returned, and the edge pass would silently have nothing to work on.
      const { deps, settings, request } = harness([], {
        vertexToleranceFt: 1,
        edgeToleranceFt: 1_000_000,
      });
      const engine = createSnapEngine(deps, () => settings);

      engine.snapAt(request({ pointer: { x: 100, y: 100 } }));

      expect(deps.queryFeatures).toHaveBeenCalledWith(
        [
          [100 - MAX_PX, 100 - MAX_PX],
          [100 + MAX_PX, 100 + MAX_PX],
        ],
        ['leases'],
      );
    });
  });

  describe('the drag cache', () => {
    const drag = { featureId: 'b', ring: 0, ordinal: 0, ringLength: 2, closed: false };

    let engine: ReturnType<typeof createSnapEngine>;
    let deps: SnapDeps & { queryFeatures: ReturnType<typeof vi.fn> };
    let request: (partial?: Partial<SnapRequest>) => SnapRequest;

    beforeEach(() => {
      const built = harness([line('a', [[1, 1], [2, 2]]), line('b', [[9, 9], [8, 8]])]);
      deps = built.deps;
      request = built.request;
      engine = createSnapEngine(deps, () => built.settings);
    });

    it('queries the whole viewport once and reuses it', () => {
      // §6.5: the camera does not move mid-drag, so a candidate's pixels do
      // not change. Re-projecting every vertex in view at 60 Hz is the most
      // expensive thing this subsystem does.
      engine.snapAt(request({ pointer: { x: 100, y: 100 }, drag }));
      engine.snapAt(request({ pointer: { x: 110, y: 110 }, drag }));
      engine.snapAt(request({ pointer: { x: 120, y: 120 }, drag }));

      expect(deps.queryFeatures).toHaveBeenCalledTimes(1);
      expect(deps.queryFeatures).toHaveBeenCalledWith(null, ['leases']);
    });

    it('re-queries after the camera moves', () => {
      // Edge-panning during a drag makes every cached pixel wrong at once.
      engine.snapAt(request({ pointer: { x: 100, y: 100 }, drag }));
      engine.invalidate();
      engine.snapAt(request({ pointer: { x: 100, y: 100 }, drag }));

      expect(deps.queryFeatures).toHaveBeenCalledTimes(2);
    });

    it('re-queries for the next hover after the drag ends', () => {
      engine.snapAt(request({ pointer: { x: 100, y: 100 }, drag }));
      engine.endDrag();
      engine.snapAt(request({ pointer: { x: 100, y: 100 } }));

      expect(deps.queryFeatures).toHaveBeenCalledTimes(2);
      expect(deps.queryFeatures).toHaveBeenLastCalledWith(expect.any(Array), ['leases']);
    });

    it('excludes the dragged vertex, or the handle sticks to itself', () => {
      // The pointer sits exactly on the vertex being dragged, at (900, 900),
      // with another vertex of the same feature 7 px away. Without the
      // exclusion the winner is the dragged vertex at distance zero and the
      // drag never moves; with it, the neighbour 7 px off wins.
      const built = harness([line('b', [[9, 9], [8, 8], [9.05, 9.05]])]);
      const dragEngine = createSnapEngine(built.deps, () => built.settings);

      const outcome = dragEngine.snapAt(
        built.request({
          pointer: { x: 900, y: 900 },
          drag: { featureId: 'b', ring: 0, ordinal: 0, ringLength: 3, closed: false },
        }),
      );

      expect(outcome.result?.vertexRef).toEqual({ ring: 0, ordinal: 2 });
    });

    it('excludes the whole edited feature when snapToSelf is off', () => {
      // Vertex 2 is neither the dragged vertex nor a ring neighbour of it, so
      // `dragExclusion` leaves it snappable. Only the toggle removes it — and
      // if it did not, the toggle would be honoured in name only.
      const geometry = [[9, 9], [8, 8], [9.05, 9.05]];
      const dragging = { featureId: 'b', ring: 0, ordinal: 0, ringLength: 3, closed: false };
      const pointer = { x: 900, y: 900 };

      const on = harness([line('b', geometry)]);
      expect(
        createSnapEngine(on.deps, () => on.settings).snapAt(on.request({ pointer, drag: dragging }))
          .result,
      ).not.toBeNull();

      const off = harness([line('b', geometry)], { snapToSelf: false });
      expect(
        createSnapEngine(off.deps, () => off.settings).snapAt(
          off.request({ pointer, drag: dragging }),
        ).result,
      ).toBeNull();
    });
  });
});
