/**
 * What the editor draws on the map. `09-editing.md` §3.3, §6.7, §11.6.
 *
 * Three things render from the edit session: the **pending edits** themselves,
 * the **vertex handles** in vertex mode, and the **snap indicator**. All three
 * go into one GeoJSON source, distinguished by an `_edit` role property, for a
 * blunt reason — a MapLibre source is a network of GPU buffers, and three of
 * them updating at pointer rate cost three times what one does. The layers
 * filter on the role, which is a per-feature test the renderer does anyway.
 *
 * Everything here is a pure function from session state to GeoJSON, so the
 * rules that matter can be asserted without a map:
 *
 * - A **closed ring's repeated last coordinate gets no handle of its own**. It
 *   is the first vertex, drawn twice; a handle there would let a user drag one
 *   copy and leave the polygon open.
 * - **Deleted features keep their id in the exclusion** so the tile copy stays
 *   hidden, while contributing no geometry — which is what makes a pending
 *   delete look like a delete before the save lands (§3.3).
 * - The indicator is **one point feature**, replaced wholesale each frame.
 */

import type { EditOverlay } from '@webmap/map';
import type { Feature as GeoJsonFeature, Geometry, Point } from 'geojson';

import { indicatorIconId } from './icons.js';
import { ringsOf } from './mapBridge.js';
import type { Feature } from './session.js';
import type { SnapResult } from './snap.js';

/** The role property every overlay feature carries. */
export const ROLE = '_edit';

export type OverlayRole = 'feature' | 'handle' | 'indicator';

/** A vertex the user has selected, in the addressing §6.6 insists on. */
export interface SelectedVertex {
  featureId: string;
  ring: number;
  ordinal: number;
}

export interface OverlayInput {
  /** The session's pending edits. `null` is a delete. */
  dirty: ReadonlyMap<string, Feature | null>;
  /** Features whose vertices get handles — in vertex mode, the selection.
   *  Empty in every other mode: handles on an unselected feature invite a drag
   *  the mode does not support. */
  handleFeatures: readonly Feature[];
  selectedVertices: readonly SelectedVertex[];
  /** The current snap, or `null`. Drawn where the snap landed, not under the
   *  cursor: the whole point is to show that the two differ. */
  snap: (Pick<SnapResult, 'type' | 'isExact'> & { lngLat: [number, number] }) | null;
  /** Base layers whose tile copies must be filtered — the layers the edited
   *  features are drawn by. */
  baseLayerIds: readonly string[];
}

/** A handle's feature id, and the string a hit test reads back. */
export function handleId(vertex: SelectedVertex): string {
  return `${vertex.featureId}:${vertex.ring}:${vertex.ordinal}`;
}

/**
 * The vertex a handle id addresses, or `null` if it is not one.
 *
 * Parsed from the right: a feature id is opaque and may itself contain a
 * colon, while the last two segments are always the ring and the ordinal.
 * Splitting from the left instead would address a vertex of the wrong feature,
 * which is a wrong edit rather than a failed one.
 */
export function parseHandleId(id: string): SelectedVertex | null {
  const parts = id.split(':');
  if (parts.length < 3) return null;

  const ordinal = Number(parts.pop());
  const ring = Number(parts.pop());
  const featureId = parts.join(':');
  if (!Number.isInteger(ring) || !Number.isInteger(ordinal) || featureId === '') return null;

  return { featureId, ring, ordinal };
}

/**
 * The vertex handles for one feature.
 *
 * A closed ring's last coordinate repeats its first, and gets no handle: two
 * handles stacked on one vertex means a drag that moves one of them and leaves
 * the ring open — the corruption §7.3 warns about, arriving from the UI rather
 * than from the topology engine.
 */
