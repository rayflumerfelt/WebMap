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
   * assembled server-side — never constructed ad hoc in the browser
   * (`03-auth-security.md` §7.3).
   *
   * Typed as `unknown` until Phase 2 wires MapLibre's `StyleSpecification`;
   * `any` would defeat the point (`CLAUDE.md` §5).
   */
  style: unknown;

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
}
