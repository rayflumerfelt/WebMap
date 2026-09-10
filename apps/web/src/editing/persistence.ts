/**
 * Save and Discard. `09-editing.md` §5.2, §5.3.
 *
 * Separate from the store for the same reason `autosave.ts` is separate from
 * `sessionStore`: a store that did I/O would have to be mocked by every test
 * that touches state, and a store that did I/O *and* held the undo stack would
 * be the place a failed request could leave the buffer half-committed.
 *
 * The order matters and is the whole of this module. The buffer is only
 * cleared **after** the server has confirmed the version — a save that marked
 * itself done optimistically and then failed would leave a geologist looking
 * at a clean editor with an hour of work that reached nothing.
 */

import { saveFeatureEdits } from '../api/features.js';
import type { ApiClient } from '../api/client.js';
import { ApiError } from '../api/client.js';
import { useEditStore } from '../stores/editStore.js';
import { isDirty, pendingDeltas } from './session.js';

export type SaveOutcome =
  | { status: 'saved'; version: number; featureCount: number }
  /** Nothing to save. Not an error: `mod+s` on a clean session is ordinary. */
  | { status: 'clean' }
  /**
   * Somebody saved first (§5.3). The buffer is **left alone** — the edits are
   * the user's work and the choice between Refresh and Force is theirs.
   */
  | { status: 'conflict'; message: string }
  | { status: 'failed'; message: string };

/**
 * Write the session's pending edits.
 *
 * `datasetId` rather than the layer id: the version pointer lives on the
 * dataset, and a layer is a view of one (`adr/0010`).
 */
export async function saveEdits(api: ApiClient, datasetId: string): Promise<SaveOutcome> {
  const store = useEditStore.getState();
  const session = store.session;
  if (!session || !isDirty(session)) return { status: 'clean' };

  const deltas = pendingDeltas(session);
  const baseVersion = session.baseVersion;

  try {
    const response = await saveFeatureEdits(api, datasetId, baseVersion, deltas);
    // Only now. Everything above can fail, and the buffer is the only copy.
    useEditStore.getState().markSaved(response.version);
    return {
      status: 'saved',
      version: response.version,
      featureCount: response.feature_count,
    };
  } catch (error) {
    if (error instanceof ApiError && error.isConflict) {
      return { status: 'conflict', message: error.message };
    }
    return {
      status: 'failed',
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

/**
 * Throw away the session's pending edits.
 *
 * Confirmation is the caller's — §5.2 makes this the one action in the editor
 * that cannot be undone, and a function that asked would be a function that
 * could not be called from a confirmed dialog.
 */
export function discardEdits(): void {
  useEditStore.getState().discardEdits();
}

/** How many features a save would write. For the confirm prompt and the
 *  dirty badge, which should agree. */
export function pendingCount(): number {
  return useEditStore.getState().session?.dirty.size ?? 0;
}
