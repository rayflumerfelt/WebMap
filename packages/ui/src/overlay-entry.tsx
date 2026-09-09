/**
 * The overlay bundle the render shell loads. `06-rendering.md` §4, §9.
 *
 * **One implementation of the legend, not two.** This is the reason the render
 * service screenshots the *page* rather than the canvas: the legend, scale bar
 * and north arrow in a rendered PNG are the same React components the
 * interactive map mounts, so a map on a slide and the same map on screen
 * cannot disagree about what a colour means.
 *
 * A second, "just for rendering" legend would drift within a month, and the
 * drift would be invisible until someone put both in front of a partner.
 *
 * Built to `apps/render/shell/overlay.js` as a UMD bundle exposing
 * `window.WebMapOverlay`. The shell is served from `file://` and fetches
 * nothing, so everything it needs must be in that file.
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import type { Root } from 'react-dom/client';

import { Legend } from './Legend/Legend.js';
import { NorthArrow } from './NorthArrow/NorthArrow.js';
import { ScaleBar } from './ScaleBar/ScaleBar.js';
import type { LegendProps } from './Legend/Legend.js';
import type { NorthArrowProps } from './NorthArrow/NorthArrow.js';
import type { ScaleBarProps } from './ScaleBar/ScaleBar.js';

export interface OverlaySpec {
  legend?: LegendProps['spec'];
  scaleBar?: Pick<ScaleBarProps, 'latitude' | 'zoom' | 'unit'>;
  northArrow?: Pick<NorthArrowProps, 'bearing' | 'always'>;
  /** Title block. Shown top-left, above everything else. */
  title?: { text: string; subtitle?: string };
  /** Provenance stamp — method, date, grid spacing. Small and cornered.
   *  "Cheap, and means the map defends itself once it is out of our hands"
   *  (`06-rendering.md` §9). */
  provenance?: string;
}

function Overlay({ spec }: { spec: OverlaySpec }) {
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        // The overlay must never intercept a pointer: in the render shell
        // nothing clicks, but the same bundle could be mounted over a live
        // map and swallowing drags would be maddening.
        pointerEvents: 'none',
        fontFamily: 'Inter, system-ui, sans-serif',
      }}
    >
      {spec.title ? (
        <div style={corner('top-left')}>
          <div
            style={{
              padding: '6px 10px',
              background: 'rgba(255, 255, 255, 0.9)',
              borderRadius: 2,
              color: '#242e39',
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 600 }}>{spec.title.text}</div>
            {spec.title.subtitle ? (
              <div style={{ fontSize: 11, opacity: 0.8 }}>{spec.title.subtitle}</div>
            ) : null}
          </div>
        </div>
      ) : null}

      {spec.northArrow ? (
        <div style={corner('top-right')}>
          <NorthArrow {...spec.northArrow} />
        </div>
      ) : null}

      {spec.scaleBar ? (
        <div style={corner('bottom-left')}>
          <ScaleBar {...spec.scaleBar} />
        </div>
      ) : null}

      {spec.legend ? (
        <div style={corner('bottom-right')}>
          <Legend spec={spec.legend} />
        </div>
      ) : null}

      {spec.provenance ? (
        <div
          style={{
            ...corner('bottom-center'),
            fontSize: 9,
            opacity: 0.75,
            color: '#242e39',
            background: 'rgba(255, 255, 255, 0.7)',
            padding: '2px 6px',
            borderRadius: 2,
          }}
        >
          {spec.provenance}
        </div>
      ) : null}
    </div>
  );
}

function corner(
  position: 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right' | 'bottom-center',
): React.CSSProperties {
  const base: React.CSSProperties = { position: 'absolute' };
  switch (position) {
    case 'top-left':
      return { ...base, top: 12, left: 12 };
    case 'top-right':
      return { ...base, top: 12, right: 12 };
    case 'bottom-left':
      return { ...base, bottom: 12, left: 12 };
    case 'bottom-right':
      return { ...base, bottom: 12, right: 12 };
    case 'bottom-center':
      return { ...base, bottom: 4, left: '50%', transform: 'translateX(-50%)' };
  }
}

let root: Root | null = null;

/**
 * Mount the overlay into a container.
 *
 * Called once by the render shell before it waits for the map to settle. The
 * shell then waits on `document.fonts.ready` as well — a legend screenshotted
 * mid-font-swap renders in a fallback face, and the difference is large enough
 * to fail a visual golden.
 */
export function mount(container: HTMLElement, spec: OverlaySpec): void {
  root ??= createRoot(container);
  root.render(
    <StrictMode>
      <Overlay spec={spec} />
    </StrictMode>,
  );
}

export function unmount(): void {
  root?.unmount();
  root = null;
}
