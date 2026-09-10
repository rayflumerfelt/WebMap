/**
 * The editor wired to the map. `09-editing.md` §6, §11.6.
 *
 * The hook is thin by design — the rules live in `gestures`, `snapPipeline`,
 * `vertexCommands` and `overlay`, each tested on its own. What is asserted here
 * is the wiring: that a press claims the pointer, that a drop writes the
 * snapped coordinate rather than the raw one, and that a drag previews without
 * touching the dirty buffer.
 */

import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useEditStore } from '../stores/editStore.js';
import { useSessionStore } from '../stores/sessionStore.js';
import { HANDLE_LAYER_ID, ROLE, handleId } from './overlay.js';
import type { Feature } from './session.js';
import { useMapEditing } from './useMapEditing.js';
import type { MapEditingOptions } from './useMapEditing.js';

const LINE: Feature = {
  id: 'fault',
  geometry: {
    type: 'LineString',
    coordinates: [
      [0, 0],
      [1, 0],
      [2, 0],
    ],
  },
  properties: {},
};

/** A hundred pixels per degree, so every expected pixel is arithmetic. */
function fakeHandle() {
  const queryFeatures = vi.fn((_box: unknown, layerIds?: string[]) => {
    if (layerIds?.[0] === HANDLE_LAYER_ID) {
      return [
        {
          id: handleId({ featureId: 'fault', ring: 0, ordinal: 1 }),
          layer: { id: HANDLE_LAYER_ID },
          geometry: { type: 'Point', coordinates: [1, 0] },
          properties: { [ROLE]: 'handle' },
        },
      ] as never[];
    }
    return [
      {
        id: 'fault',
        layer: { id: 'faults-line' },
        geometry: LINE.geometry,
        properties: {},
      },
      {
        // A second feature to snap *to*: the dragged vertex and its two ring
        // neighbours are excluded (§6.4), so a fault cannot snap to itself
        // here and a test that expected it to would be testing nothing.
        id: 'lease',
        layer: { id: 'faults-line' },
        geometry: { type: 'Point', coordinates: [0, 0] },
        properties: {},
      },
    ] as never[];
  });

  return {
    queryFeatures,
    project: vi.fn((lngLat: [number, number]): [number, number] => [
      lngLat[0] * 100,
      lngLat[1] * 100,
    ]),
    unproject: vi.fn((point: [number, number]): [number, number] => [
      point[0] / 100,
      point[1] / 100,
    ]),
    setEditOverlay: vi.fn(),
    fitBounds: vi.fn(),
    capture: vi.fn(),
    getMap: vi.fn(),
  };
}

function pointer(type: string, x: number, y: number, shiftKey = false) {
  return {
    type,
    point: [x, y] as [number, number],
    lngLat: [x / 100, y / 100] as [number, number],
    shiftKey,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    preventDefault: vi.fn(),
  };
}

function setup(overrides: Partial<MapEditingOptions> = {}) {
  const handle = fakeHandle();
  const map = { current: handle as never };
  const rendered = renderHook(() =>
    useMapEditing({
      map,
      baseLayerIds: ['faults-line'],
      // Synchronous, so a pointer move is observable in the same tick. The
      // production default is the next animation frame (§6.5).
      schedule: (callback) => callback(),
      ...overrides,
    }),
  );
  return { handle, rendered };
}

beforeEach(() => {
  useSessionStore.setState({ view: { center: [0, 0], zoom: 20 } });
  useEditStore.setState({ session: null, revision: 0 });
  useEditStore.getState().dispatch({ type: 'setActiveLayer', layerId: null });
  useEditStore.getState().activateLayer({
    layerId: 'faults',
    baseVersion: 1,
    geometry: 'line',
    canEdit: true,
    features: [LINE],
  });
  useEditStore.getState().setSnap({ enabled: false, layerIds: ['faults-line'] });
});

function enterVertexMode() {
  const store = useEditStore.getState();
  store.dispatch({ type: 'selectFeatures', ids: ['fault'] });
  store.dispatch({ type: 'setMode', mode: 'vertex' });
}

describe('a drag', () => {
  it('claims the pointer so the map does not pan', () => {
    // Claimed on the press, before the threshold: a drag that starts slowly
    // would otherwise pan the map out from under the vertex.
    enterVertexMode();
    const { handle, rendered } = setup();
    void handle;
    const down = pointer('down', 100, 0);

    act(() => rendered.result.current.onMapPointer(down as never));

    expect(down.preventDefault).toHaveBeenCalled();
  });

  it('writes the vertex where it was dropped', () => {
    enterVertexMode();
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('move', 150, 50) as never));
    act(() => rendered.result.current.onMapPointer(pointer('up', 150, 50) as never));

    const session = useEditStore.getState().session!;
    expect((session.dirty.get('fault')!.geometry as { coordinates: number[][] }).coordinates).toEqual(
      [
        [0, 0],
        [1.5, 0.5],
        [2, 0],
      ],
    );
  });

  it('writes nothing to the dirty buffer while it is still moving', () => {
    // §5.1: an operation in flight has reached neither the dirty buffer nor
    // the undo stack.
    enterVertexMode();
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('move', 150, 50) as never));

    expect(useEditStore.getState().session!.dirty.size).toBe(0);
    expect(useEditStore.getState().mode.operationActive).toBe(true);
  });

  it('draws the moving vertex, and hides the tile copy under it', () => {
    // Without the exclusion the feature's original outline draws underneath
    // the one being dragged, and the drag looks like a duplicate.
    enterVertexMode();
    const { handle, rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('move', 150, 50) as never));

    const overlay = handle.setEditOverlay.mock.calls.at(-1)![0] as {
      features: Array<{ geometry: { coordinates: unknown } }>;
      hideFromLayers?: string[];
    };
    expect(overlay.features[0]!.geometry.coordinates).toEqual([
      [0, 0],
      [1.5, 0.5],
      [2, 0],
    ]);
    expect(overlay.hideFromLayers).toEqual(['faults-line']);
  });

  it('leaves nothing behind when Escape cancels it', () => {
    enterVertexMode();
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('move', 150, 50) as never));
    let consumed = false;
    act(() => {
      consumed = rendered.result.current.onEscape();
    });

    expect(consumed).toBe(true);
    expect(useEditStore.getState().session!.dirty.size).toBe(0);
    expect(useEditStore.getState().mode.operationActive).toBe(false);
  });

  it('does not consume an Escape it had no drag for', () => {
    // §4's first Escape belongs to the operation, and the mode machine has to
    // see the press when there is no operation running.
    const { rendered } = setup();

    expect(rendered.result.current.onEscape()).toBe(false);
  });
});

