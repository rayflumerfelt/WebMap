/**
 * The map component against a mocked MapLibre. `07-frontend.md` §11:
 * "Vitest + mocked MapLibre — prop → instance-call assertions."
 *
 * Mocked rather than real because MapLibre needs a WebGL context, which jsdom
 * does not have. That is not much of a loss: what is worth asserting here is
 * *which calls the component makes*, and the bugs in this file's subject are
 * lifecycle bugs — a map recreated on every render, a controlled camera that
 * oscillates, listeners re-registered until the page stalls. Every one of
 * those is visible in the call log and invisible in a screenshot.
 */

import { act, cleanup, render } from '@testing-library/react';
import { createRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { StyleSpecification } from 'maplibre-gl';

import { WebMap } from './WebMap.js';
import type { MapView, WebMapHandle } from './types.js';

// --- the mock ---------------------------------------------------------------

interface FakeMap {
  setStyle: ReturnType<typeof vi.fn>;
  jumpTo: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
  fitBounds: ReturnType<typeof vi.fn>;
  queryRenderedFeatures: ReturnType<typeof vi.fn>;
  triggerRepaint: ReturnType<typeof vi.fn>;
  getCanvas: ReturnType<typeof vi.fn>;
  project: ReturnType<typeof vi.fn>;
  unproject: ReturnType<typeof vi.fn>;
  addSource: ReturnType<typeof vi.fn>;
  removeSource: ReturnType<typeof vi.fn>;
  getSource: ReturnType<typeof vi.fn>;
  addLayer: ReturnType<typeof vi.fn>;
  removeLayer: ReturnType<typeof vi.fn>;
  getLayer: ReturnType<typeof vi.fn>;
  getFilter: ReturnType<typeof vi.fn>;
  setFilter: ReturnType<typeof vi.fn>;
  setData: ReturnType<typeof vi.fn>;
  sources: Set<string>;
  layers: Set<string>;
  filters: Map<string, unknown>;
  on: ReturnType<typeof vi.fn>;
  off: ReturnType<typeof vi.fn>;
  once: ReturnType<typeof vi.fn>;
  getCenter(): { lng: number; lat: number };
  getZoom(): number;
  getBearing(): number;
  getPitch(): number;
  emit(event: string, payload?: unknown): void;
  camera: MapView;
}

const constructed: Array<{ options: Record<string, unknown>; map: FakeMap }> = [];

function makeFakeMap(options: Record<string, unknown>): FakeMap {
  const listeners = new Map<string, Set<(payload?: unknown) => void>>();
  const camera: MapView = {
    center: options.center as [number, number],
    zoom: options.zoom as number,
    bearing: (options.bearing as number) ?? 0,
    pitch: (options.pitch as number) ?? 0,
  };

  const sources = new Set<string>();
  const layers = new Set<string>(['contours', 'leases']);
  const filters = new Map<string, unknown>([['contours', ['get', 'is_index']]]);
  const setData = vi.fn();

  const map: FakeMap = {
    camera,
    sources,
    layers,
    filters,
    setData,
    // A fixed 100 px per degree, so a projected coordinate is arithmetic a
    // reader can check rather than a Mercator value they have to trust.
    project: vi.fn((lngLat: [number, number]) => ({
      x: lngLat[0] * 100,
      y: lngLat[1] * 100,
    })),
    unproject: vi.fn((point: [number, number]) => ({
      lng: point[0] / 100,
      lat: point[1] / 100,
    })),
    addSource: vi.fn((id: string) => sources.add(id)),
    removeSource: vi.fn((id: string) => sources.delete(id)),
    getSource: vi.fn((id: string) => (sources.has(id) ? { setData } : undefined)),
    addLayer: vi.fn((layer: { id: string }) => layers.add(layer.id)),
    removeLayer: vi.fn((id: string) => layers.delete(id)),
    getLayer: vi.fn((id: string) => (layers.has(id) ? { id } : undefined)),
    getFilter: vi.fn((id: string) => filters.get(id)),
    setFilter: vi.fn((id: string, filter: unknown) => filters.set(id, filter)),
    setStyle: vi.fn(),
    jumpTo: vi.fn((view: MapView) => {
      camera.center = view.center;
      camera.zoom = view.zoom;
      camera.bearing = view.bearing ?? 0;
      camera.pitch = view.pitch ?? 0;
      // Real MapLibre fires movestart/move/moveend synchronously from jumpTo.
      // The fake must too, or the oscillation guard has nothing to guard
      // against and its test passes whether or not the guard exists.
      map.emit('moveend');
    }),
    remove: vi.fn(),
    fitBounds: vi.fn(),
    queryRenderedFeatures: vi.fn(() => [{ id: 7 }]),
    triggerRepaint: vi.fn(),
    getCanvas: vi.fn(() => ({
      toBlob: (callback: (blob: Blob | null) => void) => callback(new Blob(['png'])),
    })),
    on: vi.fn((event: string, handler: (payload?: unknown) => void) => {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event)!.add(handler);
    }),
    off: vi.fn((event: string, handler: (payload?: unknown) => void) => {
      listeners.get(event)?.delete(handler);
    }),
    once: vi.fn((event: string, handler: (payload?: unknown) => void) => {
      const wrapped = (payload?: unknown) => {
        listeners.get(event)?.delete(wrapped);
        handler(payload);
      };
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event)!.add(wrapped);
    }),
    getCenter: () => ({ lng: camera.center[0], lat: camera.center[1] }),
    getZoom: () => camera.zoom,
    getBearing: () => camera.bearing ?? 0,
    getPitch: () => camera.pitch ?? 0,
    emit: (event, payload) => {
      for (const handler of [...(listeners.get(event) ?? [])]) handler(payload);
    },
  };
  return map;
}

