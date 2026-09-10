/**
 * The snapping engine. `09-editing.md` §6.
 *
 * "The feature that decides whether the editor is usable. Without it every
 * shared boundary is a source of slivers."
 *
 * **All snap math happens in pixels.** That avoids geodesic distance entirely
 * and makes tolerance handling trivial. An earlier revision of §6.1 converted a
 * ground tolerance to degrees per operation at the current latitude; it works,
 * and it puts a trigonometric conversion in the inner loop of a 60 Hz path to
 * reach the same answer.
 *
 * This module is the **pure half**: it takes candidates already projected to
 * screen pixels and returns the winner. Projection, `queryRenderedFeatures` and
 * `unproject` are the caller's, which is what lets every rule below be tested
 * without a map — and the rules are where the bugs are.
 *
 * Three of them are easy to get subtly wrong and each has a test:
 *
 * - **Vertex always beats edge**, and that falls out of checking vertices
 *   first. Comparing the two distances against each other is the version that
 *   snaps to an edge 2 px away instead of the vertex 3 px away, which is never
 *   what anybody wants.
 * - **The dragged vertex and its two ring neighbours are excluded**, or the
 *   drag handle sticks to itself.
 * - **Squared distances throughout.** No `Math.sqrt` in a loop that runs over
 *   every segment in view, every frame.
 */

export type SnapType = 'vertex' | 'edge' | 'intersection' | 'midpoint';

/** A point in screen pixels. */
export interface Pixel {
  x: number;
  y: number;
}

/** Where a vertex sits in its feature. Ring and ordinal, never a flat index —
 *  §6.6: simplification drops vertices, so index *i* in the tile is not index
 *  *i* in the source. */
export interface VertexRef {
  ring: number;
  ordinal: number;
}

/** One candidate feature's projected geometry, as the caller supplies it. */
export interface SnapCandidate {
  featureId: string;
  layerId: string;
  /** Rings of projected coordinates. A line is one ring; a polygon is an outer
   *  ring plus holes; a point is a ring of one. */
  rings: Pixel[][];
  /** True when these pixels came from exact geometry rather than from a tile.
   *  Drives `isExact` on the result (§6.6). */
  exact?: boolean;
}

export interface SnapResult {
  pixel: Pixel;
  type: SnapType;
  featureId: string;
  layerId: string;
  vertexRef?: VertexRef;
  segmentIndex?: number;
  /** False while still tile-derived. The indicator renders hollow until this
   *  is true and filled after — useful in development, harmless in production. */
  isExact: boolean;
}

export interface SnapConfig {
  vertex: boolean;
  edge: boolean;
  intersection: boolean;
  midpoint: boolean;
  vertexPx: number;
  edgePx: number;
}

/** Vertices excluded from snapping — the one being dragged and its neighbours. */
export interface SnapExclusion {
  featureId: string;
  refs: VertexRef[];
}

/** `09` §6.3's pixel floor and ceiling. */
export const MIN_PX = 4;
export const MAX_PX = 20;

const FEET_TO_METRES = 0.3048;

/**
 * Ground tolerance in feet to a pixel radius, clamped.
 *
 * **The clamp is not a nicety.** A fixed ground tolerance degrades badly across
 * zoom: 10 ft is roughly 6 px at z18 and well under 1 px at z15, at which point
 * snapping silently stops working. Below `MIN_PX` the floor takes over —
 * and `clamped` says so, because a silent behaviour change generates bug
 * reports and a badge generates none (§6.3).
 */
export function pixelTolerance(
  feet: number,
  latitude: number,
  zoom: number,
): { px: number; clamped: 'floor' | 'ceiling' | null } {
  const metresPerPixel =
    (156543.03392 * Math.cos((latitude * Math.PI) / 180)) / Math.pow(2, zoom);
  const raw = (feet * FEET_TO_METRES) / metresPerPixel;

  if (raw < MIN_PX) return { px: MIN_PX, clamped: 'floor' };
  if (raw > MAX_PX) return { px: MAX_PX, clamped: 'ceiling' };
  return { px: raw, clamped: null };
}

function squaredDistance(a: Pixel, b: Pixel): number {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return dx * dx + dy * dy;
}

function excluded(
  exclusion: SnapExclusion | undefined,
  featureId: string,
  ring: number,
  ordinal: number,
): boolean {
  if (!exclusion || exclusion.featureId !== featureId) return false;
  return exclusion.refs.some((ref) => ref.ring === ring && ref.ordinal === ordinal);
}

/**
 * The clamped perpendicular foot of `point` on the segment `a`–`b`.
 *
 * Clamped, so a pointer past the end of a segment lands on its endpoint rather
 * than on the infinite line — which for a fault trace would put a snap
 * kilometres off the end of the fault.
 */
