/**
 * The editor, wired to the map. `09-editing.md` §6, §11.6.
 *
 * Every rule this hook depends on lives somewhere testable — `gestures.ts`
 * decides what a press means, `snapPipeline.ts` decides where the cursor
 * lands, `vertexCommands.ts` decides what the undo stack records, `overlay.ts`
 * decides what is drawn. What is left here is the wiring, and it is deliberately
 * thin: a hook is the one place in this subsystem a test cannot easily reach,
 * so as little as possible is decided in it.
 *
 * Two things it does decide, both about *when*:
 *
 * **Pointer work is throttled to a frame.** `pointermove` fires far more often
 * than the screen paints (§6.5), and the snap pass is the expensive half of
 * the budget. The scheduler is injectable so tests run it synchronously.
 *
 * **A drag previews without writing.** §5.1: nothing reaches the dirty buffer
 * until the pointer comes up. The preview is layered over the dirty map on its
 * way to the overlay, which also hides the tile copy for the duration — without
 * that, the feature's original outline draws under the one being dragged.
 */

import type { MapPointerEvent, WebMapHandle } from '@webmap/map';
import type { Geometry } from 'geojson';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useEditStore, handleFeatures, selectedVertices } from '../stores/editStore.js';
import { useSessionStore } from '../stores/sessionStore.js';
import { setVertex } from './geometryEdits.js';
import { IDLE_GESTURE, claimsPointer, step } from './gestures.js';
import type { Gesture, GestureIntent } from './gestures.js';
import { hitFeatureIds, queryBox, ringsOf } from './mapBridge.js';
import { isVertexMode } from './modes.js';
import { HANDLE_LAYER_ID, buildOverlay, handleId, parseHandleId } from './overlay.js';
import type { SelectedVertex } from './overlay.js';
import { current } from './session.js';
import type { Feature } from './session.js';
import type { Pixel, SnapResult } from './snap.js';
import { createSnapEngine } from './snapPipeline.js';
import type { DragContext, SnapDeps, ToleranceReport } from './snapPipeline.js';
import { deleteVerticesCommand, moveVertexCommand } from './vertexCommands.js';

/** How close to a handle a press counts as being on it. Generous on purpose:
 *  §11.6 keeps the handle glyph small so it does not hide the geometry, and
 *  the hit slop is what makes a small glyph comfortable to grab. */
const HANDLE_HIT_PX = 8;

export interface MapEditingOptions {
  map: { current: WebMapHandle | null };
  /** The style layers drawing the active layer — hit-tested for selection, and
   *  filtered so a dirty feature's tile copy does not show through. */
  baseLayerIds: readonly string[];
  /**
   * Whether the editor is handling the pointer at all.
   *
   * False outside the edit tool, and it has to be checked here rather than by
   * not passing the handler: the map's pointer props are set once, and a
   * handler that stayed live would change the edit selection from a click the
   * user made with the identify tool.
   */
  enabled?: boolean;
  /** Runs the throttled pointer work. Defaults to the next animation frame;
   *  tests pass a synchronous one. */
  schedule?: (callback: () => void) => void;
  /** Surfaces a refused edit — a delete below a ring's minimum, a stale
   *  selection. Without it those messages go nowhere and the tool looks
   *  broken rather than unwilling. */
  onError?: (message: string) => void;
}

export interface MapEditing {
  /** Pass to `WebMap`'s `onMapPointer`. */
  onMapPointer(event: MapPointerEvent): void;
  /** Call from the Escape handler, before the mode machine sees it. Returns
   *  true when it consumed the press — a drag was cancelled. */
  onEscape(): boolean;
  /** The current snap, for the status strip. */
  snap: SnapResult | null;
  tolerance: ToleranceReport | null;
}

const DEFAULT_SCHEDULE = (callback: () => void) => {
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(callback);
  else callback();
};

