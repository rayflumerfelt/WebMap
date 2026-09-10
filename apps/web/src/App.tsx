/**
 * The application. `07-frontend.md` §5.2.
 *
 * Composition only: state lives in the session store, layout in `AppShell`,
 * and every panel is a component that could be mounted somewhere else. §5.5
 * asks for detachable panels later, and that stays cheap only if nothing here
 * assumes a parent layout context.
 */

import { compileStyle, deriveLegend } from '@webmap/style-model';
import type { LegendSpec, Palette } from '@webmap/style-model';
import { WebMap } from '@webmap/map';
import { LayerTree, Legend, NorthArrow, ScaleBar } from '@webmap/ui';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AttributePanel } from './attributes/AttributePanel.js';
import { editMapImages } from './editing/overlay.js';
import { fetchWorkingSet } from './api/features.js';
import { discardEdits, pendingCount, saveEdits } from './editing/persistence.js';
import { useMapEditing } from './editing/useMapEditing.js';
import { ApiClient } from './api/client.js';
import { cursorTransform } from './crs/analysisCrs.js';
import { useShortcuts } from './keyboard/useShortcuts.js';
import type { Command } from './keyboard/shortcuts.js';
import { AppShell } from './shell/AppShell.js';
import { StatusBar, scaleDenominatorFor } from './shell/StatusBar.js';
import { Toolbar } from './shell/Toolbar.js';
import type { ToolId } from './shell/Toolbar.js';
import { DEFAULT_PREFS, loadPrefs, savePrefs } from './shell/panelPrefs.js';
import type { PanelKey, PanelPrefs } from './shell/panelPrefs.js';
import { FormattingDialogContainer } from './symbology/FormattingDialogContainer.js';
import { groupIntoFamilies } from './symbology/fontFamilies.js';
import { usePalettes } from './api/sessions.js';
import { useEditStore } from './stores/editStore.js';
import { useSessionStore } from './stores/sessionStore.js';

/** Built once: nine small RGBA buffers, and rebuilding them per render would
 *  hand MapLibre a new image identity on every frame. */
const EDIT_IMAGES = editMapImages();

export interface AppProps {
  /** Injected so the shell is renderable without a live API — the session
   *  route supplies the real one. */
  palettes?: Record<string, Palette>;
  /** Tile URL template for a dataset. The app owns API base URLs and tokens;
   *  `@webmap/style-model` knows nothing about either. */
  tileUrlFor?(datasetId: string): string;
  projectName?: string;
  /** Analysis CRS label for the status bar, e.g. "EPSG:2277 · …". */
  crsLabel?: string;
  analysisUnit?: 'ft' | 'usft' | 'm';
  /** `project.crs_wkt`. Without it the status bar shows no coordinates rather
   *  than showing longitude/latitude and implying they are the analysis CRS. */
  crsWkt?: string;
  /** Supplied by the session route. The shell renders without one, which is
   *  what lets it be developed and tested with no API in reach. */
  api?: ApiClient;
  /** Feature count per dataset, from session metadata. Decides whether the
   *  attribute panel asks for attributes at all. */
  featureCounts?: Record<string, number | null>;
  /** Columns per dataset, for the formatting dialog's field pickers. From the
   *  dataset's registered `attribute_schema`. */
  attributeSchemas?: Record<string, Array<{ name: string; type: 'text' | 'number' }>>;
  /**
   * Dataset version per dataset id, from session metadata.
   *
   * Editing cannot start without it: `09` §5.3 puts optimistic concurrency on
   * this pointer and nothing else, so a session opened against a guessed
   * version would overwrite whatever another user saved in the meantime. A
   * dataset missing from this map simply cannot be edited yet, and the tool
   * says so rather than opening a session it cannot safely save.
   */
  datasetVersions?: Record<string, number>;
  /** Per-dataset tile token, for the endpoints that take one instead of a
   *  bearer header (`03` §4.4) — the tiles themselves and the working set the
   *  editor reads exact geometry from. */
  tileTokenFor?(datasetId: string): string | undefined;
  /** Font **stack** names from `GET /static/glyphs` — grouped into families
   *  here, because a stack is what MapLibre asks for and a family is what a
   *  person picks. Injected rather than fetched so the shell renders with no
   *  API in reach. */
  glyphStacks?: string[];
}

