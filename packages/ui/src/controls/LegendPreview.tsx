/**
 * The live legend beside every formatting dialog. `07-frontend.md` §6.2, §6.3.
 *
 * "The cheapest way to catch a palette that looks fine in the editor and
 * illegible on the map." Two things make it worth its space, and both are
 * about *context* rather than about the colours themselves:
 *
 * - **It draws over the map's own background**, switchable between a light and
 *   a dark ground. A pale yellow class is perfectly visible in an editor on
 *   white and invisible over satellite imagery, and the editor is where that
 *   gets noticed or does not.
 * - **It derives from the symbology model**, through the same `deriveLegend`
 *   the map and the render service use. A preview that built its own entries
 *   would be the one place allowed to disagree about how many classes there
 *   are — which is exactly the drift `08` §8 warns about.
 *
 * It renders whatever `deriveLegend` returns and adds nothing, so a mode that
 * has no legend is a mode that shows none, rather than a special case here.
 */

import type { LayerMetadata, Palette, Symbology } from '@webmap/style-model';
import { deriveLegend } from '@webmap/style-model';
import { useMemo, useState } from 'react';
import { Legend } from '../Legend/Legend.js';
import { hint, stack } from './styles.js';

export interface LegendPreviewProps {
  symbology: Symbology;
  meta: LayerMetadata;
  palettes: Record<string, Palette>;
  /** Start on the dark ground — for a layer that sits over imagery. */
  defaultDark?: boolean;
  className?: string;
}

/** The two grounds. Neither is the panel's own colour, on purpose: a legend
 *  that disappears into the dialog is the failure this control exists to
 *  catch, and it can only be caught against something the map might be. */
const GROUNDS = {
  light: { name: 'Light ground', background: '#f2efe9' },
  dark: { name: 'Dark ground', background: '#2b2f36' },
};

export function LegendPreview({
  symbology,
  meta,
  palettes,
  defaultDark = false,
  className,
}: LegendPreviewProps) {
  const [dark, setDark] = useState(defaultDark);
  const spec = useMemo(
    () => deriveLegend(symbology, meta, palettes),
    [symbology, meta, palettes],
  );
  const ground = dark ? GROUNDS.dark : GROUNDS.light;

  return (
    <div className={className} style={stack}>
      <div
        style={{
          display: 'grid',
          placeItems: 'start',
          padding: 10,
          borderRadius: 3,
          border: '1px solid #c9ccd1',
          background: ground.background,
          minHeight: 72,
        }}
      >
        <Legend spec={spec} />
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button
          type="button"
          aria-pressed={dark}
          onClick={() => setDark((current) => !current)}
          style={{
            fontSize: 11,
            padding: '2px 8px',
            border: '1px solid #c9ccd1',
            borderRadius: 3,
            background: '#f6f7f9',
            cursor: 'pointer',
          }}
        >
          {dark ? GROUNDS.light.name : GROUNDS.dark.name}
        </button>
        <span style={hint}>
          {spec.kind === 'classes'
            ? `${spec.entries.length} ${spec.entries.length === 1 ? 'entry' : 'entries'}`
            : 'Continuous'}
        </span>
      </div>
    </div>
  );
}