export function handlesFor(
  feature: Feature,
  selected: ReadonlySet<string>,
): GeoJsonFeature<Point>[] {
  let geometry;
  try {
    geometry = ringsOf(feature.geometry as Geometry);
  } catch {
    // A geometry with no single answer to "does this ring close" has no
    // sensible handle set either. It is still editable through its attributes.
    return [];
  }

  const handles: GeoJsonFeature<Point>[] = [];
  for (const [ring, coordinates] of geometry.rings.entries()) {
    const count = geometry.closed ? coordinates.length - 1 : coordinates.length;
    for (let ordinal = 0; ordinal < count; ordinal += 1) {
      const position = coordinates[ordinal]!;
      const id = handleId({ featureId: feature.id, ring, ordinal });
      handles.push({
        type: 'Feature',
        id,
        geometry: { type: 'Point', coordinates: [position[0], position[1]] },
        properties: {
          [ROLE]: 'handle' satisfies OverlayRole,
          featureId: feature.id,
          ring,
          ordinal,
          // A boolean, not the id: `icon-image` picks the selected glyph with
          // a `case` expression, and expressions cannot compare against a set.
          selected: selected.has(id),
        },
      });
    }
  }
  return handles;
}

/** The whole overlay: features, then handles, then the indicator. */
export function buildOverlay(input: OverlayInput): EditOverlay {
  const features: GeoJsonFeature[] = [];
  const hiddenIds: string[] = [];

  for (const [id, feature] of input.dirty) {
    hiddenIds.push(id);
    if (feature === null) continue;
    features.push({
      type: 'Feature',
      id,
      geometry: feature.geometry as Geometry,
      properties: { ...feature.properties, [ROLE]: 'feature' satisfies OverlayRole },
    });
  }

  const selected = new Set(input.selectedVertices.map(handleId));
  for (const feature of input.handleFeatures) {
    features.push(...handlesFor(feature, selected));
  }

  if (input.snap) {
    features.push({
      type: 'Feature',
      id: 'edit-snap-indicator',
      geometry: { type: 'Point', coordinates: input.snap.lngLat },
      properties: {
        [ROLE]: 'indicator' satisfies OverlayRole,
        icon: indicatorIconId(input.snap.type, input.snap.isExact),
      },
    });
  }

  return {
    features,
    layers: EDIT_LAYERS,
    // Only layers that actually have something to hide. An exclusion filter on
    // a clean layer costs a filter evaluation per feature per frame for no
    // effect, and `setEditOverlay` restores what it did not set.
    ...(hiddenIds.length > 0 ? { hideFromLayers: [...input.baseLayerIds] } : {}),
  };
}

const ACCENT = '#ff8c00';

/**
 * The overlay's layers, in draw order: geometry, then handles, then the
 * indicator on top of everything.
 *
 * `source` is filled in by `setEditOverlay`, which overwrites whatever is here
 * — the value below is a placeholder the type demands.
 *
 * Handles and the indicator set `icon-allow-overlap` and
 * `icon-ignore-placement`. Without both, MapLibre's label collision detection
 * drops handles in dense geometry, and a vertex whose handle silently did not
 * render is a vertex the user cannot edit and has no way to know about.
 */
export const EDIT_LAYERS: EditOverlay['layers'] = [
  {
    id: 'edit-fill',
    type: 'fill',
    source: 'placeholder',
    filter: ['all', ['==', ['get', ROLE], 'feature'], ['==', ['geometry-type'], 'Polygon']],
    paint: { 'fill-color': ACCENT, 'fill-opacity': 0.15 },
  },
  {
    id: 'edit-line',
    type: 'line',
    source: 'placeholder',
    filter: [
      'all',
      ['==', ['get', ROLE], 'feature'],
      ['!=', ['geometry-type'], 'Point'],
    ],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': ACCENT, 'line-width': 2 },
  },
  {
    id: 'edit-point',
    type: 'circle',
    source: 'placeholder',
    filter: ['all', ['==', ['get', ROLE], 'feature'], ['==', ['geometry-type'], 'Point']],
    paint: {
      'circle-radius': 5,
      'circle-color': ACCENT,
      'circle-stroke-color': '#1f2933',
      'circle-stroke-width': 1.5,
    },
  },
  {
    id: 'edit-handles',
    type: 'symbol',
    source: 'placeholder',
    filter: ['==', ['get', ROLE], 'handle'],
    layout: {
      'icon-image': ['case', ['get', 'selected'], 'edit-handle-selected', 'edit-handle'],
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
    },
  },
  {
    id: 'edit-snap-indicator',
    type: 'symbol',
    source: 'placeholder',
    filter: ['==', ['get', ROLE], 'indicator'],
    layout: {
      'icon-image': ['get', 'icon'],
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
    },
  },
];

/** The layer ids a hit test should ask for when looking for a handle. */
export const HANDLE_LAYER_ID = 'edit-handles';
