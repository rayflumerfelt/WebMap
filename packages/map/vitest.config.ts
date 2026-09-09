import { defineConfig } from 'vitest/config';

export default defineConfig({
  // jsdom, not node: the component renders a real DOM element and the tests
  // assert on it. MapLibre itself is mocked — it needs a WebGL context, which
  // jsdom does not provide and which would tell us nothing extra here
  // (`07-frontend.md` §11).
  test: { environment: 'jsdom', globals: false },
  esbuild: { jsx: 'automatic' },
});
