import type {
  FitBoundsOptions,
  LayerSpecification,
  LngLatBoundsLike,
  MapGeoJSONFeature,
  PointLike,
  StyleSpecification,
} from 'maplibre-gl';
import type maplibregl from 'maplibre-gl';

import type { LayerMetadata as _LayerMetadata } from './metadata.js';

export type { LayerMetadata } from './metadata.js';

/** Camera state. `07-frontend.md` §2. */
export interface MapView {
  center: [number, number];
  zoom: number;
  bearing?: number;
  pitch?: number;
}

export interface MapWarning {
  kind: 'tile_failed' | 'glyph_missing' | 'sprite_missing' | 'style_error';
  message: string;
  url?: string;
}

/**
 * The public API of the map component, and the contract.
 *
 * Designed as if it will be consumed by three other applications, because
 * that is the stated goal. Deliberately **not** in this API: dataset
 * fetching, authentication, API base URLs, routing. The component receives a
 * style and metadata; where those came from is the app's problem.
 */
export interface WebMapProps {
  /**
   * MapLibre Style JSON. The single source of truth for appearance, and
   * derived rather than stored — `compileStyle` in `@webmap/style-model` is
   * the one that builds it, and the same function the render service runs
   * (`07-frontend.md` §3.1).
   */
  style: StyleSpecification;

  /** Initial camera. Uncontrolled after mount unless `view` is provided. */
  initialView?: MapView;

  /** Controlled camera. Supply with onViewChange for full control. */
  view?: MapView;
  onViewChange?: (view: MapView) => void;

  /** Layer metadata for legends, identify, and the layer tree. Keyed by
   *  MapLibre layer id. */
  layerMeta: Record<string, _LayerMetadata>;

  /** Overlay elements. Rendered as HTML above the canvas — so they are
   *  captured by the headless renderer's page screenshot. */
  overlays?: {
    legend?: boolean;
    scaleBar?: boolean;
    northArrow?: boolean;
  };

  /** Fires when the map has settled — all tiles, glyphs, sprites loaded.
   *  The render shell keys off this. */
  onIdle?: () => void;

  /** Non-fatal problems: failed tile requests, missing glyphs. Distinguishes
   *  "the data really is sparse there" from "the tile server was down". */
  onWarning?: (warning: MapWarning) => void;

  /** Cursor position in WGS84 lng/lat, or null when the pointer leaves the
   *  map. A prop rather than a `getMap()` call, because the status bar needs
   *  it on every session and §2.1 asks for escape-hatch uses to stay rare. */
  onPointerMove?: (lngLat: [number, number] | null) => void;

  className?: string;

  /** Announced to screen readers in place of "Map". Name the subject — "Map
   *  of Wolfcamp A structure" tells someone what they are on. */
  ariaLabel?: string;
}

/**
 * The pending edits drawn over the tiles. `09-editing.md` §3.3.
 *
 * **Why this is data and not style.** The features change on every pointer
 * move during a vertex drag, and pushing that through the style compiler would
 * mean rebuilding and diffing a whole style document per frame. MapLibre's own
 * answer is `setData` on a GeoJSON source, and that is what this is.
 *
 * The *appearance* is still the app's business: `layers` is supplied by the
 * caller rather than invented here, so this package continues to decide
 * nothing about how anything looks.
 */
export interface EditOverlay {
  /** Dirty features in WGS84, each carrying the `id` its base layer uses. */
  features: GeoJSON.Feature[];
  /**
   * How to draw them, appended above every other layer. Their `source` is
   * overwritten with the overlay's own, so a caller cannot accidentally point
   * them at a tile source that is about to be filtered.
   */
  layers: LayerSpecification[];
  /**
   * Base layers whose stale tile copies must be hidden.
   *
   * Without this the tile version of an edited feature draws underneath the
   * overlay, so a dragged vertex shows the boundary in both its old and new
   * positions — which reads as the edit not having taken.
   */
  hideFromLayers?: string[];
}

/**
 * Operations that do not fit declarative props. `07-frontend.md` §2.1.
 */
export interface WebMapHandle {
  fitBounds(bounds: LngLatBoundsLike, options?: FitBoundsOptions): void;
  /** The map as a PNG. See `capture.ts` for why it is not `canvas.toBlob`. */
  capture(): Promise<Blob>;
  /**
   * Features under a point, or within a box.
   *
   * The box form is what snapping uses: `09` §6.1 expands the pointer by the
   * larger of the vertex and edge tolerances and queries once, rather than
   * testing every segment in view.
   */
  queryFeatures(
    point: PointLike | [PointLike, PointLike],
    layerIds?: string[],
  ): MapGeoJSONFeature[];
  /**
   * WGS84 to screen pixels, and back.
   *
   * The snapping engine is written entirely in pixels (`09` §6.1) so that
   * these two are the only place a coordinate crosses between the spaces —
   * which is what keeps geodesic distance out of a 60 Hz path.
   */
  project(lngLat: [number, number]): [number, number];
  unproject(point: [number, number]): [number, number];
  /**
   * Draw the pending edits over the tiles, or clear them with `null`.
   *
   * Installs the source and layers on first use and updates the data on every
   * call after — so a drag costs one `setData`, not a style rebuild.
   */
  setEditOverlay(overlay: EditOverlay | null): void;
  /**
   * The escape hatch, and documented as unstable.
   *
   * It exists because not everything can be anticipated, but every use of it
   * in `apps/web` is a signal that the public API is missing something —
   * §2.1 asks for those to be tracked rather than accumulated.
   */
  getMap(): maplibregl.Map;
}
