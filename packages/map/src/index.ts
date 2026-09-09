/**
 * @webmap/map — the map component. `07-frontend.md` §2.
 *
 * **Imports nothing from `apps/web`.** If it needs something from the app,
 * the app passes it as a prop. The moment this package imports app code,
 * reuse is already broken — and reuse is an explicit requirement of this
 * project. Enforced by `boundaries/element-types` in eslint.config.js.
 *
 * The `WebMap` component itself lands in Phase 2. The prop types are here now
 * because they are the contract the rest of the design is written against.
 */
export type { LayerMetadata, MapView, MapWarning, WebMapProps } from './types.js';
