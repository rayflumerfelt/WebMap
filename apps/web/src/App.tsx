import { webmapTheme } from '@webmap/ui';

/**
 * The application shell. Phase 2 fills in the docked panel layout from
 * `07-frontend.md` §5.2; Phase 0 stands up the toolchain and the viewport
 * policy.
 */
export function App() {
  return (
    <>
      <p className="min-width-notice">
        WebMap requires a window at least 1280 px wide.
      </p>
      <main className="app-shell">
        <h1>WebMap</h1>
        <p>
          Phase 0 scaffolding. Accent colour {webmapTheme.primaryColor} is
          wired from the shared theme.
        </p>
      </main>
    </>
  );
}
