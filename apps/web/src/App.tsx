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
import { FormattingDialog } from './symbology/FormattingDialog.js';
import { groupIntoFamilies } from './symbology/fontFamilies.js';
import { useSessionStore } from './stores/sessionStore.js';

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
        palettes,
      }),
    [layers, palettes, tileUrlFor],
  );

  const selectedLayer = layers.find((layer) => layer.id === selectedLayerId) ?? null;

  const legendSpec = useMemo<LegendSpec | null>(() => {
    if (!selectedLayer) return null;
    try {
      return deriveLegend(selectedLayer.symbology, { name: selectedLayer.name }, palettes);
    } catch {
      // A symbology whose palette is missing must not blank the map along
      // with its legend. The map still draws; the legend does not, and the
      // reason surfaces where the layer fails to compile.
      return null;
    }
  }, [palettes, selectedLayer]);

  const runCommand = useCallback(
    (command: Command) => {
      const store = useSessionStore.getState();
      switch (command) {
        case 'tool.select':
        case 'tool.identify':
        case 'tool.measure':
        case 'tool.edit':
          setActiveTool(command);
          return;
        case 'tool.cancel':
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
          // session.save, edit.undo/redo, search, palette and render are wired
          // in the session route, which has the API client. Falling through
          // here rather than throwing: an unhandled command is a missing
          // feature, not a crash.
          void store;
          return;
      }
    },
    [togglePanel],
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
          onCommand={runCommand}
        />
      }
      layers={
        <LayerTree
          layers={layers}
          palettes={palettes}
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
          {legendSpec ? (
            <div style={overlayCorner('bottom-right')}>
              <Legend spec={legendSpec} />
            </div>
          ) : null}
        </>
      }
      symbology={
        selectedLayer && selectedLayerId ? (
          <FormattingDialog
            layerName={selectedLayer.name}
            symbology={selectedLayer.symbology}
            onChange={(symbology) =>
              useSessionStore.getState().updateSymbology(selectedLayerId, symbology)
            }
            fields={attributeSchemas[selectedLayer.datasetId] ?? []}
            palettes={palettes}
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

function overlayCorner(corner: 'bottom-left' | 'bottom-right' | 'top-right'): React.CSSProperties {
  const base: React.CSSProperties = { position: 'absolute', zIndex: 1, pointerEvents: 'none' };
  if (corner === 'bottom-left') return { ...base, left: 8, bottom: 8 };
  if (corner === 'bottom-right') return { ...base, right: 8, bottom: 8 };
  return { ...base, right: 8, top: 8 };
}
