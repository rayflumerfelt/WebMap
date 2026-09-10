/**
 * Press, move, drop. `09-editing.md` §11.6, §4.
 *
 * The ambiguity these tests pin down is that a press on a handle is not yet
 * anything: held still it selects, moved it drags, and getting that wrong
 * fills a layer with sub-foot edits nobody made.
 */

import { describe, expect, it } from 'vitest';

import {
  DRAG_THRESHOLD_PX,
  IDLE_GESTURE,
  claimsPointer,
  step,
} from './gestures.js';
import type { Gesture } from './gestures.js';

const VERTEX = { featureId: 'lease', ring: 0, ordinal: 2 };

function press(additive = false): Gesture {
  return step(IDLE_GESTURE, {
    type: 'down',
    point: { x: 100, y: 100 },
    handle: VERTEX,
    additive,
  }).gesture;
}

describe('a press that does not move', () => {
  it('selects the vertex on release', () => {
    const result = step(press(), { type: 'up', point: { x: 100, y: 100 } });

    expect(result.intent).toEqual({ type: 'selectVertex', vertex: VERTEX, additive: false });
    expect(result.gesture).toEqual(IDLE_GESTURE);
  });

  it('carries the additive flag through, for shift-click', () => {
    const result = step(press(true), { type: 'up', point: { x: 100, y: 100 } });

    expect(result.intent).toMatchObject({ type: 'selectVertex', additive: true });
  });

  it('stays a selection within the threshold', () => {
    // Without this every click on a handle moves the vertex by whatever pixel
    // the hand shook, and the dirty buffer never empties.
    const moved = step(press(), {
      type: 'move',
      point: { x: 100 + DRAG_THRESHOLD_PX, y: 100 },
    });

    expect(moved.intent).toEqual({ type: 'none' });
    expect(moved.gesture.kind).toBe('pending');
  });
});

describe('a press that moves', () => {
  function dragged() {
    return step(press(), { type: 'move', point: { x: 120, y: 100 } });
  }

  it('becomes a drag past the threshold', () => {
    expect(dragged().gesture.kind).toBe('dragging');
  });

  it('asks for the drag to begin before it asks for a preview', () => {
    // §6.6 resolves exact geometry on the gesture start, and §6.5 caches
    // projections there. A first frame that previewed before either would
    // jump once the exact coordinates arrived.
    expect(dragged().intent).toEqual({ type: 'beginDrag', vertex: VERTEX });
  });

  it('previews on every later move without writing anything', () => {
    // §5.1: nothing reaches the dirty buffer until the operation resolves.
    const moving = step(dragged().gesture, { type: 'move', point: { x: 130, y: 105 } });

    expect(moving.intent).toEqual({
      type: 'previewDrag',
      vertex: VERTEX,
      point: { x: 130, y: 105 },
    });
    expect(moving.gesture.kind).toBe('dragging');
  });

  it('commits where it was dropped', () => {
    const dropped = step(dragged().gesture, { type: 'up', point: { x: 140, y: 110 } });

    expect(dropped.intent).toEqual({
      type: 'commitDrag',
      vertex: VERTEX,
      point: { x: 140, y: 110 },
    });
    expect(dropped.gesture).toEqual(IDLE_GESTURE);
  });

  it('does not also select the vertex it dragged', () => {
    // One intent per step: a drop that both committed and selected would need
    // an order between the two, and the order would be the bug.
    const dropped = step(dragged().gesture, { type: 'up', point: { x: 140, y: 110 } });

    expect(dropped.intent.type).not.toBe('selectVertex');
  });
});

describe('escape', () => {
  it('cancels a drag in flight', () => {
    // A drag that could only end by dropping the vertex somewhere would have
    // no way out.
    const dragging = step(press(), { type: 'move', point: { x: 120, y: 100 } }).gesture;

    expect(step(dragging, { type: 'escape' }).intent).toEqual({
      type: 'cancelDrag',
      vertex: VERTEX,
    });
  });

  it('leaves a press that never moved to the mode machine', () => {
    // §4's first Escape belongs to the operation; a press with nothing written
    // has nothing to cancel, so it must not swallow the press.
    expect(step(press(), { type: 'escape' }).intent).toEqual({ type: 'none' });
  });

  it('does nothing when idle', () => {
    expect(step(IDLE_GESTURE, { type: 'escape' })).toEqual({
      gesture: IDLE_GESTURE,
      intent: { type: 'none' },
    });
  });
});

describe('a press on empty space', () => {
  it('is not this machine’s business', () => {
    // Feature selection is a click on the map. Mixing the two here produces a
    // drag that also changes the selection underneath it.
    const result = step(IDLE_GESTURE, {
      type: 'down',
      point: { x: 10, y: 10 },
      handle: null,
      additive: false,
    });

    expect(result).toEqual({ gesture: IDLE_GESTURE, intent: { type: 'none' } });
  });

  it('ignores a move with no press behind it', () => {
    // Hover fires constantly; only snapping cares, and it reads the pointer
    // directly rather than through a gesture.
    expect(step(IDLE_GESTURE, { type: 'move', point: { x: 1, y: 1 } }).intent).toEqual({
      type: 'none',
    });
  });
});

describe('claimsPointer', () => {
  it('is true from the press, not from the threshold', () => {
    // The map must not pan while a press is being decided, or a drag that
    // starts slowly pans instead of dragging.
    expect(claimsPointer(press())).toBe(true);
    expect(claimsPointer(IDLE_GESTURE)).toBe(false);
  });
});