export function App({
  palettes = {},
  tileUrlFor = (id) => `/api/v1/tiles/${id}/{z}/{x}/{y}.mvt`,
  projectName = 'WebMap',
  crsLabel = 'EPSG:2277 · NAD83 / Texas Central (ftUS)',
  analysisUnit = 'usft',
  crsWkt,
  api,
  featureCounts = {},
  attributeSchemas = {},
  glyphStacks = [],
  datasetVersions = {},
  tileTokenFor,
}: AppProps = {}) {
  const layers = useSessionStore((state) => state.layers);
  const view = useSessionStore((state) => state.view);
  const selectedLayerId = useSessionStore((state) => state.selectedLayerId);
  const dirty = useSessionStore((state) => state.dirty);
  const conflict = useSessionStore((state) => state.conflict);
  const lastSavedAt = useSessionStore((state) => state.lastSavedAt);
  const sessionName = useSessionStore((state) => state.shortCode);

  const [prefs, setPrefs] = useState<PanelPrefs>(DEFAULT_PREFS);
  const fontFamilies = useMemo(() => groupIntoFamilies(glyphStacks), [glyphStacks]);

  // Stored palettes merged over the injected ones, so the shell still renders
  // with no API in reach and a live session sees what the deployment has.
  const storedPalettes = usePalettes(api);
  const allPalettes = useMemo<Record<string, Palette>>(() => {
    const merged: Record<string, Palette> = { ...palettes };
    for (const entry of storedPalettes.data?.items ?? []) {
      merged[entry.id] = entry;
    }
    return merged;
  }, [palettes, storedPalettes.data]);
  const [activeTool, setActiveTool] = useState<ToolId>('tool.select');
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);

  // Built once per CRS, not per mouse move: proj4 parses the definition on
  // construction, and doing that thousands of times a second while panning
  // would be the most expensive thing on the page.
  const toAnalysisCrs = useMemo(() => (crsWkt ? cursorTransform(crsWkt) : null), [crsWkt]);
  const mapRef = useRef<React.ComponentRef<typeof WebMap>>(null);

  // Read once on mount rather than during render: `loadPrefs` touches
  // localStorage, and a render that reads storage is a render that can throw
  // in a browser with site data blocked.
  useEffect(() => setPrefs(loadPrefs()), []);

  const updatePrefs = useCallback((next: PanelPrefs) => {
    setPrefs(next);
    savePrefs(next);
  }, []);

  const togglePanel = useCallback(
    (key: PanelKey) =>
      setPrefs((current) => {
        const next = { ...current, [key]: { ...current[key], collapsed: !current[key].collapsed } };
        savePrefs(next);
        return next;
      }),
    [],
  );

  // **The style is derived, never stored** (§3.1). Recomputed from the layers
  // and palettes, so there is nowhere for a stale compiled style to live.
  const style = useMemo(
    () =>
      compileStyle({
        layers: layers.map((layer, index) => ({
          id: layer.id,
          source: {
            datasetId: layer.datasetId,
            kind: 'vector' as const,
            url: tileUrlFor(layer.datasetId),
            sourceLayer: 'features',
          },
          symbology: layer.symbology,
          opacity: layer.opacity,
          visible: layer.visible,
          z: index,
        })),
        palettes: allPalettes,
      }),
    [layers, allPalettes, tileUrlFor],
  );

  const selectedLayer = layers.find((layer) => layer.id === selectedLayerId) ?? null;

  // --- editing ---------------------------------------------------------------

  const editLayerId = useEditStore((state) => state.mode.activeLayerId);
  // Recomputed whenever the store changes, which includes every bump of the
  // session's revision counter — the session itself is mutated in place, so
  // its identity says nothing.
  const editCount = useEditStore((state) => state.session?.dirty.size ?? 0);
  const [editError, setEditError] = useState<string | null>(null);

  // The compiled layers drawing the layer being edited. `compileStyle` gives
  // each session layer one source named after it and any number of layers on
  // top — fill, line and labels are three — so the source is what identifies
  // them, not the id prefix.
  const editBaseLayerIds = useMemo(() => {
    if (!editLayerId) return [];
    return style.layers
      .filter((layer) => 'source' in layer && layer.source === `src-${editLayerId}`)
      .map((layer) => layer.id);
  }, [style, editLayerId]);

  // The dataset the edit session's version pointer belongs to. A layer is a
  // view of a dataset (`adr/0010`), and the version lives on the dataset.
  const editDatasetId = layers.find((layer) => layer.id === editLayerId)?.datasetId ?? null;

  const editing = useMapEditing({
    map: mapRef,
    baseLayerIds: editBaseLayerIds,
    enabled: activeTool === 'tool.edit',
    onError: setEditError,
  });

  /**
   * Write the edit session's pending changes.
   *
   * Only the *editor's* save. The session document — layers, camera,
   * symbology — autosaves on its own debounce (§3), and conflating the two
   * would make `mod+s` mean two different things depending on which panel had
   * focus.
   */
  const saveEditSession = useCallback(async () => {
    if (pendingCount() === 0) return;
    if (!api || !editDatasetId) {
      setEditError('Editing needs a live session: there is no API client to save through.');
      return;
    }

    const outcome = await saveEdits(api, editDatasetId);
    if (outcome.status === 'saved') {
      setEditError(null);
      return;
    }
    if (outcome.status === 'conflict') {
      // §5.3: the edits are still in the buffer. Refresh and Force are the
      // two answers, and neither is built — saying so is better than a
      // message that implies the save merely needs retrying.
      setEditError(
        `${outcome.message} Your edits are still here. Rebasing them onto the ` +
          `newer version is not built yet — copy anything you cannot redo.`,
      );
      return;
    }
    if (outcome.status === 'failed') setEditError(outcome.message);
  }, [api, editDatasetId]);

  /**
   * Open an edit session on the selected layer.
   *
   * Refused without a dataset version: §5.3 puts optimistic concurrency on
   * that pointer and nothing else, so a session opened against a guessed one
   * would overwrite whatever somebody else saved in the meantime.
   */
  const startEditing = useCallback(() => {
    const store = useEditStore.getState();
    if (!selectedLayer) {
      setEditError('Select a layer in the layer tree before editing.');
      return false;
    }
    if (store.mode.activeLayerId === selectedLayer.id) return true;

    const version = datasetVersions[selectedLayer.datasetId];
    if (version === undefined) {
      setEditError(
        `'${selectedLayer.name}' cannot be edited yet: its dataset version has ` +
          `not loaded. Editing needs it to detect a save someone else made first.`,
      );
      return false;
    }

    try {
      store.activateLayer({
        layerId: selectedLayer.id,
        baseVersion: version,
        geometry: null,
        canEdit: true,
      });
      // The working set is fetched rather than awaited: the toolbar goes live
      // at once and the handles appear when the geometry lands. §17 caps it at
      // 5,000 features and the endpoint refuses above that rather than
      // truncating — a silently partial layer looks like the data.
      if (api) {
        void fetchWorkingSet(api, selectedLayer.datasetId, tileTokenFor?.(selectedLayer.datasetId))
          .then((features) => useEditStore.getState().loadWorkingSet(features))
          .catch((error: unknown) =>
            setEditError(
              `'${selectedLayer.name}' cannot be edited: ` +
                `${error instanceof Error ? error.message : String(error)}`,
            ),
          );
      }
    } catch (error) {
      // Unsaved edits on another layer. §5.2 makes that the user's decision,
      // so the message says so rather than discarding them.
      setEditError(error instanceof Error ? error.message : String(error));
      return false;
    }
    setEditError(null);
    return true;
  }, [api, datasetVersions, selectedLayer, tileTokenFor]);

  const legendSpec = useMemo<LegendSpec | null>(() => {
    if (!selectedLayer) return null;
    try {
      return deriveLegend(
        selectedLayer.symbology,
        { name: selectedLayer.name },
        allPalettes,
      );
    } catch {
      // A symbology whose palette is missing must not blank the map along
      // with its legend. The map still draws; the legend does not, and the
      // reason surfaces where the layer fails to compile.
      return null;
    }
  }, [allPalettes, selectedLayer]);

  const runCommand = useCallback(
    (command: Command) => {
      const store = useSessionStore.getState();
      switch (command) {
        case 'tool.edit':
          if (startEditing()) setActiveTool(command);
          return;
        case 'tool.select':
        case 'tool.identify':
        case 'tool.measure':
          setActiveTool(command);
          return;
        case 'session.save':
          void saveEditSession();
          return;
        case 'edit.undo':
          useEditStore.getState().undo();
          return;
        case 'edit.redo':
          useEditStore.getState().redo();
          return;
        case 'tool.cancel':
          // The editor gets the press first: §4's two-press rule gives the
          // first Escape to a drag in flight, and only the second leaves the
          // tool.
          if (editing.onEscape()) return;
          setActiveTool('tool.select');
          return;
        case 'panel.layers.toggle':
          togglePanel('layers');
          return;
        case 'panel.symbology.toggle':
          togglePanel('symbology');
          return;
        case 'panel.attributes.toggle':
          togglePanel('attributes');
          return;
        case 'view.zoomToLayer':
          // Nothing to zoom to without a bbox on the layer; the session route
          // supplies one once dataset metadata is loaded.
          return;
        default:
          // search, palette and render are wired in the session route, which
          // has the API client. Falling through here rather than throwing: an
          // unhandled command is a missing feature, not a crash.
          void store;
          return;
      }
    },
    [editing, saveEditSession, startEditing, togglePanel],
  );

  useShortcuts(runCommand);

  return (
    <AppShell
      prefs={prefs}
      onPrefsChange={updatePrefs}
      toolbar={
        <Toolbar
          projectName={projectName}
          sessionName={sessionName}
          activeTool={activeTool}
          // The edit tool needs something to edit. Permission is a separate
          // question the session's dataset metadata answers, and the store's
          // `canEdit` carries it once a session is open — greying the tool for
          // an unselected layer is what stops the first click being a refusal.
          canEdit={selectedLayer !== null}
          onCommand={runCommand}
        />
      }
      layers={
        <LayerTree
          layers={layers}
          palettes={allPalettes}
          selectedId={selectedLayerId}
          onSelect={(id) => useSessionStore.getState().select(id)}
          onReorder={(from, to) => useSessionStore.getState().reorderLayers(from, to)}
          onToggleVisibility={(id) => useSessionStore.getState().toggleVisibility(id)}
          onOpacityChange={(id, opacity) => useSessionStore.getState().setOpacity(id, opacity)}
          onRemove={(id) => useSessionStore.getState().removeLayer(id)}
        />
      }
      map={
        <>
          <WebMap
            ref={mapRef}
            style={style}
            view={view}
            layerMeta={{}}
            ariaLabel={`Map of ${projectName}`}
            images={EDIT_IMAGES}
            onMapPointer={editing.onMapPointer}
            onViewChange={(next) => useSessionStore.getState().setView(next)}
            onPointerMove={(lngLat) =>
              setCursor(lngLat && toAnalysisCrs ? toAnalysisCrs(lngLat) : null)
            }
          />
          {/* Overlays are HTML above the canvas, so the headless renderer's
              page screenshot captures them (§2). */}
          <div style={overlayCorner('bottom-left')}>
            <ScaleBar latitude={view.center[1]} zoom={view.zoom} unit="imperial" />
          </div>
          <div style={overlayCorner('top-right')}>
            <NorthArrow bearing={view.bearing ?? 0} />
          </div>
          {editError ? (
            <div role="status" style={editBanner}>
              {editError}
              {editCount > 0 ? (
                // §5.2 asks the user to save or discard, so the message that
                // asks is where Discard belongs. It is the one action in the
                // editor that cannot be undone, hence the confirm.
                <button
                  type="button"
                  style={dismiss}
                  onClick={() => {
                    const plural = editCount === 1 ? 'edit' : 'edits';
                    if (
                      globalThis.confirm?.(
                        `Discard ${editCount} unsaved ${plural}? This cannot be undone.`,
                      )
                    ) {
                      discardEdits();
                      setEditError(null);
                    }
                  }}
                >
                  Discard {editCount} unsaved {editCount === 1 ? 'edit' : 'edits'}
                </button>
              ) : null}
              <button type="button" onClick={() => setEditError(null)} style={dismiss}>
                Dismiss
              </button>
            </div>
          ) : null}
          {legendSpec ? (
            <div style={overlayCorner('bottom-right')}>
              <Legend spec={legendSpec} />
            </div>
          ) : null}
        </>
      }
      symbology={
        selectedLayer && selectedLayerId ? (
          <FormattingDialogContainer
            api={api}
            datasetId={selectedLayer.datasetId}
            layerName={selectedLayer.name}
            symbology={selectedLayer.symbology}
            onChange={(symbology) =>
              useSessionStore.getState().updateSymbology(selectedLayerId, symbology)
            }
            fields={attributeSchemas[selectedLayer.datasetId] ?? []}
            palettes={allPalettes}
            fonts={fontFamilies}
          />
        ) : (
          <p style={{ fontSize: 12, opacity: 0.7, margin: 12 }}>
            Select a layer to edit how it is drawn.
          </p>
        )
      }
      attributes={
        api ? (
          <AttributePanel
            api={api}
            datasetId={selectedLayer?.datasetId ?? null}
            layerName={selectedLayer?.name ?? null}
            featureCount={
              selectedLayer ? (featureCounts[selectedLayer.datasetId] ?? null) : null
            }
            height={prefs.attributes.width}
          />
        ) : undefined
      }
      statusBar={
        <StatusBar
          crsLabel={crsLabel}
          cursor={cursor}
          unit={analysisUnit}
          scaleDenominator={scaleDenominatorFor(view.center[1], view.zoom)}
          save={{ dirty, conflict, lastSavedAt }}
        />
      }
    />
  );
}

/** A refused edit, above the map. Not a toast: the reasons here are things the
 *  user has to act on — save the other layer, wait for metadata — and a message
 *  that vanishes on its own is one they will act on twice. */
const editBanner: React.CSSProperties = {
  position: 'absolute',
  zIndex: 2,
  top: 8,
  left: '50%',
  transform: 'translateX(-50%)',
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  maxWidth: 640,
  padding: '6px 10px',
  borderRadius: 3,
  border: '1px solid #d9a441',
  background: '#fdf6e3',
  fontSize: 12,
  color: '#4a3c1a',
};

const dismiss: React.CSSProperties = {
  border: 0,
  background: 'transparent',
  color: '#7a5c12',
  cursor: 'pointer',
  fontSize: 12,
  textDecoration: 'underline',
};

function overlayCorner(corner: 'bottom-left' | 'bottom-right' | 'top-right'): React.CSSProperties {
  const base: React.CSSProperties = { position: 'absolute', zIndex: 1, pointerEvents: 'none' };
  if (corner === 'bottom-left') return { ...base, left: 8, bottom: 8 };
  if (corner === 'bottom-right') return { ...base, right: 8, bottom: 8 };
  return { ...base, right: 8, top: 8 };
}
