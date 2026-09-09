/**
 * What a Claude link shows for the second before the session resolves.
 * `07-frontend.md` §8.
 *
 * The shell's chrome, drawn immediately, with the map area blank. A spinner on
 * an empty page tells the reader nothing about where they are going; the
 * chrome tells them they arrived somewhere that is loading, and — because the
 * panel widths come from their own stored preferences — it does not then jump
 * into a different layout when the data lands.
 */

import { loadPrefs } from '../shell/panelPrefs.js';

export interface MapSkeletonProps {
  shortCode: string;
}

export function MapSkeleton({ shortCode }: MapSkeletonProps) {
  const prefs = loadPrefs();

  return (
    <div
      className="app-shell"
      aria-busy="true"
      style={{
        gridTemplateRows: 'var(--toolbar-h, 44px) 1fr var(--statusbar-h, 24px)',
        height: '100vh',
        overflow: 'hidden',
      }}
    >
      <div style={barStyle}>
        <span style={{ fontWeight: 600 }}>WebMap</span>
        <span style={{ opacity: 0.7 }}>/s/{shortCode}</span>
      </div>

      <div style={{ display: 'flex', minHeight: 0 }}>
        <Placeholder width={prefs.layers.collapsed ? 40 : prefs.layers.width} side="left" />
        <div
          role="status"
          style={{
            flex: 1,
            display: 'grid',
            placeItems: 'center',
            fontSize: 12,
            opacity: 0.7,
          }}
        >
          Loading session…
        </div>
        <Placeholder
          width={prefs.symbology.collapsed ? 40 : prefs.symbology.width}
          side="right"
        />
      </div>

      <div style={{ ...barStyle, borderTop: '1px solid var(--chrome-border)', borderBottom: 0 }} />
    </div>
  );
}

function Placeholder({ width, side }: { width: number; side: 'left' | 'right' }) {
  return (
    <div
      aria-hidden="true"
      style={{
        width,
        flex: `0 0 ${width}px`,
        background: 'var(--chrome-bg)',
        borderRight: side === 'left' ? '1px solid var(--chrome-border)' : undefined,
        borderLeft: side === 'right' ? '1px solid var(--chrome-border)' : undefined,
      }}
    />
  );
}

const barStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '0 8px',
  fontSize: 12,
  borderBottom: '1px solid var(--chrome-border)',
  background: 'var(--chrome-bg)',
};
