/**
 * API client. `07-frontend.md` §7.
 *
 * The property worth protecting is that the server's message survives the trip
 * to the screen. The API spends real effort on those messages — the permission
 * ones name the owner so the conversation can continue (`03` §3.2) — and a
 * client that replaced them with "403 Forbidden" would discard the only part a
 * geologist can act on.
 */

import { describe, expect, it, vi } from 'vitest';

import { ApiClient, ApiError } from './client.js';

/** Run a request expected to fail and hand back the error it threw. */
async function failing(client: ApiClient, path = '/x'): Promise<ApiError> {
  try {
    await client.get(path);
  } catch (error) {
    return error as ApiError;
  }
  throw new Error(`Expected ${path} to fail, but it resolved.`);
}

function respond(status: number, body: unknown, ok = status < 400) {
  return vi.fn(() =>
    Promise.resolve({
      ok,
      status,
      json: () => Promise.resolve(body),
    } as Response),
  );
}

describe('requests', () => {
  it('sends the channel header, so audit can answer "who did this"', async () => {
    // `02-data-model.md` §3.12: actor_channel is what makes "what did Claude
    // do on my behalf" answerable, and the browser is always 'web'.
    const fetchImpl = respond(200, { ok: true });
    await new ApiClient({ fetchImpl }).get('/sessions');

    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>)['X-WebMap-Channel']).toBe('web');
  });

  it('attaches the bearer token when there is one', async () => {
    const fetchImpl = respond(200, {});
    await new ApiClient({ fetchImpl, token: 'abc.def' }).get('/sessions');

    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer abc.def');
  });

  it('omits Authorization entirely when there is no token', async () => {
    // An empty bearer is worse than none: it looks like a credential and
    // fails as one, which makes a misconfiguration read as a rejection.
    const fetchImpl = respond(200, {});
    await new ApiClient({ fetchImpl }).get('/sessions');

    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.headers).not.toHaveProperty('Authorization');
  });

  it('encodes query parameters and drops undefined ones', async () => {
    const fetchImpl = respond(200, {});
    await new ApiClient({ fetchImpl }).get('/datasets', { kind: 'pointset', limit: undefined });

    const [url] = fetchImpl.mock.calls[0] as unknown as [string];
    expect(url).toContain('kind=pointset');
    expect(url).not.toContain('limit');
  });

  it('returns nothing for a 204 rather than trying to parse a body', async () => {
    // The delete endpoint returns 204. `response.json()` on an empty body
    // throws, which would turn a successful delete into an error.
    const fetchImpl = vi.fn(() =>
      Promise.resolve({ ok: true, status: 204, json: () => Promise.reject(new Error('no body')) } as unknown as Response),
    );

    await expect(new ApiClient({ fetchImpl }).delete('/sessions/x')).resolves.toBeUndefined();
  });
});

describe('errors', () => {
  it('carries the server message through verbatim', async () => {
    // **The point of this module.** The message names the owner and the level
    // required so the conversation can continue.
    const detail =
      "You have viewer access to 'Wolfcamp A Structure' but editor is required. " +
      'Ask Ada Chen to grant edit access.';
    const fetchImpl = respond(403, { error: 'permission_denied', detail }, false);

    await expect(new ApiClient({ fetchImpl }).get('/datasets/x')).rejects.toMatchObject({
      status: 403,
      message: detail,
      code: 'permission_denied',
    });
  });

  it('classifies the statuses callers branch on', async () => {
    for (const [status, flag] of [
      [403, 'isPermission'],
      [404, 'isNotFound'],
      [409, 'isConflict'],
    ] as const) {
      const fetchImpl = respond(status, { detail: 'x' }, false);
      const error = await failing(new ApiClient({ fetchImpl }));
      expect(error[flag]).toBe(true);
    }
  });

  it('falls back to a status sentence when the body is not JSON', async () => {
    // A proxy's HTML error page. The status still carries the meaning.
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: false,
        status: 502,
        json: () => Promise.reject(new Error('not json')),
      } as unknown as Response),
    );

    await expect(new ApiClient({ fetchImpl }).get('/sessions')).rejects.toThrow(/502/);
  });

  it('reports a network failure as status 0, not as a refusal', async () => {
    // Autosave branches on 409 to decide whether to stop retrying. A network
    // failure reported as a 4xx would end autosave for a dropped packet.
    const fetchImpl = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));

    const error = await failing(new ApiClient({ fetchImpl }), '/sessions');

    expect(error.status).toBe(0);
    expect(error.isConflict).toBe(false);
    expect(error.message).toContain('still here');
  });

  it('says the unsaved work is safe when the network drops', async () => {
    // The one thing a user needs to know at that moment.
    const fetchImpl = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));

    await expect(new ApiClient({ fetchImpl }).get('/x')).rejects.toThrow(
      /unsaved changes are still here/,
    );
  });
});
