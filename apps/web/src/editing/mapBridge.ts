/**
 * Between MapLibre's feature shapes and the editing model's.
 * `09-editing.md` §3.3, §6.1.
 *
 * The editing model is deliberately free of MapLibre: `snap.ts` takes pixels,
 * `topology.ts` takes coordinates, `session.ts` takes features whose geometry
 * it never inspects. **This file is the one place that knows both**, which is
 * what keeps that true — and it is a set of pure functions over plain data, so
 * every rule below is testable without a browser.
 *
 * Two conversions matter and both have a trap in them:
 *
 * **Rings, flattened, with closedness carried alongside.** A polygon's ring is
 * closed — its first coordinate repeated at the end — and a line's is not.
 * `dragExclusion` wraps neighbours only on a closed ring and `buildIndex` must
 * not index the duplicate, so losing that distinction here produces a drag
 * handle that sticks to itself and a coincidence index that reports every ring
 * coincident with itself.
 *
 * **Feature identity is a string, and an absent id is fatal rather than
 * defaulted.** MapLibre hands back `string | number | undefined`, and a
 * feature with no id cannot be written back to anything: the dirty buffer is
 * keyed on it, the exclusion filter matches on it, and the save endpoint
 * addresses features by it. Silently substituting an index would put an edit
 * on whichever feature happened to be drawn in that position.
 */

import type {
  Feature as GeoJsonFeature,
  Position as GeoJsonPosition,
  Geometry,
} from 'geojson';

import type { Feature } from './session.js';
import type { Pixel, SnapCandidate } from './snap.js';
import type { IndexedFeature, Position } from './topology.js';

/** What `queryFeatures` hands back, narrowed to what this module reads. */
export interface QueriedFeature {
  id?: string | number | undefined;
  layer: { id: string };
  geometry: Geometry;
  properties?: Record<string, unknown> | null;
}

/** Projects WGS84 to screen pixels — `WebMapHandle.project`. */
export type Project = (lngLat: [number, number]) => [number, number];

/** One geometry's rings, with whether they close. */
export interface Rings {
  rings: Position[][];
  closed: boolean;
}

/**
 * A geometry's coordinate rings, flattened across parts.
 *
 * A multipolygon's rings run on in one list rather than nesting: the model
 * addresses a vertex as `(ring, ordinal)` and a third level would mean
 * threading a part index through the snap engine, the coincidence index and
 * every delta — for a distinction none of them make.
 *
 * Returns no rings for a geometry with none. A `GeometryCollection` is refused
 * rather than walked: it can mix closed and open rings under one feature, and
 * this function's whole output is one `closed` flag.
 */
export function ringsOf(geometry: Geometry): Rings {
  switch (geometry.type) {
    case 'Point':
      return { rings: [[toPosition(geometry.coordinates)]], closed: false };
    case 'MultiPoint':
      return {
        rings: geometry.coordinates.map((position) => [toPosition(position)]),
        closed: false,
      };
    case 'LineString':
      return { rings: [geometry.coordinates.map(toPosition)], closed: false };
    case 'MultiLineString':
      return {
        rings: geometry.coordinates.map((ring) => ring.map(toPosition)),
        closed: false,
      };
    case 'Polygon':
      return { rings: geometry.coordinates.map((ring) => ring.map(toPosition)), closed: true };
    case 'MultiPolygon':
      return {
        rings: geometry.coordinates.flat().map((ring) => ring.map(toPosition)),
        closed: true,
      };
    case 'GeometryCollection':
      throw new Error(
        'A GeometryCollection cannot be edited as one feature: it can hold ' +
          'closed and open rings at once, and vertex addressing needs a single ' +
          'answer to "does this ring close". Explode it first.',
      );
  }
}

/**
 * The id a feature is addressed by, as a string.
 *
 * Throws rather than defaulting. `09` §6.6 prefers server-provided identity
 * precisely because a guessed one is worse than none: the dirty buffer, the
 * exclusion filter and the save endpoint all key on this, so an invented id
 * puts an edit on whichever feature happened to be drawn in that position.
 */
export function featureId(feature: QueriedFeature): string {
  if (feature.id === undefined || feature.id === '') {
    throw new Error(
      `A feature in layer '${feature.layer.id}' came back with no id, so it ` +
        `cannot be edited: the dirty buffer, the tile-exclusion filter and the ` +
        `save endpoint all address features by id. Check the tile source sets ` +
        `\`promoteId\` — MapLibre drops a feature's id unless it is told which ` +
        `property carries it.`,
    );
  }
  return String(feature.id);
}

/**
 * Queried features as snap candidates, projected to pixels.
 *
 * **`exact: false`, always.** These come from `queryRenderedFeatures`, which
 * reads tile geometry — simplified and clipped (`09` §6.6). The indicator
 * renders hollow until the exact geometry has been resolved, and a candidate
 * that claimed otherwise here would make that distinction meaningless.
 *
 * A feature whose geometry has no rings is skipped rather than raising: a
 * query over a box legitimately returns a raster feature or an empty geometry,
 * and refusing the whole pass over one would take snapping down for a layer
 * that merely happens to be underneath.
 */