export function perpendicularFoot(point: Pixel, a: Pixel, b: Pixel): Pixel {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) return a;

  const t = Math.max(
    0,
    Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / lengthSquared),
  );
  return { x: a.x + t * dx, y: a.y + t * dy };
}

/**
 * The best snap for a pointer position, or null.
 *
 * **Passes run in priority order and the first hit wins**: vertex →
 * intersection → midpoint → edge (§6.2). An intersection is a more meaningful
 * place to land than an arbitrary point on an edge, and a geologist digitising
 * a fault network depends on it.
 */
export function snap(
  pointer: Pixel,
  candidates: readonly SnapCandidate[],
  config: SnapConfig,
  exclusion?: SnapExclusion,
): SnapResult | null {
  if (config.vertex) {
    const hit = vertexPass(pointer, candidates, config.vertexPx, exclusion);
    if (hit) return hit;
  }
  if (config.intersection) {
    const hit = intersectionPass(pointer, candidates, config.edgePx);
    if (hit) return hit;
  }
  if (config.midpoint) {
    const hit = midpointPass(pointer, candidates, config.vertexPx, exclusion);
    if (hit) return hit;
  }
  if (config.edge) {
    const hit = edgePass(pointer, candidates, config.edgePx, exclusion);
    if (hit) return hit;
  }
  return null;
}

function vertexPass(
  pointer: Pixel,
  candidates: readonly SnapCandidate[],
  tolerancePx: number,
  exclusion?: SnapExclusion,
): SnapResult | null {
  const limit = tolerancePx * tolerancePx;
  let best: SnapResult | null = null;
  let bestDistance = limit;

  for (const candidate of candidates) {
    for (const [ring, coordinates] of candidate.rings.entries()) {
      for (const [ordinal, vertex] of coordinates.entries()) {
        if (excluded(exclusion, candidate.featureId, ring, ordinal)) continue;
        const distance = squaredDistance(pointer, vertex);
        // Strictly less: with two candidates at the same distance the first
        // wins, which makes the result depend on candidate order rather than
        // on nothing at all.
        if (distance < bestDistance) {
          bestDistance = distance;
          best = {
            pixel: vertex,
            type: 'vertex',
            featureId: candidate.featureId,
            layerId: candidate.layerId,
            vertexRef: { ring, ordinal },
            isExact: candidate.exact ?? false,
          };
        }
      }
    }
  }
  return best;
}

function midpointPass(
  pointer: Pixel,
  candidates: readonly SnapCandidate[],
  tolerancePx: number,
  exclusion?: SnapExclusion,
): SnapResult | null {
  const limit = tolerancePx * tolerancePx;
  let best: SnapResult | null = null;
  let bestDistance = limit;

  for (const candidate of candidates) {
    for (const [ring, coordinates] of candidate.rings.entries()) {
      for (let index = 0; index + 1 < coordinates.length; index += 1) {
        const a = coordinates[index]!;
        const b = coordinates[index + 1]!;
        if (
          excluded(exclusion, candidate.featureId, ring, index) ||
          excluded(exclusion, candidate.featureId, ring, index + 1)
        ) {
          continue;
        }
        const middle = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        const distance = squaredDistance(pointer, middle);
        if (distance < bestDistance) {
          bestDistance = distance;
          best = {
            pixel: middle,
            type: 'midpoint',
            featureId: candidate.featureId,
            layerId: candidate.layerId,
            segmentIndex: index,
            isExact: candidate.exact ?? false,
          };
        }
      }
    }
  }
  return best;
}

function edgePass(
  pointer: Pixel,
  candidates: readonly SnapCandidate[],
  tolerancePx: number,
  exclusion?: SnapExclusion,
): SnapResult | null {
  const limit = tolerancePx * tolerancePx;
  let best: SnapResult | null = null;
  let bestDistance = limit;

  for (const candidate of candidates) {
    for (const [ring, coordinates] of candidate.rings.entries()) {
      for (let index = 0; index + 1 < coordinates.length; index += 1) {
        const a = coordinates[index]!;
        const b = coordinates[index + 1]!;
        if (
          excluded(exclusion, candidate.featureId, ring, index) &&
          excluded(exclusion, candidate.featureId, ring, index + 1)
        ) {
          continue;
        }
        // A cheap bbox rejection before the perpendicular math (§6.1). The
        // segment's box, grown by the tolerance: if the pointer is outside it,
        // no point on the segment can be within tolerance.
        if (
          pointer.x < Math.min(a.x, b.x) - tolerancePx ||
          pointer.x > Math.max(a.x, b.x) + tolerancePx ||
          pointer.y < Math.min(a.y, b.y) - tolerancePx ||
          pointer.y > Math.max(a.y, b.y) + tolerancePx
        ) {
          continue;
        }

        const foot = perpendicularFoot(pointer, a, b);
        const distance = squaredDistance(pointer, foot);
        if (distance < bestDistance) {
          bestDistance = distance;
          best = {
            pixel: foot,
            type: 'edge',
            featureId: candidate.featureId,
            layerId: candidate.layerId,
            segmentIndex: index,
            isExact: candidate.exact ?? false,
          };
        }
      }
    }
  }
  return best;
}