describe('a press that does not move', () => {
  it('selects the vertex', () => {
    enterVertexMode();
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('up', 100, 0) as never));

    expect(useEditStore.getState().mode.selectedVertexIds).toEqual(['fault:0:1']);
  });

  it('toggles it off on a second shift-press', () => {
    enterVertexMode();
    const { rendered } = setup();

    for (const shift of [true, true]) {
      act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0, shift) as never));
      act(() => rendered.result.current.onMapPointer(pointer('up', 100, 0, shift) as never));
    }

    expect(useEditStore.getState().mode.selectedVertexIds).toEqual([]);
  });
});

describe('selection', () => {
  it('selects the feature under a click in select mode', () => {
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('click', 100, 0) as never));

    expect(useEditStore.getState().mode.selectedFeatureIds).toEqual(['fault']);
  });

  it('ignores a click while in vertex mode', () => {
    // Vertex mode's clicks belong to the handles; re-selecting the feature
    // underneath would clear the vertex selection on every press.
    enterVertexMode();
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('up', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('click', 100, 0) as never));

    expect(useEditStore.getState().mode.selectedVertexIds).toEqual(['fault:0:1']);
  });
});

describe('double-click on a handle', () => {
  it('deletes that vertex', () => {
    enterVertexMode();
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('dblclick', 100, 0) as never));

    const geometry = useEditStore.getState().session!.dirty.get('fault')!.geometry as {
      coordinates: number[][];
    };
    expect(geometry.coordinates).toEqual([
      [0, 0],
      [2, 0],
    ]);
  });

  it('reports a refusal instead of failing silently', () => {
    // A delete that would take the ring below its minimum is refused (§11.6),
    // and a refusal nobody sees is a tool that looks broken.
    enterVertexMode();
    const onError = vi.fn();
    const { rendered } = setup({ onError });

    act(() => rendered.result.current.onMapPointer(pointer('dblclick', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('dblclick', 100, 0) as never));

    expect(onError).toHaveBeenCalledWith(expect.stringMatching(/needs at least 2/));
  });
});

describe('when the editor is not the active tool', () => {
  it('ignores the pointer entirely', () => {
    // The map's pointer props are set once. A handler that stayed live would
    // change the edit selection from a click made with the identify tool.
    enterVertexMode();
    const { rendered } = setup({ enabled: false });

    act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('up', 100, 0) as never));

    expect(useEditStore.getState().mode.selectedVertexIds).toEqual([]);
  });

  it('takes the overlay down with it', () => {
    // Handles left on the map by a tool that is no longer active are handles
    // that do nothing when clicked.
    enterVertexMode();
    const { handle } = setup({ enabled: false });

    expect(handle.setEditOverlay).toHaveBeenCalledWith(null);
  });
});

describe('snapping', () => {
  it('reports what the cursor is over on hover', () => {
    enterVertexMode();
    useEditStore.getState().setSnap({ enabled: true, layerIds: ['faults-line'] });
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('move', 100, 2) as never));

    expect(rendered.result.current.snap?.featureId).toBe('fault');
    expect(rendered.result.current.snap?.type).toBe('vertex');
  });

  it('drops the vertex on the snap, not on the cursor', () => {
    // The point of snapping: the committed coordinate is the target's, and a
    // commit that used the raw pointer would leave the sliver behind anyway.
    enterVertexMode();
    useEditStore.getState().setSnap({ enabled: true, layerIds: ['faults-line'] });
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('down', 100, 0) as never));
    act(() => rendered.result.current.onMapPointer(pointer('move', 3, 2) as never));
    act(() => rendered.result.current.onMapPointer(pointer('up', 3, 2) as never));

    const geometry = useEditStore.getState().session!.dirty.get('fault')!.geometry as {
      coordinates: number[][];
    };
    expect(geometry.coordinates[1]).toEqual([0, 0]);
    expect(rendered.result.current.snap?.featureId).toBe('lease');
  });

  it('reports the tolerance clamp for the toolbar badge', () => {
    enterVertexMode();
    useEditStore.getState().setSnap({ enabled: true, layerIds: ['faults-line'] });
    const { rendered } = setup();

    act(() => rendered.result.current.onMapPointer(pointer('move', 100, 2) as never));

    expect(rendered.result.current.tolerance?.clamped).toBe('ceiling');
  });
});