vi.mock('maplibre-gl', () => ({
  default: {
    Map: class {
      constructor(options: Record<string, unknown>) {
        const map = makeFakeMap(options);
        constructed.push({ options, map });
        return map as unknown as object;
      }
    },
  },
}));

const STYLE = { version: 8, sources: {}, layers: [] } as unknown as StyleSpecification;
const MIDLAND: MapView = { center: [-102.08, 31.99], zoom: 9.5 };

function latest(): FakeMap {
  const entry = constructed.at(-1);
  if (!entry) throw new Error('no map was constructed');
  return entry.map;
}

beforeEach(() => {
  constructed.length = 0;
});

afterEach(cleanup);

// --- lifecycle --------------------------------------------------------------

describe('instance lifecycle', () => {
  it('creates the map exactly once across re-renders', () => {
    // §2.2: "the commonest MapLibre-in-React bug is recreating the map on
    // every render." It shows up as a map that flickers and loses its camera
    // whenever anything above it changes.
    const { rerender } = render(<WebMap style={STYLE} layerMeta={{}} />);

    rerender(<WebMap style={STYLE} layerMeta={{}} className="a" />);
    rerender(<WebMap style={STYLE} layerMeta={{}} className="b" />);

    expect(constructed).toHaveLength(1);
    expect(latest().remove).not.toHaveBeenCalled();
  });

  it('does not set preserveDrawingBuffer', () => {
    // It costs a second full-size buffer on every pan and zoom, permanently,
    // for something only capture() needs. capture.ts gets it on demand.
    render(<WebMap style={STYLE} layerMeta={{}} />);

    expect(constructed[0]!.options).not.toHaveProperty('preserveDrawingBuffer');
  });

  it('opens at initialView', () => {
    render(<WebMap style={STYLE} layerMeta={{}} initialView={MIDLAND} />);

    expect(constructed[0]!.options).toMatchObject({ center: MIDLAND.center, zoom: 9.5 });
  });

  it('removes the map and its listeners on unmount', () => {
    // A map left behind holds a WebGL context, and browsers cap those at
    // around sixteen. The seventeenth session in a tab renders nothing.
    const { unmount } = render(<WebMap style={STYLE} layerMeta={{}} />);
    const map = latest();

    unmount();

    expect(map.remove).toHaveBeenCalledOnce();
    expect(map.off).toHaveBeenCalledWith('moveend', expect.any(Function));
    expect(map.off).toHaveBeenCalledWith('idle', expect.any(Function));
    expect(map.off).toHaveBeenCalledWith('error', expect.any(Function));
  });

  it('registers each listener once, however often props change', () => {
    // Re-registering on every render leaks handlers rather than instances,
    // which is slower to notice and just as fatal.
    const { rerender } = render(<WebMap style={STYLE} layerMeta={{}} onIdle={() => {}} />);

    rerender(<WebMap style={STYLE} layerMeta={{}} onIdle={() => {}} />);
    rerender(<WebMap style={STYLE} layerMeta={{}} onIdle={() => {}} />);

    const idleRegistrations = latest().on.mock.calls.filter(([event]) => event === 'idle');
    expect(idleRegistrations).toHaveLength(1);
  });

  it('calls the current callback, not the one captured at mount', () => {
    // The reason the props ref exists. Registering once and capturing the
    // first render's callbacks would leave onIdle pointing at a stale closure
    // for the life of the map.
    const stale = vi.fn();
    const current = vi.fn();
    const { rerender } = render(<WebMap style={STYLE} layerMeta={{}} onIdle={stale} />);

    rerender(<WebMap style={STYLE} layerMeta={{}} onIdle={current} />);
    act(() => latest().emit('idle'));

    expect(current).toHaveBeenCalledOnce();
    expect(stale).not.toHaveBeenCalled();
  });
});

