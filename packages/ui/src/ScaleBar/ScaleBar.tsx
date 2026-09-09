/**
 * Scale bar overlay. `06-rendering.md` §9.
 *
 * Takes a latitude and a zoom rather than a map instance: `@webmap/ui`
 * imports no MapLibre, which is what lets the same component render in the
 * SPA and in the headless render shell, where there is no map in scope for
 * overlays. One implementation, so a slide and the screen cannot disagree.
 */

import { scaleBar } from './scale.js';
import type { ScaleUnit } from './scale.js';

export interface ScaleBarProps {
  /** Latitude of the map centre. Web Mercator scale varies with it by
   *  1/cos(φ) — see `scale.ts` for why this is not optional. */
  latitude: number;
  zoom: number;
  /** Maximum bar width in CSS pixels. The bar takes the largest round
   *  distance that fits within it. */
  maxWidth?: number;
  unit?: ScaleUnit;
  className?: string;
}

export function ScaleBar({
  latitude,
  zoom,
  maxWidth = 120,
  unit = 'imperial',
  className,
}: ScaleBarProps) {
  const bar = scaleBar(latitude, zoom, maxWidth, unit);
  if (bar.widthPx <= 0) return null;

  return (
    <div
      className={className}
      // `img` with a label rather than raw text: a screen reader announcing
      // "1 km" out of context is meaningless, and the bar's meaning is the
      // pairing of the number with the drawn length.
      role="img"
      aria-label={`Scale bar: ${bar.label}`}
      style={{
        display: 'inline-flex',
        flexDirection: 'column',
        alignItems: 'stretch',
        gap: 2,
        padding: '3px 5px',
        // Semi-opaque plate: the bar sits over map data of unknown colour,
        // and a bar that vanishes over a dark ramp is not a scale bar.
        background: 'rgba(255, 255, 255, 0.82)',
        borderRadius: 2,
        fontSize: 11,
        lineHeight: 1.2,
        color: '#242e39',
        userSelect: 'none',
      }}
    >
      <span style={{ textAlign: 'center' }}>{bar.label}</span>
      <div
        style={{
          width: bar.widthPx,
          height: 6,
          // Ticked at both ends, open in the middle: the classic form, and it
          // makes the measured span unambiguous where a filled bar's edges
          // are guesswork.
          borderLeft: '2px solid #242e39',
          borderRight: '2px solid #242e39',
          borderBottom: '2px solid #242e39',
        }}
      />
    </div>
  );
}
