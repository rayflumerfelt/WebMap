/**
 * Map/session state. `07-frontend.md` §3.
 *
 * Three kinds of state, deliberately not conflated: server state is TanStack
 * Query's, ephemeral UI state is React local, and *this* is the session — the
 * layers, their order, their symbology, and the camera. It is the state that
 * has a URL, that Claude can read back, and that must reload identically.
 *
 * **Autosave exists because the session is the shared vocabulary with Claude.**
 * If a geologist reorders layers and then asks Claude to re-render, the server
 * must already hold what they are looking at — otherwise the render is of a
 * map nobody has seen. Debounced at 2 s, and the debounce is on the *trailing*
 * edge because the value worth saving is where a drag ended, not where it
 * started.
 */

import type { Symbology } from '@webmap/style-model';
import { create } from 'zustand';

export interface MapView {
  center: [number, number];
  zoom: number;
  bearing?: number;
  pitch?: number;
}

export interface SessionLayer {
  id: string;
  datasetId: string;
  name: string;
  symbology: Symbology;
  opacity: number;
  visible: boolean;
}

/** The shape written to `/api/v1/sessions`. Mirrors the stored document, so
 *  the wire format and the store cannot drift apart silently. */
export interface SessionSnapshot {
  layers: Array<{
    dataset_id: string;
    symbology_override: Symbology | null;
    opacity: number;
    visible: boolean;
    z: number;
  }>;
  view: MapView;
}

export interface SessionState {
  sessionId: string | null;
  shortCode: string | null;
  layers: SessionLayer[];
  view: MapView;
  selectedLayerId: string | null;
  /** True from the first local change until the save that covers it lands. */
  dirty: boolean;
  /** Set when a save was refused because someone else saved first. */
  conflict: boolean;
  lastSavedAt: number | null;

  load(session: LoadedSession): void;
  addLayer(layer: SessionLayer): void;
  removeLayer(layerId: string): void;
  reorderLayers(from: number, to: number): void;
  toggleVisibility(layerId: string): void;
  setOpacity(layerId: string, opacity: number): void;
  updateSymbology(layerId: string, symbology: Symbology): void;
  setView(view: MapView): void;
  select(layerId: string | null): void;
  markSaved(at?: number): void;
  markConflict(): void;
}

export interface LoadedSession {
  id: string;
  short_code: string;
  layers: SessionLayer[];
  view: MapView;
}

export const useSessionStore = create<SessionState>()((set) => ({
  sessionId: null,
  shortCode: null,
  layers: [],
  view: { center: [0, 0], zoom: 2 },
  selectedLayerId: null,
  dirty: false,
  conflict: false,
  lastSavedAt: null,

  load: (session) =>
    set({
      sessionId: session.id,
      shortCode: session.short_code,
      layers: session.layers,
      view: session.view,
      selectedLayerId: session.layers[0]?.id ?? null,
      // Loading is not a change. Marking it dirty would make every session
      // open write itself straight back, which turns a read into a write and
      // makes `updated_at` meaningless.
      dirty: false,
      conflict: false,
    }),

  addLayer: (layer) =>
    set((state) => ({
      layers: [...state.layers, layer],
      selectedLayerId: layer.id,
      dirty: true,
    })),

  removeLayer: (layerId) =>
    set((state) => ({
      layers: state.layers.filter((layer) => layer.id !== layerId),
      selectedLayerId: state.selectedLayerId === layerId ? null : state.selectedLayerId,
      dirty: true,
    })),

  reorderLayers: (from, to) =>
    set((state) => {
      if (from === to) return state;
      const layers = [...state.layers];
      const [moved] = layers.splice(from, 1);
      if (!moved) return state;
      layers.splice(to, 0, moved);
      return { layers, dirty: true };
    }),

  toggleVisibility: (layerId) =>
    set((state) => ({
      layers: state.layers.map((layer) =>
        layer.id === layerId ? { ...layer, visible: !layer.visible } : layer,
      ),
      dirty: true,
    })),

  setOpacity: (layerId, opacity) =>
    set((state) => ({
      layers: state.layers.map((layer) =>
        layer.id === layerId
          ? { ...layer, opacity: Math.max(0, Math.min(1, opacity)) }
          : layer,
      ),
      dirty: true,
    })),

  updateSymbology: (layerId, symbology) =>
    set((state) => ({
      layers: state.layers.map((layer) =>
        layer.id === layerId ? { ...layer, symbology } : layer,
      ),
      dirty: true,
    })),

  setView: (view) => set({ view, dirty: true }),

  select: (layerId) =>
    // Selection is ephemeral, not session state — it is not stored and it does
    // not make the session dirty. Two people opening the same link should not
    // fight over which layer is highlighted.
    set({ selectedLayerId: layerId }),

  markSaved: (at = Date.now()) => set({ dirty: false, conflict: false, lastSavedAt: at }),
  markConflict: () => set({ conflict: true }),
}));

/**
 * The snapshot the API stores.
 *
 * `z` comes from array position: the store keeps draw order as list order,
 * which is what the layer tree drags around, and the number is derived at the
 * boundary rather than maintained in two places.
 */
export function snapshotOf(state: Pick<SessionState, 'layers' | 'view'>): SessionSnapshot {
  return {
    layers: state.layers.map((layer, index) => ({
      dataset_id: layer.datasetId,
      symbology_override: layer.symbology,
      opacity: layer.opacity,
      visible: layer.visible,
      z: index,
    })),
    view: state.view,
  };
}
