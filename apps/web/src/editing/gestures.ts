/**
 * Pointer gestures on vertex handles. `09-editing.md` §11.6.
 *
 * A press on a handle is ambiguous until the pointer either moves or does not:
 * held still it is a selection, moved it is a drag. This reducer is where that
 * ambiguity is resolved, and it is a reducer rather than a tangle of refs in
 * the pointer handler for the usual reason — the rules are the content, and in
 * a hook they would be untestable.
 *
 * **The threshold is not decoration.** Without it every click on a handle moves
 * the vertex by whatever pixel the hand shook, which produces a layer full of
 * sub-foot edits nobody made and a dirty buffer that never empties. Three
 * pixels is below what anyone can hold still and far below the snap floor of
 * four (§6.3), so a deliberate drag always crosses it before anything snaps.
 *
 * **Escape cancels a drag without ending the mode.** §4's two-press rule: the
 * first press abandons the operation, the second leaves the mode. A drag that
 * could only be ended by dropping the vertex somewhere would have no way out.
 */

import type { SelectedVertex } from './overlay.js';
import type { Pixel } from './snap.js';

/** Pixels the pointer must travel before a press becomes a drag. */
export const DRAG_THRESHOLD_PX = 3;

export type Gesture =
  | { kind: 'idle' }
  /** Pressed on a handle; not yet moved far enough to be a drag. */
  | { kind: 'pending'; vertex: SelectedVertex; origin: Pixel; additive: boolean }
  | { kind: 'dragging'; vertex: SelectedVertex; origin: Pixel };

export type GestureInput =
  | { type: 'down'; point: Pixel; handle: SelectedVertex | null; additive: boolean }
  | { type: 'move'; point: Pixel }
  | { type: 'up'; point: Pixel }
  | { type: 'escape' };

/**
 * What the caller should do. One per step, because a step that could ask for
 * two things would need an order and the order would be the bug.
 */
export type GestureIntent =
  | { type: 'none' }
  /** A press that turned out to be a click: select the vertex. */
  | { type: 'selectVertex'; vertex: SelectedVertex; additive: boolean }
  /** Crossed the threshold: cache projections, resolve exact geometry (§6.6). */
  | { type: 'beginDrag'; vertex: SelectedVertex }
  /** Mid-drag: show the vertex here, but do not write to the dirty buffer. */
  | { type: 'previewDrag'; vertex: SelectedVertex; point: Pixel }
  /** Dropped: this is the one that becomes a command. */
  | { type: 'commitDrag'; vertex: SelectedVertex; point: Pixel }
  | { type: 'cancelDrag'; vertex: SelectedVertex };

export const IDLE_GESTURE: Gesture = { kind: 'idle' };

function beyondThreshold(origin: Pixel, point: Pixel): boolean {
  const dx = point.x - origin.x;
  const dy = point.y - origin.y;
  return dx * dx + dy * dy > DRAG_THRESHOLD_PX * DRAG_THRESHOLD_PX;
}

/**
 * One step of the machine.
 *
 * Returns the next gesture and the single thing the caller should do. A press
 * on empty space is not this module's business — feature selection is a click
 * on the map, and mixing the two here is what produces a drag that also
 * changes the selection under it.
 */
export function step(
  gesture: Gesture,
  input: GestureInput,
): { gesture: Gesture; intent: GestureIntent } {
  switch (input.type) {
    case 'down': {
      if (!input.handle) return { gesture: IDLE_GESTURE, intent: { type: 'none' } };
      return {
        gesture: {
          kind: 'pending',
          vertex: input.handle,
          origin: input.point,
          additive: input.additive,
        },
        intent: { type: 'none' },
      };
    }

    case 'move': {
      if (gesture.kind === 'pending') {
        if (!beyondThreshold(gesture.origin, input.point)) {
          return { gesture, intent: { type: 'none' } };
        }
        return {
          gesture: { kind: 'dragging', vertex: gesture.vertex, origin: gesture.origin },
          // `beginDrag` and not a preview: the exact-geometry resolution of
          // §6.6 happens here, and the first preview frame should already be
          // against exact coordinates rather than jumping once they arrive.
          intent: { type: 'beginDrag', vertex: gesture.vertex },
        };
      }
      if (gesture.kind === 'dragging') {
        return {
          gesture,
          intent: { type: 'previewDrag', vertex: gesture.vertex, point: input.point },
        };
      }
      return { gesture, intent: { type: 'none' } };
    }

    case 'up': {
      if (gesture.kind === 'pending') {
        return {
          gesture: IDLE_GESTURE,
          intent: {
            type: 'selectVertex',
            vertex: gesture.vertex,
            additive: gesture.additive,
          },
        };
      }
      if (gesture.kind === 'dragging') {
        return {
          gesture: IDLE_GESTURE,
          intent: { type: 'commitDrag', vertex: gesture.vertex, point: input.point },
        };
      }
      return { gesture, intent: { type: 'none' } };
    }

    case 'escape': {
      if (gesture.kind === 'dragging') {
        return { gesture: IDLE_GESTURE, intent: { type: 'cancelDrag', vertex: gesture.vertex } };
      }
      // A press that never became a drag has written nothing to cancel, so
      // Escape leaves it to the mode machine — which is what makes the first
      // Escape of §4's two-press rule reach the operation and not this.
      if (gesture.kind === 'pending') {
        return { gesture: IDLE_GESTURE, intent: { type: 'none' } };
      }
      return { gesture, intent: { type: 'none' } };
    }
  }
}

/** Whether this gesture has claimed the pointer, so the map must not pan. */
export function claimsPointer(gesture: Gesture): boolean {
  return gesture.kind !== 'idle';
}