// --- style ------------------------------------------------------------------

describe('style updates', () => {
  it('diffs rather than recreating', () => {
    // Recreating would blank the map and refetch every tile on each symbology
    // tweak — and symbology edits arrive every 150 ms while a ramp stop is
    // being dragged (§9).
    const { rerender } = render(<WebMap style={STYLE} layerMeta={{}} />);
    const next = { ...STYLE, layers: [] } as StyleSpecification;

    rerender(<WebMap style={next} layerMeta={{}} />);

    expect(latest().setStyle).toHaveBeenCalledWith(next, { diff: true });
    expect(constructed).toHaveLength(1);
  });

  it('does not reapply an unchanged style object', () => {
    const { rerender } = render(<WebMap style={STYLE} layerMeta={{}} />);
    latest().setStyle.mockClear();

    rerender(<WebMap style={STYLE} layerMeta={{}} className="changed" />);

    expect(latest().setStyle).not.toHaveBeenCalled();
  });
});

// --- camera -----------------------------------------------------------------

describe('camera', () => {
  it('reports user movement through onViewChange', () => {
    const onViewChange = vi.fn();
    render(<WebMap style={STYLE} layerMeta={{}} onViewChange={onViewChange} />);

    act(() => {
      latest().camera.zoom = 12;
      latest().emit('moveend');
    });

    expect(onViewChange).toHaveBeenCalledWith(expect.objectContaining({ zoom: 12 }));
  });

  it('applies a controlled view that differs', () => {
    const { rerender } = render(<WebMap style={STYLE} layerMeta={{}} view={MIDLAND} />);

    rerender(<WebMap style={STYLE} layerMeta={{}} view={{ ...MIDLAND, zoom: 14 }} />);

    expect(latest().jumpTo).toHaveBeenCalledWith(expect.objectContaining({ zoom: 14 }));
  });

  it('ignores a controlled view equal to the current camera', () => {
    // Applying an equal view fights the user mid-drag: the parent re-renders
    // with the view it was told about, and the map jumps back.
    const { rerender } = render(<WebMap style={STYLE} layerMeta={{}} view={MIDLAND} />);
    latest().jumpTo.mockClear();

    rerender(<WebMap style={STYLE} layerMeta={{}} view={{ ...MIDLAND }} />);

    expect(latest().jumpTo).not.toHaveBeenCalled();
  });

  it('does not report its own camera moves back to the parent', () => {
    // **The oscillation.** Without the guard, a controlled map applies the
    // parent's view, the resulting moveend is reported as a user gesture, the
    // parent sets state, and the loop runs until React gives up.
    const onViewChange = vi.fn();
    const { rerender } = render(
      <WebMap style={STYLE} layerMeta={{}} view={MIDLAND} onViewChange={onViewChange} />,
    );

    rerender(
      <WebMap
        style={STYLE}
        layerMeta={{}}
        view={{ ...MIDLAND, zoom: 14 }}
        onViewChange={onViewChange}
      />,
    );
    act(() => latest().emit('moveend'));

    // The moveend that follows a programmatic jump is not a gesture. The one
    // after it, from a real drag, still is.
    expect(onViewChange).toHaveBeenCalledTimes(1);
    expect(onViewChange).toHaveBeenCalledWith(expect.objectContaining({ zoom: 14 }));
  });
});

