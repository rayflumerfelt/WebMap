/**
 * The failure page for a Claude link. `07-frontend.md` §8.
 *
 * §8 names four cases this must handle — permission, a short code that does
 * not resolve, deleted datasets, and expiry — and singles out the first:
 * "a geologist following a shared link to data they cannot access should see
 * who to ask, matching the message from `03-auth-security.md` §3.2."
 *
 * So the assertions are about *what the reader is told*, not about which
 * branch ran.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client.js';
import { SessionError } from './SessionError.js';

afterEach(cleanup);

describe('permission', () => {
  it('shows who to ask, verbatim from the API', () => {
    // **The case §8 singles out.** A generic "access denied" would throw away
    // the owner's name and leave the reader with nobody to ask.
    const detail =
      "You have viewer access to 'Wolfcamp A Structure' but editor is required. " +
      'Ask Ada Chen to grant edit access.';

    render(
      <SessionError
        error={new ApiError(403, detail, 'permission_denied')}
        shortCode="k3n8fq"
      />,
    );

    expect(screen.getByText(detail)).toBeDefined();
    expect(screen.getByText(/Ask Ada Chen/)).toBeDefined();
  });

  it('does not offer a retry for a refusal', () => {
    // Retrying a 403 fails identically. A button that cannot work is worse
    // than no button: it implies the problem might be transient.
    render(
      <SessionError error={new ApiError(403, 'Ask Ada.', 'permission_denied')} shortCode="k3n8fq" onRetry={vi.fn()} />,
    );

    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  });
});

describe('a link that does not resolve', () => {
  it('gives one answer for wrong, deleted and expired alike', () => {
    // The API deliberately does not distinguish them — confirming that a short
    // code belongs to someone is itself a disclosure — and this page must not
    // invent a distinction the server refused to make.
    const detail =
      'No session k3n8fq. The link may be wrong, the session may have been ' +
      'deleted, or it may belong to someone who has not shared it with you.';

    render(<SessionError error={new ApiError(404, detail)} shortCode="k3n8fq" />);

    expect(screen.getByText(detail)).toBeDefined();
  });
});

describe('transient failures', () => {
  it('offers a retry when the network is down', () => {
    render(
      <SessionError
        error={new ApiError(0, 'Could not reach the WebMap API.', 'network_error')}
        shortCode="k3n8fq"
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: 'Try again' })).toBeDefined();
  });

  it('offers a retry for a server error', () => {
    render(
      <SessionError error={new ApiError(503, 'Upstream unavailable.')} shortCode="k3n8fq" onRetry={vi.fn()} />,
    );

    expect(screen.getByRole('button', { name: 'Try again' })).toBeDefined();
  });
});

describe('always', () => {
  it('shows the link that was followed', () => {
    // The reader arrived from a chat and may have several. Naming the one
    // that failed is the difference between "this link is broken" and
    // "WebMap is broken".
    render(<SessionError error={new ApiError(404, 'No session.')} shortCode="k3n8fq" />);

    expect(screen.getByText('/s/k3n8fq')).toBeDefined();
  });

  it('announces itself as an alert', () => {
    render(<SessionError error={new ApiError(404, 'No session.')} shortCode="k3n8fq" />);

    expect(screen.getByRole('alert')).toBeDefined();
  });

  it('says something useful even for an error with no message', () => {
    // A thrown string, a network stack quirk, a bug here. The reader still
    // needs a next action, and "ask whoever shared it to open it themselves"
    // is a real one — it separates a session problem from an access problem.
    render(<SessionError error={new Error()} shortCode="k3n8fq" />);

    expect(screen.getByText(/ask whoever shared the link/i)).toBeDefined();
  });
});