export function toCandidates(
  features: readonly QueriedFeature[],
  project: Project,
): SnapCandidate[] {
  const candidates: SnapCandidate[] = [];

  for (const feature of features) {
    let rings: Rings;
    try {
      rings = ringsOf(feature.geometry);
    } catch {
      continue;
    }
    if (rings.rings.length === 0) continue;

    candidates.push({
      featureId: featureId(feature),
      layerId: feature.layer.id,
      rings: rings.rings.map((ring) =>
        // Spread rather than pass: the model's `Position` is readonly and
        // the map handle's signature is not.
        ring.map(([x, y]) => toPixel(project([x, y]))),
      ),
      exact: false,
    });
  }
  return candidates;
}

function toPixel(point: [number, number]): Pixel {
  return { x: point[0], y: point[1] };
}

/**
 * A GeoJSON position as the two ordinates the editing model addresses.
 *
 * **Z is dropped here and kept everywhere it matters.** Snapping and the
 * coincidence index are 2D by construction — a breakline's elevation is not a
 * thing to snap to — but the feature written back on save is the session's own,
 * whose geometry this module never touches. So a 3D breakline keeps its Z
 * through an edit; only the vertex *addressing* is flat.
 */
function toPosition(position: GeoJsonPosition): Position {
  const [x, y] = position;
  if (x === undefined || y === undefined) {
    throw new Error(
      `A coordinate came back with ${position.length} ordinate(s): ` +
        `${JSON.stringify(position)}. Every position needs at least an x and a y.`,
    );
  }
  return [x, y];
}

/**
 * Queried features as coincidence-index input, in **layer coordinates**.
 *
 * Not projected, and that is the point: `topology.ts` hashes coordinates to
 * decide whether two vertices are the same, at a precision three orders of
 * magnitude below the snap tolerance (§7.2). Hashing *pixels* would make
 * coincidence a function of the current zoom — two vertices a foot apart would
 * be "the same vertex" when zoomed out, which is exactly the bug that gets
 * reported as the editor corrupting a layer.
 */
export function toIndexedFeatures(
  features: readonly QueriedFeature[],
): IndexedFeature[] {
  const indexed: IndexedFeature[] = [];

  for (const feature of features) {
    let rings: Rings;
    try {
      rings = ringsOf(feature.geometry);
    } catch {
      continue;
    }
    if (rings.rings.length === 0) continue;

    indexed.push({
      featureId: featureId(feature),
      rings: rings.rings,
      closed: rings.closed,
    });
  }
  return indexed;
}

/**
 * The feature ids under a click, nearest first.
 *
 * MapLibre returns them in render order — topmost first — which is the order a
 * user means: clicking where two leases overlap selects the one they can see.
 * Duplicates are collapsed because a multipolygon comes back once per part.
 */
export function hitFeatureIds(features: readonly QueriedFeature[]): string[] {
  const seen = new Set<string>();
  for (const feature of features) {
    if (feature.id === undefined || feature.id === '') continue;
    seen.add(String(feature.id));
  }
  return [...seen];
}

/**
 * The box to query for snap candidates: the pointer grown by `tolerancePx`.
 *
 * `09` §6.1 — one query over the box rather than a test per segment in view.
 * The tolerance passed should be the **larger** of the vertex and edge
 * tolerances, or an edge candidate just outside the vertex radius is never
 * returned and the edge pass silently has nothing to work with.
 */
export function queryBox(
  pointer: Pixel,
  tolerancePx: number,
): [[number, number], [number, number]] {
  return [
    [pointer.x - tolerancePx, pointer.y - tolerancePx],
    [pointer.x + tolerancePx, pointer.y + tolerancePx],
  ];
}

/**
 * The session's dirty features as GeoJSON for the edit overlay.
 *
 * Deleted features — `null` in the buffer — are **omitted from the overlay and
 * still named in `hideFromLayers`'s exclusion**, which is how a pending delete
 * looks like a delete: the tile copy is filtered out and nothing is drawn in
 * its place. Omitting the id instead would leave the feature on screen until
 * the save landed.
 */
export function overlayFeatures(
  dirty: ReadonlyMap<string, Feature | null>,
): { features: GeoJsonFeature[]; hiddenIds: string[] } {
  const features: GeoJsonFeature[] = [];
  const hiddenIds: string[] = [];

  for (const [id, feature] of dirty) {
    hiddenIds.push(id);
    if (feature === null) continue;
    features.push({
      type: 'Feature',
      id,
      geometry: feature.geometry as Geometry,
      properties: feature.properties,
    });
  }
  return { features, hiddenIds };
}
