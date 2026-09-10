/**
 * Topological editing: the coincidence index and propagation.
 * `09-editing.md` §7, `adr/0013`.
 *
 * A toolbar toggle, **off by default**. When on, an edit to a vertex moves
 * every vertex that sits exactly on it — which is what makes editing adjacent
 * leases or adjacent units bearable, and what stops a boundary drag opening a
 * sliver behind it.
 *
 * Three decisions carry this file, and each is a way of not being wrong:
 *
 * **The tolerance is 0.01 ft, three orders of magnitude below the snap
 * tolerance, and the two must never be confused.** Snap tolerance is a *UI
 * affordance* measured in pixels; topological coincidence is a *property of the
 * data* and has to be near-exact. Reusing the snap tolerance makes a vertex
 * drag move an unrelated vertex twelve feet away, which is the kind of bug
 * reported as "the editor corrupted my layer".
 *
 * **Exact coincidence needs no spatial index.** A coordinate hash is O(1) per
 * drag, built when the session opens and rebuilt on `moveend` and after each
 * save. `09` §7.4 also points out that this index is the gap detector for
 * §12.2, so it earns its keep twice.
 *
 * **Vertex add propagates, and skipping it is the classic half-implementation.**
 * Inserting a vertex on a shared edge in only one polygon does not create a gap
 * immediately — it guarantees one on the next drag, and the gap is invisible
 * until somebody runs Validate a week later.
 */

/** A coordinate, in the layer's own units. */
export type Position = readonly [number, number];

export interface VertexRef {
  featureId: string;
  ring: number;
  ordinal: number;
  /** Distinct vertices in this ring — the closing duplicate excluded.
   *
   *  Stored because adjacency wraps: a closed ring's last segment runs from
   *  ordinal N-1 back to ordinal 0, so its endpoints are N-1 apart rather than
   *  1. Without this, `sharedEdges` never recognises that segment, and on a
   *  neighbour whose shared boundary happens to be its closing edge a vertex
   *  insert goes into one polygon only — the sliver §7.3 exists to prevent. */
  ringLength: number;
  /** Whether that ring is closed, which is what makes the wrap legitimate. */
  closed: boolean;
}

/**
 * Coincidence tolerance, in **feet**, and deliberately tiny.
 *
 * Expressed as a decimal-place count for the hash key rather than as a
 * distance, because a hash is an equality test and a distance is not: two
 * coordinates 0.009 ft apart hash to the same key, and one 0.011 ft away does
 * not. Seven decimal places of a degree is about 1 cm — under 0.05 ft — which
 * is the precision `09` §6.6 already uses for exact-coordinate matching.
 */
export const KEY_PRECISION = 7;

/** `09` §7.2's default, for the settings UI and for a projected-units layer. */
export const DEFAULT_TOLERANCE_FT = 0.01;

export type CoincidenceIndex = Map<string, VertexRef[]>;

export function key(position: Position): string {
  return `${position[0].toFixed(KEY_PRECISION)},${position[1].toFixed(KEY_PRECISION)}`;
}

/** One feature's rings, as the index reads them. */
export interface IndexedFeature {
  featureId: string;
  rings: Position[][];
  /** A closed ring repeats its first coordinate at the end. That duplicate is
   *  not a separate vertex and must not be indexed, or every ring reports a
   *  coincidence with itself. */
  closed?: boolean;
}

/**
 * Build the coordinate hash over the active layer's features in view.
 *
 * **The active layer only.** `09` §7.1: cross-layer propagation would mean
 * editing a layer the user did not make active, breaking §3.1's single-layer
 * invariant. Cross-layer coincidence is Align's job.
 */
export function buildIndex(features: Iterable<IndexedFeature>): CoincidenceIndex {
  const index: CoincidenceIndex = new Map();

  for (const feature of features) {
    for (const [ring, coordinates] of feature.rings.entries()) {
      const count = feature.closed ? coordinates.length - 1 : coordinates.length;
      for (let ordinal = 0; ordinal < count; ordinal += 1) {
        const position = coordinates[ordinal];
        if (!position) continue;
        const bucket = index.get(key(position));
        const ref: VertexRef = {
          featureId: feature.featureId,
          ring,
          ordinal,
          ringLength: count,
          closed: feature.closed ?? false,
        };
        if (bucket) bucket.push(ref);
        else index.set(key(position), [ref]);
      }
    }
  }
  return index;
}

