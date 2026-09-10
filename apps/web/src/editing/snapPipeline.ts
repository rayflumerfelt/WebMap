/**
 * The snapping pipeline: pointer in, snapped coordinate out. `09-editing.md` §6.
 *
 * `snap.ts` is the pure half — candidates already in pixels, winner out. This
 * is the half that talks to the map, and it exists as its own module because
 * three of §6's rules live here rather than there, and each is the kind that
 * looks like an optimisation until it is a data-loss bug:
 *
 * **Dirty features snap against their dirty geometry** (§6.4). The tile copy of
 * a feature the user has already moved is stale by definition; snapping to it
 * would put the new boundary where the old one used to be, which is precisely
 * the sliver the whole feature exists to prevent.
 *
 * **Projections are cached for the duration of a drag** (§6.5). The camera does
 * not move mid-drag, so a candidate's pixels do not change — and re-projecting
 * every vertex in view at 60 Hz is the single most expensive thing this
 * subsystem does. The cache is dropped on any camera move, because edge-panning
 * during a drag makes every cached pixel wrong at once.
 *
 * **The tolerance clamp is reported, not hidden** (§6.3). A snap radius that has
 * quietly become 20 px of screen rather than 50 ft of ground is a behaviour
 * change the user is entitled to see.
 *
 * Everything here takes its map access as an injected `SnapDeps`, so the rules
 * are testable without a browser.
 */

import type { Geometry } from 'geojson';

import { ringsOf, toCandidates, queryBox } from './mapBridge.js';
import type { Project, QueriedFeature } from './mapBridge.js';
import type { Feature } from './session.js';
import { dragExclusion, pixelTolerance, snap } from './snap.js';
import type { Pixel, SnapCandidate, SnapConfig, SnapResult } from './snap.js';

/** A pixel box, or `null` for the whole viewport. */
export type QueryBox = [[number, number], [number, number]] | null;

/** What the pipeline needs from the map. Three methods, all synchronous. */
export interface SnapDeps {
  queryFeatures(box: QueryBox, layerIds: readonly string[]): QueriedFeature[];
  project: Project;
  unproject(point: [number, number]): [number, number];
}

/** The snapping settings a layer carries — `09` §6.2, in the user's units. */
export interface SnapSettings {
  /** The toolbar master toggle. */
  enabled: boolean;
  /** Whether other vertices of the feature being edited are snap targets.
   *  Default true: tracing a parcel back onto its own boundary is ordinary. */
  snapToSelf: boolean;
  /** Layers queried for candidates. Empty means nothing snaps — a state the
   *  status strip should show, since it looks identical to snapping being off. */
  layerIds: readonly string[];
  vertex: boolean;
  edge: boolean;
  intersection: boolean;
  midpoint: boolean;
  vertexToleranceFt: number;
  edgeToleranceFt: number;
}

/**
 * §6.2's defaults, with §6.3's running example as the distance.
 *
 * Intersection and midpoint are off because each costs a pass over every pair
 * of candidates and most editing does not need them; a fault-network session
 * turns them on, which is when the cost is worth paying.
 */
export const DEFAULT_SNAP_SETTINGS: SnapSettings = {
  enabled: true,
  snapToSelf: true,
  layerIds: [],
  vertex: true,
  edge: true,
  intersection: false,
  midpoint: false,
  vertexToleranceFt: 50,
  edgeToleranceFt: 50,
};

/** The vertex a drag is moving, so it and its ring neighbours are excluded. */
export interface DragContext {
  featureId: string;
  ring: number;
  ordinal: number;
  /** Coordinates in the ring, counting a closed ring's repeated first one. */
  ringLength: number;
  closed: boolean;
}

/** Where the camera is. Both are needed: a ground tolerance in pixels depends
 *  on latitude as well as zoom, and the difference over a state is large. */
export interface CameraContext {
  latitude: number;
  zoom: number;
}

export interface SnapRequest {
  pointer: Pixel;
  camera: CameraContext;
  /** The active layer's local geometry — pending edits and the working set. */
  local: LocalGeometry;
  drag?: DragContext | undefined;
  /**
   * Settings for this pass only.
   *
   * Vertex-add needs an edge pass whether or not snapping is switched on
   * (§11.6: "project onto the nearest edge of a selected feature"), and that
   * is a different question from where the cursor should land while drawing.
   */
  override?: Partial<SnapSettings> | undefined;
  /**
   * Restrict candidates to these features.
   *
   * Also vertex-add: a click near a *neighbour's* boundary must not splice a
   * vertex into the neighbour, because the feature the user selected is the
   * one they are editing.
   */
  onlyFeatureIds?: readonly string[] | undefined;
}

