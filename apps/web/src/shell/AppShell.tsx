/**
 * The docked application shell. `07-frontend.md` §5.2.
 *
 * Single-document application: the map is the document and everything else
 * docks around it. Panels take width from the map rather than covering it,
 * because the map is what the geologist is reading.
 *
 * ```
 * ┌──────────────────────────────────────────────────────┐
 * │  toolbar                                             │ 44px
 * ├────────┬────────────────────────────────┬────────────┤
 * │ LAYERS │              MAP               │ SYMBOLOGY  │
 * │        ├────────────────────────────────┤            │
 * │        │  ATTRIBUTE TABLE (resizable)   │            │
 * ├────────┴────────────────────────────────┴────────────┤
 * │  status: CRS · cursor · scale · job                  │ 24px
 * └──────────────────────────────────────────────────────┘
 * ```
 *
 * Below 1280 px this renders the minimum-width notice instead of a degraded
 * layout (§5.1). That is a policy, not an oversight: a reflowed layout would
 * imply small viewports are supported, and they are not.
 */

import type { ReactNode } from 'react';

import { DockedPanel } from './DockedPanel.js';
import type { PanelKey, PanelPrefs } from './panelPrefs.js';

export interface AppShellProps {
  toolbar: ReactNode;
  /** A second bar under the main one — the edit toolbar of `09` §10.1, which
   *  is present only while editing. The rows size to content, so its absence
   *  costs nothing. */
  secondaryToolbar?: ReactNode;
  layers: ReactNode;
  map: ReactNode;
  symbology: ReactNode;
  attributes?: ReactNode;
  statusBar: ReactNode;
  prefs: PanelPrefs;
  onPrefsChange(prefs: PanelPrefs): void;
}

export function AppShell(props: AppShellProps) {
  const { prefs, onPrefsChange } = props;

  const update = (key: PanelKey, patch: Partial<PanelPrefs[PanelKey]>) =>
    onPrefsChange({ ...prefs, [key]: { ...prefs[key], ...patch } });

  return (
    <>
      {/* Rendered unconditionally and hidden by a min-width query rather than
          switched in JavaScript: the notice must appear before hydration, and
          `07-frontend.md` §5.1 forbids a max-width query for the inverse. */}
      <div className="min-width-notice">
        <div style={{ maxWidth: 420, padding: 24, textAlign: 'center' }}>
          <h1 style={{ fontSize: 16, marginBottom: 8 }}>WebMap needs a wider window</h1>
          <p style={{ fontSize: 13, lineHeight: 1.5 }}>
            This application is built for a workstation display and needs a window at
            least 1280 px wide. Widen the window or move it to a larger monitor.
          </p>
        </div>
      </div>

      <div
        className="app-shell"
        style={{
          // `auto` rather than fixed heights: the bars size themselves, which
          // is what lets a second one appear while editing without the map
          // overflowing the viewport.
          gridTemplateRows: 'auto auto 1fr auto',
          height: '100vh',
          overflow: 'hidden',
        }}
      >
        {props.toolbar}
        {props.secondaryToolbar}

        <div style={{ display: 'flex', minHeight: 0, minWidth: 0 }}>
          <DockedPanel
            title="Layers"
            side="left"
            width={prefs.layers.width}
            collapsed={prefs.layers.collapsed}
            onWidthChange={(width) => update('layers', { width })}
            onCollapsedChange={(collapsed) => update('layers', { collapsed })}
          >
            {props.layers}
          </DockedPanel>

          {/* The map and the attribute table share the centre column: the
              table docks under the map rather than over it, so it never hides
              the features whose rows it is showing. */}
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
            <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>{props.map}</div>
            {props.attributes && !prefs.attributes.collapsed ? (
              <div
                style={{
                  height: prefs.attributes.width,
                  borderTop: '1px solid var(--chrome-border)',
                  overflow: 'auto',
                  background: 'var(--chrome-bg)',
                }}
              >
                {props.attributes}
              </div>
            ) : null}
          </div>

          <DockedPanel
            title="Symbology"
            side="right"
            width={prefs.symbology.width}
            collapsed={prefs.symbology.collapsed}
            onWidthChange={(width) => update('symbology', { width })}
            onCollapsedChange={(collapsed) => update('symbology', { collapsed })}
          >
            {props.symbology}
          </DockedPanel>
        </div>

        {props.statusBar}
      </div>
    </>
  );
}