/**
 * Every vertex coincident with `position`, **including the one being dragged**.
 *
 * Including it on purpose: a caller propagating a move applies the same new
 * position to every ref, and special-casing the origin means writing the same
 * move twice in two ways. The caller that wants the others filters by ref.
 */
export function coincident(index: CoincidenceIndex, position: Position): VertexRef[] {
  return index.get(key(position)) ?? [];
}

/**
 * Refs other than the one given. What a propagating move actually needs.
 */
export function others(refs: readonly VertexRef[], origin: VertexRef): VertexRef[] {
  return refs.filter(
    (ref) =>
      ref.featureId !== origin.featureId ||
      ref.ring !== origin.ring ||
      ref.ordinal !== origin.ordinal,
  );
}

/**
 * Which operations honour the toggle. `09` §7.3.
 *
 * **The two `false` rows are explicit non-topological operations**, not
 * oversights, and their tooltips say so. Moving a whole feature is ambiguous —
 * does the neighbour stretch to follow, or translate with it? — and the same
 * ambiguity applies to rotate and scale. A tool that guessed would be wrong
 * half the time on data nobody can check by eye.
 */
export const PROPAGATES: Record<string, boolean> = {
  'vertex.move': true,
  'vertex.delete': true,
  // Skipping this is the classic half-implementation: it does not open a gap
  // immediately, it guarantees one on the next drag.
  'vertex.add': true,
  'edit.split': true,
  'edit.reshape': true,
  'transform.move': false,
  'transform.rotate': false,
  'transform.scale': false,
};

export function propagates(operationId: string, enabled: boolean): boolean {
  return enabled && PROPAGATES[operationId] === true;
}

/** Why an operation does not propagate, for its tooltip. */
export function nonTopologicalReason(operationId: string): string | null {
  if (PROPAGATES[operationId] !== false) return null;
  return (
    'This does not move neighbouring features even with topological editing on: ' +
    'whether a neighbour should stretch to follow or move with it is ambiguous, ' +
    'and guessing would be wrong half the time on data nobody can check by eye.'
  );
}

/**
 * The edge shared between two features, if they share one.
 *
 * Used by vertex-add: inserting a vertex on a shared edge has to insert it in
 * **every** feature that shares that edge, and "shares an edge" means both
 * endpoints coincide — not one. Two polygons meeting at a single corner share a
 * vertex and no edge, and inserting into both would move a boundary that is not
 * there.
 *
 * Returns the segment index in each feature, because the insertion point is a
 * position within a ring rather than a coordinate.
 */
export function sharedEdges(
  index: CoincidenceIndex,
  a: Position,
  b: Position,
): Array<{ featureId: string; ring: number; afterOrdinal: number }> {
  const atA = coincident(index, a);
  const atB = coincident(index, b);
  const shared: Array<{ featureId: string; ring: number; afterOrdinal: number }> = [];

  for (const first of atA) {
    for (const second of atB) {
      if (first.featureId !== second.featureId || first.ring !== second.ring) continue;
      // Adjacent ordinals in either direction: a neighbour may wind the other
      // way round, which is ordinary for two polygons sharing a boundary.
      const gap = Math.abs(first.ordinal - second.ordinal);
      const wraps = first.closed && gap === first.ringLength - 1;
      if (gap !== 1 && !wraps) continue;
      shared.push({
        featureId: first.featureId,
        ring: first.ring,
        // On the wrap the segment runs from the last vertex to the first, so
        // the insertion point is after the last — not after ordinal 0, which
        // would put the new vertex on the ring's *opening* segment instead.
        afterOrdinal: wraps ? first.ringLength - 1 : Math.min(first.ordinal, second.ordinal),
      });
    }
  }
  return shared;
}
