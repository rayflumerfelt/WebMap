/**
 * Save and Discard. `09-editing.md` §5.2, §5.3, §13.
 *
 * The rule these tests exist for is the order: the buffer is cleared only
 * after the server has confirmed the version. A save that marked itself done
 * optimistically and then failed would leave a geologist looking at a clean
 * editor with an hour of work that reached nothing.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { ApiClient, ApiError } from '../api/client.js';
import { toEditPayload } from '../api/features.js';
import { useEditStore } from '../stores/editStore.js';
import { discardEdits, pendingCount, saveEdits } from './persistence.js';
import type { Command, Feature } from './session.js';

const WELL: Feature = {
  id: '42',
  geometry: { type: 'Point', coordinates: [-102.08, 31.99] },
  properties: { name: 'Wolfcamp 0042' },
};

const MOVED: Feature = {
  ...WELL,
  geometry: { type: 'Point', coordinates: [-102.07, 31.98] },
};

function moveCommand(): Command {
  return {
    id: 'command-1',
    label: 'Move Vertex',
    deltas: [{ featureId: '42', before: WELL, after: MOVED }],
    timestamp: 0,
  };
}

function deleteCommand(): Command {
  return {
    id: 'command-2',
    label: 'Delete Feature',
    deltas: [{ featureId: '42', before: WELL, after: null }],
    timestamp: 0,
  };
}

/** An ApiClient whose fetch is ours, so the request body is inspectable. */
function client(respond: (body: unknown) => Response): {
  api: ApiClient;
  bodies: unknown[];
} {
  const bodies: unknown[] = [];
  const api = new ApiClient({
    baseUrl: '/api/v1',
    fetchImpl: (async (_url: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body));
      bodies.push(body);
      return respond(body);
    }) as unknown as typeof fetch,
  });
  return { api, bodies };
}

function ok(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  useEditStore.setState({ session: null, revision: 0 });
  useEditStore.getState().dispatch({ type: 'setActiveLayer', layerId: null });
  useEditStore.getState().activateLayer({
    layerId: 'wells',
    baseVersion: 7,
    geometry: 'point',
    canEdit: true,
    features: [WELL],
  });
});

describe('toEditPayload', () => {
  it('sends the whole feature for an edit', () => {
    // Properties are replaced whole rather than merged — a merge cannot
    // express clearing a field.
    expect(toEditPayload([{ featureId: '42', before: WELL, after: MOVED }])).toEqual([
      {
        feature_id: 42,
        geometry: MOVED.geometry,
        properties: { name: 'Wolfcamp 0042' },
      },
    ]);
  });

  it('sends a delete as a delete, with no new state', () => {
    expect(toEditPayload([{ featureId: '42', before: WELL, after: null }])).toEqual([
      { feature_id: 42, deleted: true },
    ]);
  });

  it('refuses a non-numeric id rather than sending NaN', () => {
    // `Number('abc')` is NaN, and a save carrying it would come back as a
    // message about a feature that does not exist. The real fault is upstream.
    expect(() =>
      toEditPayload([{ featureId: 'abc', before: WELL, after: MOVED }]),
    ).toThrow(/promoteId/);
  });
});

