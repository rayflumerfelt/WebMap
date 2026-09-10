/**
 * Vertex operations as commands. `09-editing.md` §3.4, §11.6.
 *
 * `geometryEdits.ts` changes a geometry; `session.ts` records a change. This
 * module is the join: it reads the feature as it currently stands, applies the
 * geometry operation, and returns the `Command` — `before` and `after` for
 * every feature touched — that the undo stack keeps.
 *
 * **`before` is read through `current`, never from the exact cache.** A second
 * edit to a feature must record the state the first edit left, or undoing it
 * silently reverts both.
 *
 * **A multi-vertex delete removes descending ordinals first.** Removing vertex
 * 1 renumbers vertex 2, so ascending order deletes the wrong vertices from the
 * second onward — the sort is the entire content of that function and the
 * reason it is not written inline at the call site.
 */

import type { Geometry } from 'geojson';

import { insertVertex, removeVertex, setVertex } from './geometryEdits.js';
import { ringsOf } from './mapBridge.js';
import type { SelectedVertex } from './overlay.js';
import { current } from './session.js';
import type { Command, EditSession, Feature, FeatureDelta } from './session.js';
import { buildIndex, coincident, others, propagates } from './topology.js';
import type { Position, VertexRef } from './topology.js';

/** Ids exist to key the undo stack, so uniqueness is all they need. */
function commandId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `command-${Date.now()}-${Math.random()}`;
}

function required(session: EditSession, featureId: string, operation: string): Feature {
  const feature = current(session, featureId);
  if (!feature) {
    throw new Error(
      `Cannot ${operation}: feature '${featureId}' is not in this edit session — ` +
        `it was deleted, or the working set was rebuilt by a pan. Reselect it.`,
    );
  }
  return feature;
}

function delta(before: Feature, geometry: Geometry): FeatureDelta {
  return { featureId: before.id, before, after: { ...before, geometry } };
}

/**
 * Move one vertex to a coordinate. The command a drag commits.
 *
 * With `topological` on, every vertex of the active layer coincident with the
 * one being moved moves with it (`09` §7, `adr/0013`). That is the difference
 * between editing a lease boundary and editing one side of it: without it the
 * neighbour keeps the old line and the gap between them is invisible until
 * somebody runs Validate a week later.
 *
 * **Coincidence is an equality test at 1 cm, not the snap tolerance** (§7.2).
 * Reusing the snap radius here is the mistake that moves an unrelated vertex
 * twelve feet away, so the index is built from the coordinate hash and nothing
 * else.
 */
export function moveVertexCommand(
  session: EditSession,
  vertex: SelectedVertex,
  position: [number, number],
  options: { topological?: boolean } = {},
): Command {
  const before = required(session, vertex.featureId, 'move a vertex');
  const deltas = [
    delta(before, setVertex(before.geometry as Geometry, vertex.ring, vertex.ordinal, position)),
  ];

  if (propagates('vertex.move', options.topological ?? false)) {
    const origin = positionOf(before, vertex);
    if (origin) {
      for (const ref of others(coincident(indexOf(session), origin), toRef(vertex, before))) {
        // A feature that cannot be read or moved is skipped rather than
        // failing the drag: propagation is an addition to the edit the user
        // asked for, and losing the whole move because a neighbour is
        // malformed would be the worse trade.
        const neighbour = current(session, ref.featureId);
        if (!neighbour) continue;
        try {
          deltas.push(
            delta(
              neighbour,
              setVertex(neighbour.geometry as Geometry, ref.ring, ref.ordinal, position),
            ),
          );
        } catch {
          continue;
        }
      }
    }
  }

  return {
    id: commandId(),
    label: deltas.length === 1 ? 'Move Vertex' : `Move Vertex (${deltas.length} features)`,
    deltas,
    timestamp: Date.now(),
  };
}

/** The coordinate a vertex currently sits at, in layer units. */
function positionOf(feature: Feature, vertex: SelectedVertex): Position | null {
  try {
    return ringsOf(feature.geometry as Geometry).rings[vertex.ring]?.[vertex.ordinal] ?? null;
  } catch {
    return null;
  }
}

