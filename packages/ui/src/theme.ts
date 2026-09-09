import { createTheme } from '@mantine/core';

/**
 * `07-frontend.md` §4. A working instrument for subsurface geologists, not a
 * consumer product. Chrome is near-neutral with a cool cast so it does not
 * compete with map colour ramps, which span the full spectrum.
 */
export const webmapTheme = createTheme({
  colors: {
    // Chrome greys with a slight blue cast — reads as "instrument", and
    // critically does not tint perception of adjacent map colours.
    slate: [
      '#f5f7f9', '#e6eaee', '#ccd4dc', '#adb9c5', '#8e9dad',
      '#75879a', '#5f7286', '#4a5b6d', '#374553', '#242e39',
    ],
    // Single accent, used only for selection and active tools.
    signal: [
      '#e8f4f8', '#c5e4ef', '#9dd2e4', '#6fbdd6', '#48a9c8',
      '#2b93b3', '#1e7a97', '#15607a', '#0e485c', '#08313f',
    ],
  },
  primaryColor: 'signal',
  fontFamily: 'Inter, system-ui, sans-serif',
  // Tabular figures throughout. Coordinates, elevations, and contour values
  // must align vertically in tables and readouts — proportional digits make
  // scanning a column of depths genuinely harder.
  fontFamilyMonospace: 'IBM Plex Mono, monospace',
  headings: { fontFamily: 'Inter, system-ui, sans-serif', fontWeight: '600' },
  defaultRadius: 'xs',
});