export interface ToleranceReport {
  vertexPx: number;
  edgePx: number;
  /**
   * Whether the clamp is in force, for the snap control's badge.
   *
   * The **vertex** tolerance decides, because it is the one the user is
   * usually thinking about and because reporting two independently would make
   * the badge say two different things at the same zoom. Reported as `null`
   * only when neither is clamped.
   */
  clamped: 'floor' | 'ceiling' | null;
}

export interface SnapOutcome {
  result: SnapResult | null;
  /** WGS84 of the snapped point, or of the raw pointer when nothing snapped —
   *  never null, because the caller always has a coordinate to place. */
  lngLat: [number, number];
  tolerance: ToleranceReport;
  /** Candidates considered, for the status strip and for tests. */
  candidateCount: number;
}

export interface SnapEngine {
  snapAt(request: SnapRequest): SnapOutcome;
  /** Camera moved: every cached pixel is now wrong. */
  invalidate(): void;
  /** The drag ended; drop the cache so the next hover re-queries. */
  endDrag(): void;
}

/** The active layer's local geometry, which beats anything a tile says. */
export interface LocalGeometry {
  /** Pending edits. `null` is a delete. */
  dirty: ReadonlyMap<string, Feature | null>;
  /** The working set: the layer's exact geometry as the server holds it
   *  (§17). Empty until it has been fetched. */
  exact: ReadonlyMap<string, Feature>;
  /** The style layers drawing the active layer. **Substitution is confined to
   *  these**: feature ids are assigned per dataset, so layer A's feature 42
   *  and layer B's feature 42 both exist, and replacing one with the other's
   *  geometry would move a snap target onto a different feature entirely. */
  layerIds: readonly string[];
  /** The layer id the substituted candidates are reported under. */
  activeLayerId: string;
}

/**
 * Local geometry substituted for tile geometry.
 *
 * Three rules, §6.4 and §6.6:
 *
 * - A feature in the **dirty buffer** replaces its tile copy, which is stale by
 *   definition. A pending delete removes it outright, so it stops being a snap
 *   target at once rather than when the save lands.
 * - A feature in the **working set** replaces its tile copy too, and the
 *   candidate is `exact: true`. That is §6.6's resolution, done by lookup
 *   rather than by coordinate matching — the working set *is* the exact
 *   geometry, so there is nothing to reconcile.
 * - Both are confined to the active layer's own style layers. Anything else
 *   keeps its tile geometry and stays `exact: false`.
 *
 * Every dirty feature is added whether or not the tile query returned it, and
 * every local feature is projected: the working set is capped at 5,000
 * features (§17), so this is bounded, and it runs once per drag rather than
 * per frame because the caller caches it.
 */
export function withLocal(
  candidates: readonly SnapCandidate[],
  local: LocalGeometry,
  project: Project,
): SnapCandidate[] {
  const mine = new Set(local.layerIds);
  const replaced = new Set<string>();

  const kept: SnapCandidate[] = [];
  for (const candidate of candidates) {
    if (!mine.has(candidate.layerId)) {
      kept.push(candidate);
      continue;
    }
    const feature = local.dirty.has(candidate.featureId)
      ? local.dirty.get(candidate.featureId)
      : local.exact.get(candidate.featureId);
    if (feature === undefined) {
      // Not local at all — a feature outside the working set, which happens
      // whenever the layer is larger than the cap and drawn from tiles.
      kept.push(candidate);
      continue;
    }
    replaced.add(candidate.featureId);
    const substituted = feature === null ? null : toCandidate(candidate.featureId, feature, local, project);
    if (substituted) kept.push(substituted);
  }

  // Dirty features the tile query did not return: a feature dragged out of
  // the query box is still being edited, and losing it as a snap target
  // mid-drag is the bug this covers.
  for (const [id, feature] of local.dirty) {
    if (feature === null || replaced.has(id)) continue;
    const candidate = toCandidate(id, feature, local, project);
    if (candidate) kept.push(candidate);
  }
  return kept;
}