describe('pointer position', () => {
  it('reports lng/lat on mousemove', () => {
    // The status bar shows coordinates in the analysis CRS on every session,
    // so this is a prop rather than a getMap() call — §2.1 asks for
    // escape-hatch uses to stay rare and countable.
    const onPointerMove = vi.fn();
    render(<WebMap style={STYLE} layerMeta={{}} onPointerMove={onPointerMove} />);

    act(() => latest().emit('mousemove', { lngLat: { lng: -102.08, lat: 31.99 } }));

    expect(onPointerMove).toHaveBeenCalledWith([-102.08, 31.99]);
  });

  it('reports null when the pointer leaves the map', () => {
    // Otherwise the status bar keeps showing the last position the cursor was
    // over, which reads as a live coordinate and is not one.
    const onPointerMove = vi.fn();
    render(<WebMap style={STYLE} layerMeta={{}} onPointerMove={onPointerMove} />);

    act(() => latest().emit('mouseout'));

    expect(onPointerMove).toHaveBeenCalledWith(null);
  });
});

// --- warnings ---------------------------------------------------------------

describe('warnings', () => {
  it.each([
    ['Failed to load glyph range 0-255', 'glyph_missing'],
    ['Unable to load sprite from https://x/sprite', 'sprite_missing'],
    ['AJAX error: 503 for .../10/221/416.mvt', 'tile_failed'],
    ['layers[3]: missing required property "source"', 'style_error'],
  ])('classifies %j as %s', (message, kind) => {
    // The kind is the point. A missing glyph removes every label, which reads
    // as a styling choice; a failed tile leaves a gap, which reads as sparse
    // data. A geologist will believe the second.
    const onWarning = vi.fn();
    render(<WebMap style={STYLE} layerMeta={{}} onWarning={onWarning} />);

    act(() => latest().emit('error', { error: new Error(message) }));

    expect(onWarning).toHaveBeenCalledWith(expect.objectContaining({ kind, message }));
  });

  it('reports an error with no message rather than swallowing it', () => {
    const onWarning = vi.fn();
    render(<WebMap style={STYLE} layerMeta={{}} onWarning={onWarning} />);

    act(() => latest().emit('error', {}));

    expect(onWarning).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining('no message') }),
    );
  });
});

// --- imperative handle ------------------------------------------------------

describe('imperative handle', () => {
  it('forwards fitBounds and queryFeatures to the instance', () => {
    const ref = createRef<WebMapHandle>();
    render(<WebMap ref={ref} style={STYLE} layerMeta={{}} />);

    ref.current!.fitBounds([-103, 31, -101, 33]);
    const features = ref.current!.queryFeatures([10, 10], ['a-fill']);

    expect(latest().fitBounds).toHaveBeenCalledWith([-103, 31, -101, 33], undefined);
    expect(latest().queryRenderedFeatures).toHaveBeenCalledWith([10, 10], { layers: ['a-fill'] });
    expect(features).toHaveLength(1);
  });

  it('captures by rendering on demand rather than from a preserved buffer', async () => {
    const ref = createRef<WebMapHandle>();
    render(<WebMap ref={ref} style={STYLE} layerMeta={{}} />);

    const pending = ref.current!.capture();
    act(() => latest().emit('render'));
    const blob = await pending;

    expect(latest().triggerRepaint).toHaveBeenCalledOnce();
    expect(blob.size).toBeGreaterThan(0);
  });
});

// --- accessibility ----------------------------------------------------------

describe('accessibility', () => {
  it('is focusable and labelled', () => {
    // §10: a map that cannot be panned without a mouse fails the floor.
    // MapLibre binds arrow keys once the container has focus.
    const { container } = render(
      <WebMap style={STYLE} layerMeta={{}} ariaLabel="Map of Wolfcamp A structure" />,
    );

    const element = container.querySelector('[role="application"]')!;
    expect(element.getAttribute('tabindex')).toBe('0');
    expect(element.getAttribute('aria-label')).toBe('Map of Wolfcamp A structure');
  });
});

// --- what editing needs from the map ----------------------------------------

describe('projection', () => {
  it('hands back plain tuples rather than MapLibre points', () => {
    // The snapping engine is written in pixels and imports no MapLibre
    // (`09` §6.1). A handle that returned `maplibregl.Point` would drag the
    // dependency into every module that touches a coordinate.
    const ref = createRef<WebMapHandle>();
    render(<WebMap ref={ref} style={STYLE} layerMeta={{}} initialView={MIDLAND} />);

    expect(ref.current!.project([-102.08, 31.99])).toEqual([-10208, 3199]);
    expect(ref.current!.unproject([-10208, 3199])).toEqual([-102.08, 31.99]);
  });
});