export function useMapEditing(options: MapEditingOptions): MapEditing {
  const { map, baseLayerIds, onError } = options;
  const enabled = options.enabled ?? true;
  const schedule = options.schedule ?? DEFAULT_SCHEDULE;

  const store = useEditStore();
  const view = useSessionStore((state) => state.view);

  const [snap, setSnap] = useState<SnapResult | null>(null);
  const [tolerance, setTolerance] = useState<ToleranceReport | null>(null);
  const [preview, setPreview] = useState<Feature | null>(null);

  const gestureRef = useRef<Gesture>(IDLE_GESTURE);
  const frameRef = useRef(false);
  // Read inside callbacks that must see the current values without being
  // rebuilt — a handler rebuilt every render would re-register listeners.
  const stateRef = useRef({ store, view, baseLayerIds });
  stateRef.current = { store, view, baseLayerIds };
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const engine = useMemo(() => {
    const deps: SnapDeps = {
      queryFeatures: (box, layerIds) =>
        map.current?.queryFeatures(box ?? undefined, [...layerIds]) ?? [],
      project: (lngLat) => map.current?.project(lngLat) ?? [0, 0],
      unproject: (point) => map.current?.unproject(point) ?? [0, 0],
    };
    return createSnapEngine(deps, () => stateRef.current.store.snap);
  }, [map]);

  // The camera moved: every cached projection is now wrong (§6.5).
  useEffect(() => engine.invalidate(), [engine, view]);

  const dragContextFor = useCallback((vertex: SelectedVertex): DragContext | undefined => {
    const session = stateRef.current.store.session;
    const feature = session ? current(session, vertex.featureId) : null;
    if (!feature) return undefined;

    try {
      const rings = ringsOf(feature.geometry as Geometry);
      return {
        featureId: vertex.featureId,
        ring: vertex.ring,
        ordinal: vertex.ordinal,
        ringLength: rings.rings[vertex.ring]?.length ?? 0,
        closed: rings.closed,
      };
    } catch {
      return undefined;
    }
  }, []);

  /** One snap pass. Returns where the pointer should be treated as being. */
  const runSnap = useCallback(
    (point: Pixel, vertex: SelectedVertex | null): [number, number] => {
      const { store: live, view: camera } = stateRef.current;
      const session = live.session;
      const outcome = engine.snapAt({
        pointer: point,
        camera: { latitude: camera.center[1], zoom: camera.zoom },
        dirty: session?.dirty ?? new Map(),
        activeLayerId: live.mode.activeLayerId ?? '',
        ...(vertex ? { drag: dragContextFor(vertex) } : {}),
      });

      setSnap(outcome.result);
      setTolerance(outcome.tolerance);
      return outcome.lngLat;
    },
    [dragContextFor, engine],
  );

  const previewAt = useCallback((vertex: SelectedVertex, lngLat: [number, number]) => {
    const session = stateRef.current.store.session;
    const feature = session ? current(session, vertex.featureId) : null;
    if (!feature) return;

    try {
      setPreview({
        ...feature,
        geometry: setVertex(feature.geometry as Geometry, vertex.ring, vertex.ordinal, lngLat),
      });
    } catch {
      // A ring index that no longer exists — a background refresh rebuilt the
      // working set mid-drag. Dropping the preview frame is the right response:
      // the drop will refuse loudly, and a half-drawn preview would suggest it
      // had worked.
      setPreview(null);
    }
  }, []);

  const handleAt = useCallback(
    (point: Pixel): SelectedVertex | null => {
      if (!map.current) return null;
      const features = map.current.queryFeatures(queryBox(point, HANDLE_HIT_PX), [
        HANDLE_LAYER_ID,
      ]);
      const [id] = hitFeatureIds(features);
      return id ? parseHandleId(id) : null;
    },
    [map],
  );

  const perform = useCallback(
    (intent: GestureIntent, point: Pixel) => {
      const live = stateRef.current.store;
      switch (intent.type) {
        case 'none':
          return;

        case 'selectVertex': {
          const id = handleId(intent.vertex);
          const already = live.mode.selectedVertexIds;
          const ids = intent.additive
            ? already.includes(id)
              ? already.filter((candidate) => candidate !== id)
              : [...already, id]
            : [id];
          live.dispatch({ type: 'selectVertices', ids });
          return;
        }

        case 'beginDrag':
          live.dispatch({ type: 'operationStarted' });
          previewAt(intent.vertex, runSnap(point, intent.vertex));
          return;

        case 'previewDrag':
          previewAt(intent.vertex, runSnap(point, intent.vertex));
          return;

        case 'commitDrag': {
          const lngLat = runSnap(point, intent.vertex);
          setPreview(null);
          live.dispatch({ type: 'operationResolved' });
          if (!live.session) return;
          try {
            live.applyCommand(moveVertexCommand(live.session, intent.vertex, lngLat));
          } catch (error) {
            onError?.(error instanceof Error ? error.message : String(error));
          }
          return;
        }

        case 'cancelDrag':
          setPreview(null);
          engine.endDrag();
          live.dispatch({ type: 'operationResolved' });
          return;
      }
    },
    [engine, onError, previewAt, runSnap],
  );

  const onMapPointer = useCallback(
    (event: MapPointerEvent) => {
      if (!enabledRef.current) return;
      const point: Pixel = { x: event.point[0], y: event.point[1] };
      const live = stateRef.current.store;
      const vertexMode = isVertexMode(live.mode.mode);

      switch (event.type) {
        case 'down': {
          if (!vertexMode) return;
          const result = step(gestureRef.current, {
            type: 'down',
            point,
            handle: handleAt(point),
            additive: event.shiftKey,
          });
          gestureRef.current = result.gesture;
          // Claimed before the threshold, or a drag that starts slowly pans
          // the map instead of moving the vertex.
          if (claimsPointer(result.gesture)) event.preventDefault();
          perform(result.intent, point);
          return;
        }

        case 'move': {
          if (frameRef.current) return;
          frameRef.current = true;
          schedule(() => {
            frameRef.current = false;
            const result = step(gestureRef.current, { type: 'move', point });
            gestureRef.current = result.gesture;
            if (result.intent.type === 'none' && isVertexMode(stateRef.current.store.mode.mode)) {
              // Hover: the indicator still has to track the cursor, which is
              // what tells the user a snap is available before they commit to
              // a drag (§6.7).
              runSnap(point, null);
              return;
            }
            perform(result.intent, point);
          });
          return;
        }

        case 'up': {
          const result = step(gestureRef.current, { type: 'up', point });
          gestureRef.current = result.gesture;
          engine.endDrag();
          perform(result.intent, point);
          return;
        }

        case 'click': {
          // Only when no gesture claimed the press: a drop is not a click on
          // whatever happened to be underneath.
          if (gestureRef.current.kind !== 'idle' || vertexMode) return;
          const features = map.current?.queryFeatures(queryBox(point, HANDLE_HIT_PX), [
            ...stateRef.current.baseLayerIds,
          ]);
          live.dispatch({
            type: 'selectFeatures',
            ids: features ? hitFeatureIds(features).slice(0, 1) : [],
            additive: event.shiftKey,
          });
          return;
        }

        case 'dblclick': {
          // §11.6: double-clicking a handle deletes that vertex.
          if (!vertexMode) return;
          const vertex = handleAt(point);
          if (!vertex || !live.session) return;
          event.preventDefault();
          try {
            live.applyCommand(deleteVerticesCommand(live.session, [vertex]));
          } catch (error) {
            onError?.(error instanceof Error ? error.message : String(error));
          }
          return;
        }
      }
    },
    [engine, handleAt, map, onError, perform, runSnap, schedule],
  );

  const onEscape = useCallback(() => {
    const result = step(gestureRef.current, { type: 'escape' });
    gestureRef.current = result.gesture;
    if (result.intent.type === 'none') return false;
    perform(result.intent, { x: 0, y: 0 });
    return true;
  }, [perform]);

  // The overlay: dirty features with the drag preview layered over them, the
  // handles for the selection, and the snap indicator.
  useEffect(() => {
    const handle = map.current;
    if (!handle) return;

    const session = store.session;
    if (!session || !enabled) {
      handle.setEditOverlay(null);
      return;
    }

    const dirty = new Map(session.dirty);
    // The preview goes in with the dirty features rather than beside them, so
    // the tile copy of the feature being dragged is hidden for the duration.
    // Without that the original outline draws underneath the dragged one.
    if (preview) dirty.set(preview.id, preview);

    const withPreview = preview
      ? handleFeatures(store).map((feature) => (feature.id === preview.id ? preview : feature))
      : handleFeatures(store);

    handle.setEditOverlay(
      buildOverlay({
        dirty,
        handleFeatures: withPreview,
        selectedVertices: selectedVertices(store),
        snap: snap
          ? {
              type: snap.type,
              isExact: snap.isExact,
              lngLat: map.current?.unproject([snap.pixel.x, snap.pixel.y]) ?? [0, 0],
            }
          : null,
        baseLayerIds,
      }),
    );
  }, [baseLayerIds, enabled, map, preview, snap, store]);

  return { onMapPointer, onEscape, snap, tolerance };
}
