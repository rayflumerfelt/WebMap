/**
 * Builds the render shell's overlay bundle. `06-rendering.md` §4.
 *
 * A single self-contained UMD file, because the shell is served from `file://`
 * and fetches nothing — every guard in `03-auth-security.md` §7 assumes the
 * page loads no network resource of its own, and a bundle with external
 * imports would quietly break that assumption.
 *
 * React is bundled in rather than externalised for the same reason.
 */
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  build: {
    lib: {
      entry: 'src/overlay-entry.tsx',
      name: 'WebMapOverlay',
      formats: ['umd'],
      fileName: () => 'overlay.js',
    },
    outDir: '../../apps/render/shell',
    emptyOutDir: false,
    // The shell is not served to browsers over the wire; a readable bundle is
    // worth more here than a small one when a render comes out wrong.
    minify: false,
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
  define: { 'process.env.NODE_ENV': '"production"' },
});
