/**
 * Moving, adding and removing a vertex. `09-editing.md` §11.6.
 *
 * The three operations a vertex drag is made of, as pure functions from one
 * geometry to another. They are here rather than in the drag handler because
 * each carries a rule that is easy to get wrong, silent when wrong, and
 * expensive later:
 *
 * **A closed ring's first and last coordinates are one vertex.** Move one
 * without the other and the polygon opens — a defect no renderer complains
 * about and every area calculation is then quietly wrong.
 *
 * **Z survives.** A breakline is digitised in three dimensions and the
 * interpolation worker reads its elevation from the third ordinate; the editor
 * addresses only x and y. So a moved vertex keeps the Z it had, and a *new*
 * vertex takes the linear interpolation of its two neighbours' — which is the
 * elevation the segment already implied at that point, so adding a vertex to a
 * breakline changes where it can be dragged and nothing about the surface.
 *
 * **A ring has a minimum.** Three distinct vertices for a polygon ring, two for
 * a line (§11.6). Below that the geometry is not a smaller shape, it is not a
 * shape — and the delete is refused rather than silently dropping the ring.
 *
 * Multi-part geometries are addressed by a **flattened** ring index, the same
 * one `mapBridge.ts` produces, and rebuilt into their original nesting on the
 * way out. That is what lets the snap engine, the coincidence index and these
 * three functions share one way of naming a vertex.
 */

import type { Geometry, Position } from 'geojson';

/** A geometry's rings, flattened, with the shape needed to rebuild it. */
interface Flattened {
  rings: Position[][];
  closed: boolean;
  /** Rings per part, for a multipolygon. Empty for everything else. */
  partSizes: number[];
}

function flatten(geometry: Geometry): Flattened {
  switch (geometry.type) {
    case 'Point':
      return { rings: [[geometry.coordinates]], closed: false, partSizes: [] };
    case 'MultiPoint':
      return {
        rings: geometry.coordinates.map((position) => [position]),
        closed: false,
        partSizes: [],
      };
    case 'LineString':
      return { rings: [geometry.coordinates], closed: false, partSizes: [] };
    case 'MultiLineString':
      return { rings: geometry.coordinates, closed: false, partSizes: [] };
    case 'Polygon':
      return { rings: geometry.coordinates, closed: true, partSizes: [] };
    case 'MultiPolygon':
      return {
        rings: geometry.coordinates.flat(),
        closed: true,
        partSizes: geometry.coordinates.map((part) => part.length),
      };
    case 'GeometryCollection':
      throw new Error(
        'A GeometryCollection has no single vertex addressing — its members ' +
          'can be closed and open at once. Explode it before editing vertices.',
      );
  }
}

function rebuild(geometry: Geometry, rings: Position[][]): Geometry {
  switch (geometry.type) {
    case 'Point':
      return { type: 'Point', coordinates: rings[0]![0]! };
    case 'MultiPoint':
      return { type: 'MultiPoint', coordinates: rings.map((ring) => ring[0]!) };
    case 'LineString':
      return { type: 'LineString', coordinates: rings[0]! };
    case 'MultiLineString':
      return { type: 'MultiLineString', coordinates: rings };
    case 'Polygon':
      return { type: 'Polygon', coordinates: rings };
    case 'MultiPolygon': {
      const parts: Position[][][] = [];
      let index = 0;
      for (const size of geometry.coordinates.map((part) => part.length)) {
        parts.push(rings.slice(index, index + size));
        index += size;
      }
      return { type: 'MultiPolygon', coordinates: parts };
    }
    case 'GeometryCollection':
      throw new Error('A GeometryCollection cannot be rebuilt from flat rings.');
  }
}

function ringAt(flattened: Flattened, ring: number, operation: string): Position[] {
  const coordinates = flattened.rings[ring];
  if (!coordinates) {
    throw new Error(
      `Cannot ${operation}: ring ${ring} does not exist — this geometry has ` +
        `${flattened.rings.length}. A ring index from a stale selection is the ` +
        `usual cause; reselect the feature.`,
    );
  }
  return coordinates;
}

/** How many coordinates a ring holds that are not the repeat of another. */
function distinctCount(ring: Position[], closed: boolean): number {
  return closed ? ring.length - 1 : ring.length;
}

/** A position at x, y, keeping every ordinate beyond the second. */
function withXy(original: Position | undefined, x: number, y: number): Position {
  return original && original.length > 2 ? [x, y, ...original.slice(2)] : [x, y];
}

/**
 * Move one vertex.
 *
 * On a closed ring, moving the first vertex moves the closing coordinate with
 * it — they are the same vertex, drawn twice.
 */