describe('feature queries', () => {
  it('passes a box through, which is what snapping asks for', () => {
    // `09` §6.1 expands the pointer by the larger tolerance and queries once,
    // rather than testing every segment in view.
    const ref = createRef<WebMapHandle>();
    render(<WebMap ref={ref} style={STYLE} layerMeta={{}} initialView={MIDLAND} />);

    ref.current!.queryFeatures(
      [
        [10, 20],
        [30, 40],
      ],
      ['leases'],
    );

    expect(latest().queryRenderedFeatures).toHaveBeenCalledWith(
      [
        [10, 20],
        [30, 40],
      ],
      { layers: ['leases'] },
    );
  });
});

describe('the edit overlay', () => {
  const FEATURE: GeoJSON.Feature = {
    type: 'Feature',
    id: 'lease-1',
    geometry: { type: 'Point', coordinates: [-102.08, 31.99] },
    properties: {},
  };

  const LAYERS = [
    { id: 'edit-points', type: 'circle', source: 'will-be-overwritten' },
  ] as never[];

  function mounted() {
    const ref = createRef<WebMapHandle>();
    render(<WebMap ref={ref} style={STYLE} layerMeta={{}} initialView={MIDLAND} />);
    return ref.current!;
  }

  it('adds the source and layers once, then only pushes data', () => {
    // Re-adding layers per frame would drop and rebuild their GPU buffers,
    // which is the entire cost this arrangement exists to avoid.
    const handle = mounted();
    const map = latest();

    handle.setEditOverlay({ features: [FEATURE], layers: LAYERS });
    handle.setEditOverlay({ features: [FEATURE], layers: LAYERS });
    handle.setEditOverlay({ features: [FEATURE], layers: LAYERS });

    expect(map.addSource).toHaveBeenCalledTimes(1);
    expect(map.addLayer).toHaveBeenCalledTimes(1);
    expect(map.setData).toHaveBeenCalledTimes(2);
  });

  it('overwrites the layer source rather than trusting the caller', () => {
    // A caller pointing an overlay layer at a tile source it is about to
    // filter would hide the very edit it is trying to show.
    const handle = mounted();
    handle.setEditOverlay({ features: [FEATURE], layers: LAYERS });

    expect(latest().addLayer).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'edit-points', source: 'webmap-edit-overlay' }),
    );
  });

  it('hides the stale tile copy of an edited feature', () => {
    // Without this the tile version draws under the overlay, so a dragged
    // boundary shows in both its old and new positions.
    const handle = mounted();
    handle.setEditOverlay({
      features: [FEATURE],
      layers: LAYERS,
      hideFromLayers: ['leases'],
    });

    expect(latest().setFilter).toHaveBeenCalledWith('leases', [
      '!',
      ['in', ['id'], ['literal', ['lease-1']]],
    ]);
  });

  it("combines with the layer's own filter instead of replacing it", () => {
    // `contours` is filtered to index contours in the fixture. Replacing that
    // would bring every intermediate contour back the moment an edit started.
    const handle = mounted();
    handle.setEditOverlay({
      features: [FEATURE],
      layers: LAYERS,
      hideFromLayers: ['contours'],
    });

    expect(latest().setFilter).toHaveBeenCalledWith('contours', [
      'all',
      ['get', 'is_index'],
      ['!', ['in', ['id'], ['literal', ['lease-1']]]],
    ]);
  });

  it('restores the original filter when the overlay is cleared', () => {
    const handle = mounted();
    const map = latest();

    handle.setEditOverlay({
      features: [FEATURE],
      layers: LAYERS,
      hideFromLayers: ['contours'],
    });
    handle.setEditOverlay(null);

    expect(map.filters.get('contours')).toEqual(['get', 'is_index']);
    expect(map.removeLayer).toHaveBeenCalledWith('edit-points');
    expect(map.removeSource).toHaveBeenCalledWith('webmap-edit-overlay');
  });

  it('stops filtering a layer the next overlay does not name', () => {
    // The bookkeeping rule: nothing stays filtered that the current overlay
    // did not ask for. A layer left filtered after its feature was saved is a
    // feature that has silently vanished from the map.
    const handle = mounted();
    const map = latest();

    handle.setEditOverlay({
      features: [FEATURE],
      layers: LAYERS,
      hideFromLayers: ['leases'],
    });
    handle.setEditOverlay({ features: [FEATURE], layers: LAYERS });

    expect(map.filters.get('leases')).toBeUndefined();
  });
});