/**
 * Where two candidates' segments cross.
 *
 * Computed rather than looked up: an intersection is not a vertex of either
 * feature, which is exactly why snapping to it matters — a geologist tying a
 * fault into another fault needs the crossing, and neither trace has a vertex
 * there until they make one.
 *
 * Only *between* features, never within one. A self-intersection is a defect
 * the validator reports (§12), and offering to snap to it would be offering to
 * build on it.
 */
function intersectionPass(
  pointer: Pixel,
  candidates: readonly SnapCandidate[],
  tolerancePx: number,
): SnapResult | null {
  const limit = tolerancePx * tolerancePx;
  let best: SnapResult | null = null;
  let bestDistance = limit;

  for (let i = 0; i < candidates.length; i += 1) {
    for (let j = i + 1; j < candidates.length; j += 1) {
      const left = candidates[i]!;
      const right = candidates[j]!;
      for (const leftRing of left.rings) {
        for (const rightRing of right.rings) {
          for (let a = 0; a + 1 < leftRing.length; a += 1) {
            for (let b = 0; b + 1 < rightRing.length; b += 1) {
              const crossing = segmentIntersection(
                leftRing[a]!,
                leftRing[a + 1]!,
                rightRing[b]!,
                rightRing[b + 1]!,
              );
              if (!crossing) continue;
              const distance = squaredDistance(pointer, crossing);
              if (distance < bestDistance) {
                bestDistance = distance;
                best = {
                  pixel: crossing,
                  type: 'intersection',
                  featureId: left.featureId,
                  layerId: left.layerId,
                  segmentIndex: a,
                  isExact: (left.exact ?? false) && (right.exact ?? false),
                };
              }
            }
          }
        }
      }
    }
  }
  return best;
}

/** Where two segments cross, or null. Parallel and collinear both give null:
 *  collinear overlap has no single crossing point to snap to. */
export function segmentIntersection(
  p1: Pixel,
  p2: Pixel,
  p3: Pixel,
  p4: Pixel,
): Pixel | null {
  const d1x = p2.x - p1.x;
  const d1y = p2.y - p1.y;
  const d2x = p4.x - p3.x;
  const d2y = p4.y - p3.y;

  const denominator = d1x * d2y - d1y * d2x;
  if (denominator === 0) return null;

  const t = ((p3.x - p1.x) * d2y - (p3.y - p1.y) * d2x) / denominator;
  const u = ((p3.x - p1.x) * d1y - (p3.y - p1.y) * d1x) / denominator;

  if (t < 0 || t > 1 || u < 0 || u > 1) return null;
  return { x: p1.x + t * d1x, y: p1.y + t * d1y };
}

/**
 * The vertices to exclude while dragging one: the vertex itself **and its two
 * ring neighbours** (§6.4).
 *
 * Without the neighbours the handle sticks to the segment it is attached to,
 * because the perpendicular foot of a point on its own segment is the point
 * itself. Without the vertex, it sticks to where it started.
 *
 * Ring-aware: on a closed ring the neighbour of the first vertex is the last
 * real one, not a wrap onto the duplicated closing coordinate.
 */
export function dragExclusion(
  featureId: string,
  ring: number,
  ordinal: number,
  ringLength: number,
  closed: boolean,
): SnapExclusion {
  // A closed ring repeats its first coordinate at the end, so the count of
  // distinct vertices is one less and the wrap has to skip the duplicate.
  const distinct = closed ? ringLength - 1 : ringLength;
  const refs: VertexRef[] = [{ ring, ordinal }];

  const before = ordinal - 1;
  const after = ordinal + 1;

  if (before >= 0) refs.push({ ring, ordinal: before });
  else if (closed) refs.push({ ring, ordinal: distinct - 1 });

  if (after < distinct) refs.push({ ring, ordinal: after });
  else if (closed) refs.push({ ring, ordinal: 0 });

  return { featureId, refs };
}

/** The defaults `09` §6.2 gives a layer. */
export function defaultConfig(vertexPx: number, edgePx: number): SnapConfig {
  return {
    vertex: true,
    edge: true,
    // Off by default because both cost a pass over every pair of candidates,
    // and most editing does not need them. A fault-network session turns them
    // on, which is when the cost is worth paying.
    intersection: false,
    midpoint: false,
    vertexPx,
    edgePx,
  };
}
