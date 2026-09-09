/**
 * Legend overlay. `06-rendering.md` §9, `08-styling-palettes.md` §8.
 *
 * **Mandatory for anything going into a presentation.** A map on a slide is
 * shown to people who were not in the conversation, in a room where nobody
 * can ask what the colours mean.
 *
 * Renders a `LegendSpec` and nothing else — the decisions about how many
 * entries there are and what they are labelled were made in
 * `@webmap/style-model`, against the symbology model rather than the compiled
 * style. Splitting it that way is what lets the count be tested without a
 * browser, which is where the off-by-one §8 warns about would otherwise hide.
 */

import type { ColorbarLegend, LegendSpec, Palette } from '@webmap/style-model';
import { colourAt } from '@webmap/style-model';

export interface LegendProps {
  spec: LegendSpec;
  className?: string;
}

export function Legend({ spec, className }: LegendProps) {
  return (
    <div
      className={className}
      style={{
        display: 'inline-block',
        padding: '6px 8px',
        background: 'rgba(255, 255, 255, 0.9)',
        borderRadius: 2,
        fontSize: 11,
        lineHeight: 1.35,
        color: '#242e39',
        maxWidth: 220,
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{spec.title}</div>
      {spec.kind === 'classes' ? (
        <CategoryLegend spec={spec} />
      ) : (
        <ColorBar spec={spec} />
      )}
    </div>
  );
}

function CategoryLegend({ spec }: { spec: Extract<LegendSpec, { kind: 'classes' }> }) {
  return (
    <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
      {spec.entries.map((entry) => (
        <li
          key={`${entry.swatch}:${entry.label}`}
          style={{ display: 'flex', alignItems: 'center', gap: 6, minHeight: 16 }}
        >
          <span
            aria-hidden="true"
            style={{
              width: 12,
              height: 12,
              flex: '0 0 auto',
              background: entry.swatch,
              // A hairline border, so a white or very pale swatch is still a
              // swatch rather than a gap in the list.
              border: '1px solid rgba(36, 46, 57, 0.35)',
            }}
          />
          <span>{entry.label}</span>
        </li>
      ))}
    </ul>
  );
}

/** Number of gradient stops sampled for the bar. */
const BAR_STOPS = 32;

function ColorBar({ spec }: { spec: ColorbarLegend }) {
  const [low, high] = spec.range;
  const gradient = rampGradient(spec.palette);
  const suffix = spec.unit ? ` ${spec.unit}` : '';

  return (
    <div style={{ minWidth: 160 }}>
      <div
        role="img"
        aria-label={`Colour ramp from ${low}${suffix} to ${high}${suffix}`}
        style={{
          height: 10,
          background: `linear-gradient(to right, ${gradient})`,
          border: '1px solid rgba(36, 46, 57, 0.35)',
        }}
      />
      <div style={{ position: 'relative', height: 14, marginTop: 2 }}>
        {spec.ticks.map((tick) => (
          <span
            key={tick}
            style={{
              position: 'absolute',
              // Ticks sit at pretty values, so their positions are uneven by
              // design — placed proportionally rather than distributed, or
              // the label would not sit over the colour it describes.
              left: `${((tick - low) / (high - low)) * 100}%`,
              transform: 'translateX(-50%)',
              whiteSpace: 'nowrap',
            }}
          >
            {tick.toLocaleString('en-US')}
          </span>
        ))}
      </div>
      {spec.unit ? (
        <div style={{ textAlign: 'right', opacity: 0.75 }}>{spec.unit}</div>
      ) : null}
    </div>
  );
}

/**
 * A CSS gradient for a palette.
 *
 * Sampled through `colourAt` rather than emitting the palette's own stops
 * directly, so a discrete palette renders as hard bands — the browser would
 * otherwise interpolate between them and draw a continuous ramp for a
 * classification that is not continuous, which is the same lie the compiler
 * avoids by emitting `step` instead of `interpolate`.
 */
function rampGradient(palette: Palette): string {
  if (palette.interpolation === 'discrete') {
    const stops = [...palette.stops].sort((a, b) => a.position - b.position);
    return stops
      .flatMap((stop, index) => {
        const next = stops[index + 1];
        const end = next ? next.position : 1;
        return [
          `${stop.color} ${stop.position * 100}%`,
          `${stop.color} ${end * 100}%`,
        ];
      })
      .join(', ');
  }

  return Array.from({ length: BAR_STOPS }, (_, i) => {
    const position = i / (BAR_STOPS - 1);
    return `${colourAt(palette, position)} ${position * 100}%`;
  }).join(', ');
}