describe('saveEdits', () => {
  it('does nothing on a clean session', () => {
    // `mod+s` on a clean editor is ordinary, not an error.
    const { api, bodies } = client(() => ok({}));

    return saveEdits(api, 'dataset-1').then((outcome) => {
      expect(outcome).toEqual({ status: 'clean' });
      expect(bodies).toEqual([]);
    });
  });

  it('sends the base version the session was opened against', async () => {
    // §5.3 puts optimistic concurrency on that pointer and nowhere else.
    useEditStore.getState().applyCommand(moveCommand());
    const { api, bodies } = client(() => ok({ version: 8, feature_count: 400 }));

    await saveEdits(api, 'dataset-1');

    expect(bodies[0]).toMatchObject({ base_version: 7 });
  });

  it('clears the buffer and adopts the new version', async () => {
    useEditStore.getState().applyCommand(moveCommand());
    const { api } = client(() => ok({ version: 8, feature_count: 400 }));

    const outcome = await saveEdits(api, 'dataset-1');

    expect(outcome).toEqual({ status: 'saved', version: 8, featureCount: 400 });
    expect(useEditStore.getState().session!.dirty.size).toBe(0);
    expect(useEditStore.getState().session!.baseVersion).toBe(8);
  });

  it('makes the saved geometry the one the next edit starts from', async () => {
    // The dirty features become the exact ones. Without that, the next edit
    // computes its `before` from the state two saves ago.
    useEditStore.getState().applyCommand(moveCommand());
    const { api } = client(() => ok({ version: 8, feature_count: 400 }));

    await saveEdits(api, 'dataset-1');

    expect(useEditStore.getState().session!.exactCache.get('42')).toEqual(MOVED);
  });

  it('drops a deleted feature from the exact cache', async () => {
    useEditStore.getState().applyCommand(deleteCommand());
    const { api } = client(() => ok({ version: 8, feature_count: 399 }));

    await saveEdits(api, 'dataset-1');

    expect(useEditStore.getState().session!.exactCache.has('42')).toBe(false);
  });

  it('keeps the buffer when the save fails', async () => {
    // The buffer is the only copy. This is the assertion the module exists
    // for.
    useEditStore.getState().applyCommand(moveCommand());
    const { api } = client(
      () => new Response('{"detail":"boom"}', { status: 500 }),
    );

    const outcome = await saveEdits(api, 'dataset-1');

    expect(outcome.status).toBe('failed');
    expect(useEditStore.getState().session!.dirty.size).toBe(1);
    expect(useEditStore.getState().session!.baseVersion).toBe(7);
  });

  it('reports a 409 as a conflict and leaves the edits alone', async () => {
    // §5.3: the choice between Refresh and Force is the user's, and either
    // needs the edits still to be there.
    useEditStore.getState().applyCommand(moveCommand());
    const { api } = client(
      () =>
        new Response(JSON.stringify({ detail: 'Someone saved while you were editing.' }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        }),
    );

    const outcome = await saveEdits(api, 'dataset-1');

    expect(outcome.status).toBe('conflict');
    expect(useEditStore.getState().session!.dirty.size).toBe(1);
  });

  it('leaves the undo stack intact after a failure', async () => {
    // A failed save that had cleared it would strand the user: no undo, and
    // no save either.
    useEditStore.getState().applyCommand(moveCommand());
    const { api } = client(() => new Response('{}', { status: 503 }));

    await saveEdits(api, 'dataset-1');

    expect(useEditStore.getState().session!.undoStack).toHaveLength(1);
  });

  it('clears the undo stack after a save', async () => {
    // After a commit there is nothing local to undo — the previous state is a
    // version on the server, and undoing into it would disagree with the
    // baseVersion the session has just advanced past.
    useEditStore.getState().applyCommand(moveCommand());
    const { api } = client(() => ok({ version: 8, feature_count: 400 }));

    await saveEdits(api, 'dataset-1');

    expect(useEditStore.getState().session!.undoStack).toHaveLength(0);
  });
});

describe('discardEdits', () => {
  it('empties the buffer and both stacks', () => {
    useEditStore.getState().applyCommand(moveCommand());

    discardEdits();

    expect(pendingCount()).toBe(0);
    expect(useEditStore.getState().session!.undoStack).toHaveLength(0);
    expect(useEditStore.getState().session!.redoStack).toHaveLength(0);
  });

  it('leaves the version pointer where it was', () => {
    // Discarding is not a save: nothing reached the server, so the session is
    // still editing against the version it opened.
    useEditStore.getState().applyCommand(moveCommand());

    discardEdits();

    expect(useEditStore.getState().session!.baseVersion).toBe(7);
  });
});

describe('pendingCount', () => {
  it('counts features, not commands', () => {
    // Four drags of one vertex are one feature to write (§13).
    const store = useEditStore.getState();
    store.applyCommand(moveCommand());
    store.applyCommand({ ...moveCommand(), id: 'command-3' });

    expect(pendingCount()).toBe(1);
  });

  it('is zero with no session', () => {
    useEditStore.setState({ session: null });

    expect(pendingCount()).toBe(0);
  });
});

describe('ApiError', () => {
  it('recognises a conflict, which is what the save flow branches on', () => {
    expect(new ApiError(409, 'x').isConflict).toBe(true);
  });
});
