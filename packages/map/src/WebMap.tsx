/**
 * The map component. `07-frontend.md` §2.
 *
 * **Imports nothing from `apps/web`.** Dataset fetching, authentication, API
 * base URLs and routing are deliberately outside this API: the component
 * receives a style and some metadata, and where those came from is the app's
 * problem. That is what makes it consumable by the three other applications
 * the project intends.
 *
 * The commonest MapLibre-in-React bug is recreating the map on every render
 * (§2.2). The instance is created exactly once, in an effect with an empty
 * dependency list; every prop change afterwards mutates the existing map.
 */

import maplibregl from 'maplibre-gl';
import type { StyleSpecification } from 'maplibre-gl';
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from 'react';

import { capture } from './capture.js';
import type { MapView, MapWarning, WebMapHandle, WebMapProps } from './types.js';

const DEFAULT_VIEW: MapView = { center: [0, 0], zoom: 2 };

/** Below this the camera is treated as unchanged. */
const VIEW_EPSILON = 1e-9;

export const WebMap = forwardRef<WebMapHandle, WebMapProps>(function WebMap(props, ref) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);

  // Props read inside long-lived MapLibre listeners. Held in a ref rather than
  // captured, so the listeners can be registered once and still see current
  // callbacks — re-registering them on every render is the other half of the
  // recreate-the-map bug, and it leaks handlers instead of instances.
  const propsRef = useRef(props);
  propsRef.current = props;

  // Set while the component is moving the camera itself, so the resulting
  // `moveend` is not reported back as a user gesture. Without it a controlled
  // map feeds its own updates back to the parent and oscillates.
  const applyingViewRef = useRef(false);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const initial = propsRef.current.view ?? propsRef.current.initialView ?? DEFAULT_VIEW;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: propsRef.current.style as StyleSpecification,
      center: initial.center,
      zoom: initial.zoom,
      bearing: initial.bearing ?? 0,
      pitch: initial.pitch ?? 0,
      // NOT set: preserveDrawingBuffer. It costs real performance on every
      // pan and zoom, for a buffer only `capture()` needs — which gets it by
      // rendering on demand instead. See capture.ts.
      attributionControl: false,
    });
    mapRef.current = map;

    const handleMove = () => {
      if (applyingViewRef.current) return;
      propsRef.current.onViewChange?.(viewOf(map));
    };
    const handleIdle = () => propsRef.current.onIdle?.();
    const handleError = (event: { error?: Error }) => {
      // MapLibre reports a failed tile and a broken style through the same
      // event. Distinguishing them is the point: "the tile server was down"
      // and "the data really is sparse there" look identical on screen, and
      // a geologist will believe the second (§2, MapWarning).
      propsRef.current.onWarning?.(warningFrom(event.error));
    };

    const handlePointerMove = (event: { lngLat: { lng: number; lat: number } }) =>
      propsRef.current.onPointerMove?.([event.lngLat.lng, event.lngLat.lat]);
    const handlePointerOut = () => propsRef.current.onPointerMove?.(null);

    map.on('moveend', handleMove);
    map.on('idle', handleIdle);
    map.on('error', handleError);
    map.on('mousemove', handlePointerMove);
    map.on('mouseout', handlePointerOut);

    return () => {
      map.off('moveend', handleMove);
      map.off('idle', handleIdle);
      map.off('error', handleError);
      map.off('mousemove', handlePointerMove);
      map.off('mouseout', handlePointerOut);
      map.remove();
      mapRef.current = null;
    };
    // Empty deps, and honestly so: the body reads only refs. `07-frontend.md`
    // §2.2 sketches this effect capturing `props` directly and suppressing
    // exhaustive-deps to keep it from re-running — routing them through
    // `propsRef` instead makes the suppression unnecessary, so there is no
    // disabled rule here to go stale.
  }, []);

  // Style: diff and apply, never recreate. `setStyle` with `diff: true` keeps
  // the camera, the loaded tiles and the GPU buffers for layers that did not
  // change — recreating would blank the map on every symbology tweak.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.setStyle(props.style as StyleSpecification, { diff: true });
  }, [props.style]);

  // Camera: only when the parent controls it, and only when it actually
  // differs. Applying an equal view would fight the user mid-drag.
  useEffect(() => {
    const map = mapRef.current;
    const view = props.view;
    if (!map || !view || sameView(viewOf(map), view)) return;

    applyingViewRef.current = true;
    map.jumpTo({
      center: view.center,
      zoom: view.zoom,
      bearing: view.bearing ?? 0,
      pitch: view.pitch ?? 0,
    });
    applyingViewRef.current = false;
  }, [props.view]);

  const getMap = useCallback(() => {
    const map = mapRef.current;
    if (!map) {
      throw new Error(
        'The map is not mounted yet. Call this from onIdle or a later event ' +
          'rather than during the first render.',
      );
    }
    return map;
  }, []);

  useImperativeHandle(
    ref,
    (): WebMapHandle => ({
      fitBounds: (bounds, options) => getMap().fitBounds(bounds, options),
      capture: () => capture(getMap()),
      queryFeatures: (point, layerIds) =>
        getMap().queryRenderedFeatures(point, layerIds ? { layers: layerIds } : undefined),
      // The escape hatch. Every use of it in `apps/web` is a signal that the
      // public API is missing something — §2.1 asks for those to be tracked.
      getMap,
    }),
    [getMap],
  );

  return (
    <div
      ref={containerRef}
      className={props.className}
      // The canvas is not reachable by keyboard on its own, and a map that
      // cannot be panned without a mouse fails the §10 floor. MapLibre binds
      // arrow keys once the container has focus.
      tabIndex={0}
      role="application"
      aria-label={props.ariaLabel ?? 'Map'}
      style={{ width: '100%', height: '100%' }}
    />
  );
});

function viewOf(map: maplibregl.Map): MapView {
  const center = map.getCenter();
  return {
    center: [center.lng, center.lat],
    zoom: map.getZoom(),
    bearing: map.getBearing(),
    pitch: map.getPitch(),
  };
}

function sameView(a: MapView, b: MapView): boolean {
  return (
    Math.abs(a.center[0] - b.center[0]) < VIEW_EPSILON &&
    Math.abs(a.center[1] - b.center[1]) < VIEW_EPSILON &&
    Math.abs(a.zoom - b.zoom) < VIEW_EPSILON &&
    Math.abs((a.bearing ?? 0) - (b.bearing ?? 0)) < VIEW_EPSILON &&
    Math.abs((a.pitch ?? 0) - (b.pitch ?? 0)) < VIEW_EPSILON
  );
}

/**
 * Classify a MapLibre error into something a user can act on.
 *
 * The kinds matter more than the text. A missing glyph means every label on
 * the map is absent, which looks like a styling choice; a failed tile means a
 * blank area, which looks like sparse data. Both need to be reported as
 * failures rather than absorbed.
 */
function warningFrom(error: Error | undefined): MapWarning {
  const message = error?.message ?? 'The map reported an error with no message.';
  const url = (error as { url?: string } | undefined)?.url;

  let kind: MapWarning['kind'] = 'style_error';
  if (/glyph/i.test(message)) kind = 'glyph_missing';
  else if (/sprite/i.test(message)) kind = 'sprite_missing';
  else if (/tile|\.mvt|\.png/i.test(message) || url) kind = 'tile_failed';

  return { kind, message, ...(url ? { url } : {}) };
}
