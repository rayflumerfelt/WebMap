/**
 * @webmap/map — the map component. `07-frontend.md` §2.
 *
 * **Imports nothing from `apps/web`.** If it needs something from the app,
 * the app passes it as a prop. The moment this package imports app code,
 * reuse is already broken — and reuse is an explicit requirement of this
 * project. Enforced by `boundaries/element-types` in eslint.config.js.
 *
 */
export { WebMap } from './WebMap.js';
export { capture } from './capture.js';
export type {
  EditOverlay,
  LayerMetadata,
  MapView,
  MapWarning,
  WebMapHandle,
  WebMapProps,
} from './types.js';