export function setVertex(
  geometry: Geometry,
  ring: number,
  ordinal: number,
  position: [number, number],
): Geometry {
  const flattened = flatten(geometry);
  const coordinates = ringAt(flattened, ring, 'move a vertex');
  const distinct = distinctCount(coordinates, flattened.closed);

  if (ordinal < 0 || ordinal >= distinct) {
    throw new Error(
      `Cannot move vertex ${ordinal}: ring ${ring} has ${distinct} vertices. ` +
        `On a closed ring the repeated closing coordinate is not addressable — ` +
        `it moves with vertex 0.`,
    );
  }

  const next = [...coordinates];
  next[ordinal] = withXy(coordinates[ordinal], position[0], position[1]);
  if (flattened.closed && ordinal === 0) {
    // The closing coordinate keeps its own Z, which for a well-formed ring is
    // the Z of vertex 0 anyway.
    next[coordinates.length - 1] = withXy(
      coordinates[coordinates.length - 1],
      position[0],
      position[1],
    );
  }

  return rebuild(geometry, replaceRing(flattened.rings, ring, next));
}

/**
 * Add a vertex on the segment starting at `segmentIndex`.
 *
 * Its Z is the linear interpolation of the segment's endpoints, weighted by
 * how far along the new vertex lies. That is the elevation the segment already
 * implied there, so the surface is unchanged and only the editing handles
 * differ. With no Z on the endpoints, none is invented.
 */
export function insertVertex(
  geometry: Geometry,
  ring: number,
  segmentIndex: number,
  position: [number, number],
): Geometry {
  const flattened = flatten(geometry);
  const coordinates = ringAt(flattened, ring, 'add a vertex');

  if (segmentIndex < 0 || segmentIndex >= coordinates.length - 1) {
    throw new Error(
      `Cannot add a vertex on segment ${segmentIndex}: ring ${ring} has ` +
        `${Math.max(coordinates.length - 1, 0)} segments.`,
    );
  }

  const start = coordinates[segmentIndex]!;
  const end = coordinates[segmentIndex + 1]!;
  const inserted = interpolateZ(start, end, position);

  const next = [...coordinates];
  next.splice(segmentIndex + 1, 0, inserted);
  return rebuild(geometry, replaceRing(flattened.rings, ring, next));
}

function interpolateZ(start: Position, end: Position, position: [number, number]): Position {
  const startZ = start[2];
  const endZ = end[2];
  if (startZ === undefined || endZ === undefined) return [position[0], position[1]];

  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  const lengthSquared = dx * dx + dy * dy;
  // A zero-length segment has no "along": both endpoints are the same place,
  // so either Z is as good as the other and the first is the one that exists.
  const along =
    lengthSquared === 0
      ? 0
      : ((position[0] - start[0]) * dx + (position[1] - start[1]) * dy) / lengthSquared;

  return [position[0], position[1], startZ + (endZ - startZ) * Math.min(Math.max(along, 0), 1)];
}

/** The fewest distinct vertices a ring of this geometry may have. §11.6. */
export function minimumRingSize(geometry: Geometry): number {
  switch (geometry.type) {
    case 'Polygon':
    case 'MultiPolygon':
      return 3;
    case 'LineString':
    case 'MultiLineString':
      return 2;
    default:
      return 1;
  }
}

/**
 * Remove one vertex, or refuse.
 *
 * Refused when the ring would fall below its minimum: below three a polygon
 * ring is not a smaller polygon, and below two a line is not a shorter line.
 * §11.6 makes this a rejection rather than a cascade into deleting the ring,
 * because a user pressing Delete on a vertex did not ask to delete a feature.
 */
export function removeVertex(geometry: Geometry, ring: number, ordinal: number): Geometry {
  const flattened = flatten(geometry);
  const coordinates = ringAt(flattened, ring, 'delete a vertex');
  const distinct = distinctCount(coordinates, flattened.closed);
  const minimum = minimumRingSize(geometry);

  if (ordinal < 0 || ordinal >= distinct) {
    throw new Error(
      `Cannot delete vertex ${ordinal}: ring ${ring} has ${distinct} vertices.`,
    );
  }
  if (distinct <= minimum) {
    throw new Error(
      `Deleting this vertex would leave ${distinct - 1} in the ring, and a ` +
        `${geometry.type} needs at least ${minimum}. Delete the whole feature ` +
        `if that is what you meant.`,
    );
  }

  const next = [...coordinates];
  next.splice(ordinal, 1);
  if (flattened.closed && ordinal === 0) {
    // The ring now starts at what was vertex 1; its closing coordinate has to
    // follow, or the ring is open.
    next[next.length - 1] = withXy(next[next.length - 1], next[0]![0], next[0]![1]);
  }

  return rebuild(geometry, replaceRing(flattened.rings, ring, next));
}

function replaceRing(rings: Position[][], index: number, replacement: Position[]): Position[][] {
  const next = [...rings];
  next[index] = replacement;
  return next;
}