function toCandidate(
  featureId: string,
  feature: Feature,
  local: LocalGeometry,
  project: Project,
): SnapCandidate | null {
  let rings;
  try {
    rings = ringsOf(feature.geometry as Geometry);
  } catch {
    // A geometry the model cannot ring is not a snap target. It is still a
    // perfectly good feature — a GeometryCollection pasted in, say — so this
    // must not take the rest of the pass down with it.
    return null;
  }
  if (rings.rings.length === 0) return null;

  return {
    featureId,
    layerId: local.activeLayerId,
    rings: rings.rings.map((ring) => ring.map(([x, y]) => toPixel(project([x, y])))),
    // Dirty or exact, both are authoritative: the buffer is the exact geometry
    // for a pending edit (§3.3) and the working set is the server's own.
    exact: true,
  };
}

function toPixel(point: [number, number]): Pixel {
  return { x: point[0], y: point[1] };
}

/** The tolerances for one frame, in pixels, with the clamp reported. */
export function tolerancesFor(
  settings: SnapSettings,
  camera: CameraContext,
): ToleranceReport {
  const vertex = pixelTolerance(settings.vertexToleranceFt, camera.latitude, camera.zoom);
  const edge = pixelTolerance(settings.edgeToleranceFt, camera.latitude, camera.zoom);
  return { vertexPx: vertex.px, edgePx: edge.px, clamped: vertex.clamped ?? edge.clamped };
}

function configFrom(settings: SnapSettings, tolerance: ToleranceReport): SnapConfig {
  return {
    vertex: settings.vertex,
    edge: settings.edge,
    intersection: settings.intersection,
    midpoint: settings.midpoint,
    vertexPx: tolerance.vertexPx,
    edgePx: tolerance.edgePx,
  };
}

/**
 * The engine. One per map; settings are read per call rather than captured, so
 * a toolbar toggle takes effect on the next pointer move rather than on the
 * next drag.
 */
export function createSnapEngine(deps: SnapDeps, getSettings: () => SnapSettings): SnapEngine {
  // Tile-derived candidates, already projected, held for the duration of a
  // drag. Never used outside one: on hover the pointer roams and a set built
  // for one box says nothing about another.
  let cached: SnapCandidate[] | null = null;

  function tileCandidates(
    request: SnapRequest,
    tolerance: ToleranceReport,
    settings: SnapSettings,
  ): SnapCandidate[] {
    if (request.drag) {
      if (cached === null) {
        // The whole viewport, once. A box around the pointer would have to be
        // re-queried as the pointer moved, which is the cost the cache exists
        // to remove.
        cached = toCandidates(deps.queryFeatures(null, settings.layerIds), deps.project);
      }
      return cached;
    }

    const box = queryBox(request.pointer, Math.max(tolerance.vertexPx, tolerance.edgePx));
    return toCandidates(deps.queryFeatures(box, settings.layerIds), deps.project);
  }

  return {
    invalidate() {
      cached = null;
    },
    endDrag() {
      cached = null;
    },
    snapAt(request: SnapRequest): SnapOutcome {
      const settings = request.override
        ? { ...getSettings(), ...request.override }
        : getSettings();
      const tolerance = tolerancesFor(settings, request.camera);
      const raw = deps.unproject([request.pointer.x, request.pointer.y]);

      if (!settings.enabled || settings.layerIds.length === 0) {
        return { result: null, lngLat: raw, tolerance, candidateCount: 0 };
      }

      let candidates = withLocal(
        tileCandidates(request, tolerance, settings),
        request.local,
        deps.project,
      );

      if (request.onlyFeatureIds) {
        const wanted = new Set(request.onlyFeatureIds);
        candidates = candidates.filter((candidate) => wanted.has(candidate.featureId));
      }

      const drag = request.drag;
      if (drag && !settings.snapToSelf) {
        // The whole feature, not just the dragged vertex: `snapToSelf: false`
        // means "do not snap to the thing I am editing", and leaving its other
        // vertices in would honour the toggle in name only.
        candidates = candidates.filter((candidate) => candidate.featureId !== drag.featureId);
      }

      const exclusion = drag
        ? dragExclusion(drag.featureId, drag.ring, drag.ordinal, drag.ringLength, drag.closed)
        : undefined;

      const result = snap(request.pointer, candidates, configFrom(settings, tolerance), exclusion);

      return {
        result,
        lngLat: result ? deps.unproject([result.pixel.x, result.pixel.y]) : raw,
        tolerance,
        candidateCount: candidates.length,
      };
    },
  };
}