function toRef(vertex: SelectedVertex, feature: Feature): VertexRef {
  let ringLength = 0;
  let closed = false;
  try {
    const rings = ringsOf(feature.geometry as Geometry);
    closed = rings.closed;
    const ring = rings.rings[vertex.ring];
    ringLength = ring ? (closed ? ring.length - 1 : ring.length) : 0;
  } catch {
    // Left at zero: `others` compares ids, rings and ordinals only, so a
    // ring length it could not determine changes nothing about the filter.
  }
  return { ...vertex, ringLength, closed };
}

/**
 * The coincidence index over the session's **current** features.
 *
 * Built per command rather than cached, and that is a deliberate trade: a
 * cache would have to be invalidated on every edit, every working-set arrival
 * and every undo, and a stale coincidence index propagates a move onto a
 * vertex that is no longer there. The working set is capped at 5,000 features
 * (§17), which is the bound that makes rebuilding affordable.
 */
function indexOf(session: EditSession) {
  const features: Array<{ featureId: string; rings: Position[][]; closed: boolean }> = [];
  const seen = new Set<string>();

  const add = (feature: Feature | null, id: string) => {
    if (!feature || seen.has(id)) return;
    seen.add(id);
    try {
      const rings = ringsOf(feature.geometry as Geometry);
      features.push({ featureId: id, rings: rings.rings, closed: rings.closed });
    } catch {
      // Not indexable, not a propagation target. The edit itself still works.
    }
  };

  // Dirty first, so an edited feature is indexed at where it is now rather
  // than where the session found it.
  for (const [id] of session.dirty) add(current(session, id), id);
  for (const [id] of session.exactCache) add(current(session, id), id);
  return buildIndex(features);
}

/** Add a vertex on a segment. Vertex-add mode's click. */
export function addVertexCommand(
  session: EditSession,
  featureId: string,
  ring: number,
  segmentIndex: number,
  position: [number, number],
): Command {
  const before = required(session, featureId, 'add a vertex');
  const geometry = insertVertex(before.geometry as Geometry, ring, segmentIndex, position);

  return {
    id: commandId(),
    label: 'Add Vertex',
    deltas: [delta(before, geometry)],
    timestamp: Date.now(),
  };
}

/**
 * Delete every selected vertex, across however many features.
 *
 * Descending by ordinal within each ring: removing vertex 1 renumbers vertex 2,
 * so an ascending pass deletes the wrong vertex from the second onward. And one
 * delta per feature rather than one per vertex, because §5.1 makes an operation
 * atomic — an undo must not leave half the vertices deleted.
 *
 * A ring that would fall below its minimum throws from `removeVertex`, and the
 * whole command fails rather than deleting what it could: a partial delete is
 * harder to reason about than a refusal, and §11.6 asks for a rejection.
 */
export function deleteVerticesCommand(
  session: EditSession,
  vertices: readonly SelectedVertex[],
): Command {
  if (vertices.length === 0) {
    throw new Error('No vertices are selected, so there is nothing to delete.');
  }

  const byFeature = new Map<string, SelectedVertex[]>();
  for (const vertex of vertices) {
    const group = byFeature.get(vertex.featureId);
    if (group) group.push(vertex);
    else byFeature.set(vertex.featureId, [vertex]);
  }

  const deltas: FeatureDelta[] = [];
  for (const [featureId, group] of byFeature) {
    const before = required(session, featureId, 'delete a vertex');
    let geometry = before.geometry as Geometry;

    const ordered = [...group].sort(
      (left, right) => right.ring - left.ring || right.ordinal - left.ordinal,
    );
    for (const vertex of ordered) {
      geometry = removeVertex(geometry, vertex.ring, vertex.ordinal);
    }
    deltas.push(delta(before, geometry));
  }

  return {
    id: commandId(),
    label: vertices.length === 1 ? 'Delete Vertex' : `Delete ${vertices.length} Vertices`,
    deltas,
    timestamp: Date.now(),
  };
}
