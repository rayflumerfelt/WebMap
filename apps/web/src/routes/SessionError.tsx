/**
 * What a geologist sees when a session link does not open.
 * `07-frontend.md` §8.
 *
 * "`SessionError` must handle the permission case specifically — a geologist
 * following a shared link to data they cannot access should see who to ask,
 * matching the message from `03-auth-security.md` §3.2 — alongside a short
 * code that does not resolve, a session whose datasets have been deleted, and
 * one that has expired."
 *
 * The API already writes those messages well, and each one names a next
 * action. This component's job is to **show the server's sentence rather than
 * replace it**, and to add only what the browser knows that the server does
 * not: the link that was followed, and where to go instead.
 */

import { ApiError } from '../api/client.js';

export interface SessionErrorProps {
  error: unknown;
  shortCode: string;
  onRetry?(): void;
}

export function SessionError({ error, shortCode, onRetry }: SessionErrorProps) {
  const { heading, detail, retryable } = describe(error, shortCode);

  return (
    <div
      role="alert"
      style={{
        display: 'grid',
        placeItems: 'center',
        minHeight: '100vh',
        padding: 24,
        background: 'var(--chrome-bg)',
      }}
    >
      <div style={{ maxWidth: 520 }}>
        <h1 style={{ fontSize: 16, margin: '0 0 8px' }}>{heading}</h1>
        <p style={{ fontSize: 13, lineHeight: 1.55, margin: '0 0 12px' }}>{detail}</p>
        <p style={{ fontSize: 12, opacity: 0.7, margin: '0 0 16px' }}>
          Link: <code>/s/{shortCode}</code>
        </p>
        {retryable && onRetry ? (
          <button type="button" onClick={onRetry} style={buttonStyle}>
            Try again
          </button>
        ) : null}
      </div>
    </div>
  );
}

function describe(
  error: unknown,
  shortCode: string,
): { heading: string; detail: string; retryable: boolean } {
  if (error instanceof ApiError) {
    // **The permission case, first and specifically.** The API's message names
    // the owner and the level required so the conversation can continue; a
    // generic "access denied" here would throw that away and leave the reader
    // with nobody to ask.
    if (error.isPermission) {
      return { heading: 'You do not have access to this session', detail: error.message, retryable: false };
    }

    if (error.isNotFound) {
      // One heading for "wrong code", "deleted" and "expired", because the API
      // deliberately does not distinguish them — confirming that a short code
      // belongs to someone is itself a disclosure. The server's sentence
      // covers all three and says what to do.
      return {
        heading: 'That session did not open',
        detail: error.message,
        retryable: false,
      };
    }

    if (error.status === 0) {
      return { heading: 'Cannot reach WebMap', detail: error.message, retryable: true };
    }

    return {
      heading: 'The session could not be loaded',
      detail: error.message,
      retryable: error.status >= 500,
    };
  }

  return {
    heading: 'The session could not be loaded',
    detail:
      `Something went wrong opening ${shortCode}, and the error carried no ` +
      `explanation. Reloading sometimes clears it; if not, ask whoever shared ` +
      `the link to open it themselves so we can tell whether the problem is ` +
      `the session or the access.`,
    retryable: true,
  };
}

const buttonStyle: React.CSSProperties = {
  height: 'var(--control-h, 28px)',
  padding: '0 12px',
  border: '1px solid var(--chrome-border)',
  borderRadius: 2,
  background: 'transparent',
  cursor: 'pointer',
  font: 'inherit',
};
