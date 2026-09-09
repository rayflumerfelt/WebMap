import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // `/auth` as well as `/api`. The session cookie is how the browser
    // authenticates, and the API sets no CORS headers — deliberately, since
    // in production the SPA is served from the same origin. Without this
    // proxy, signing in during development is a cross-origin POST that the
    // browser will not send credentials for, and the app is unusable against
    // a local stack.
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/auth': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  test: { environment: 'jsdom', globals: false, setupFiles: ['./vitest.setup.ts'] },
});
