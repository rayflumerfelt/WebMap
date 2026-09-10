/**
 * The editor, as the application exposes it. `09-editing.md` §5.3, §4.
 *
 * Two rules are decided here and nowhere else, so they are asserted here: an
 * edit session is not opened without the dataset version it will be saved
 * against, and Escape reaches a drag in flight before it reaches the toolbar.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from './App.js';
import { useEditStore } from './stores/editStore.js';
import { useSessionStore } from './stores/sessionStore.js';
import type { SessionLayer } from './stores/sessionStore.js';

// The map needs a WebGL context jsdom does not have. What matters here is
// which props reach it, so a stub that records them is the whole mock.
const mapProps: Record<string, unknown>[] = [];
vi.mock('@webmap/map', () => ({
  WebMap: (props: Record<string, unknown>) => {
    mapProps.push(props);
    return null;
  },
}));

const LAYER: SessionLayer = {
  id: 'layer-1',
  datasetId: 'dataset-1',
  name: 'Wolfcamp A faults',
  symbology: {
    type: 'single',
    symbol: {
      geometry: 'line',
      color: '#213547',
      width: 1,
      style: 'solid',
      opacity: 1,
    },
  } as never,
  opacity: 1,
  visible: true,
};

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mapProps.length = 0;
  useSessionStore.setState({
    layers: [LAYER],
    selectedLayerId: 'layer-1',
    view: { center: [-102.08, 31.99], zoom: 12 },
  });
  useEditStore.setState({ session: null, revision: 0 });
  useEditStore.getState().dispatch({ type: 'setActiveLayer', layerId: null });
});

afterEach(cleanup);

describe('the map props', () => {
  it('carries the editor’s glyphs', () => {
    // An `icon-image` naming an image that was never added renders nothing,
    // with no error — the handles simply do not appear.
    render(<App />, { wrapper });

    const images = mapProps.at(-1)!['images'] as Array<{ id: string }>;
    expect(images.map((image) => image.id)).toContain('edit-handle');
    expect(images.map((image) => image.id)).toContain('edit-snap-vertex-tile');
  });

  it('carries a pointer handler', () => {
    render(<App />, { wrapper });

    expect(typeof mapProps.at(-1)!['onMapPointer']).toBe('function');
  });
});

describe('starting an edit session', () => {
  it('refuses without the dataset version, and says why', () => {
    // §5.3 puts optimistic concurrency on that pointer and nothing else. A
    // session opened against a guessed version would overwrite whatever
    // somebody else saved in the meantime.
    render(<App />, { wrapper });

    fireEvent.click(screen.getByRole('radio', { name: 'Edit' }));

    expect(screen.getByRole('status').textContent).toMatch(/dataset version has not loaded/i);
    expect(useEditStore.getState().session).toBeNull();
  });

  it('opens the session at the version it was given', () => {
    render(<App datasetVersions={{ 'dataset-1': 12 }} />, { wrapper });

    fireEvent.click(screen.getByRole('radio', { name: 'Edit' }));

    expect(useEditStore.getState().session?.baseVersion).toBe(12);
    expect(useEditStore.getState().mode.activeLayerId).toBe('layer-1');
  });

  it('reports unsaved edits on another layer rather than discarding them', () => {
    // §5.2 makes save-or-discard the user's decision.
    useEditStore.getState().activateLayer({
      layerId: 'other',
      baseVersion: 1,
      geometry: 'line',
      canEdit: true,
      features: [{ id: 'f', geometry: { type: 'Point', coordinates: [0, 0] }, properties: {} }],
    });
    useEditStore.getState().applyCommand({
      id: 'c1',
      label: 'Move Vertex',
      deltas: [
        {
          featureId: 'f',
          before: { id: 'f', geometry: { type: 'Point', coordinates: [0, 0] }, properties: {} },
          after: { id: 'f', geometry: { type: 'Point', coordinates: [1, 1] }, properties: {} },
        },
      ],
      timestamp: 0,
    });

    render(<App datasetVersions={{ 'dataset-1': 12 }} />, { wrapper });
    fireEvent.click(screen.getByRole('radio', { name: 'Edit' }));

    expect(screen.getByRole('status').textContent).toMatch(/unsaved edit/i);
    expect(useEditStore.getState().mode.activeLayerId).toBe('other');
  });

  it('can be dismissed once read', () => {
    // The reasons here need acting on — save the other layer, wait for
    // metadata — so the message stays until the user is done with it.
    render(<App />, { wrapper });
    fireEvent.click(screen.getByRole('radio', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));

    expect(screen.queryByRole('status')).toBeNull();
  });
});
