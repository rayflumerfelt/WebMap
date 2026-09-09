import type {
  FitBoundsOptions,
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

  className?: string;

  /** Announced to screen readers in place of "Map". Name the subject — "Map
   *  of Wolfcamp A structure" tells someone what they are on. */
  ariaLabel?: string;
}

/**
 * Operations that do not fit declarative props. `07-frontend.md` §2.1.
 */
export interface WebMapHandle {
  fitBounds(bounds: LngLatBoundsLike, options?: FitBoundsOptions): void;
  /** The map as a PNG. See `capture.ts` for why it is not `canvas.toBlob`. */
  capture(): Promise<Blob>;
  queryFeatures(point: PointLike, layerIds?: string[]): MapGeoJSONFeature[];
  /**
   * The escape hatch, and documented as unstable.
   *
   * It exists because not everything can be anticipated, but every use of it
   * in `apps/web` is a signal that the public API is missing something —
   * §2.1 asks for those to be tracked rather than accumulated.
   */
  getMap(): maplibregl.Map;
}
