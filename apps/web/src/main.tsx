import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App.js';
import { SessionRoute } from './routes/SessionRoute.js';
import './styles/layout.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('No #root element. index.html and main.tsx are out of step.');
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Five minutes: dataset metadata and project settings change rarely, and
      // a geologist keeps a session open for hours. Refetching constantly
      // would spend requests to learn nothing.
      staleTime: 5 * 60_000,
      retry: (failureCount, error) => {
        // A 4xx will refuse identically next time and its message is already
        // the answer. Only server and network failures are worth retrying.
        const status = (error as { status?: number }).status;
        if (status !== undefined && status >= 400 && status < 500) return false;
        return failureCount < 2;
      },
    },
  },
});

/**
 * Routing, such as it is.
 *
 * One route that matters — `/s/:shortCode`, the link Claude hands out — and
 * everything else is the empty shell. A router library buys nothing at two
 * routes, and `07-frontend.md` §8 describes the session route as the entry
 * point rather than as one of many. When a second real route appears, this is
 * the place that changes.
 */
function Root() {
  const match = /^\/s\/([A-Za-z0-9]+)\/?$/.exec(globalThis.location.pathname);
  const shortCode = match?.[1];

  return (
    <QueryClientProvider client={queryClient}>
      {shortCode ? <SessionRoute shortCode={shortCode} /> : <App />}
    </QueryClientProvider>
  );
}

createRoot(container).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
