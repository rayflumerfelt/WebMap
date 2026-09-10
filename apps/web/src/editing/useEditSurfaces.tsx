/**
 * The editing surfaces, wired to the store. `09-editing.md` §8, §10.
 *
 * The four surfaces were built against `EditState` and a `CommandDef[]` and
 * nothing rendered them, which meant the editor had no way into vertex mode
 * except a test. This is the container that closes that: it derives the state
 * snapshot, binds the handlers, and renders the toolbar, the operation bar,
 * the status strip and the palette.
 *
 * **The handlers are the only new decisions here.** Everything else — which
 * commands exist, which are enabled, what they are called — is the registry's,
 * and a container that re-decided any of it would be the second copy §8 exists
 * to prevent. A command with no handler stays registered and runs as a no-op,
 * which is the honest state of an operation that has not landed.
 */

import { useMemo } from 'react';
import type { ReactNode } from 'react';

import { editState, selectedVertices, useEditStore } from '../stores/editStore.js';
import { CommandPalette } from './CommandPalette.js';
import { EditStatusStrip } from './EditStatusStrip.js';
import type { SnapReadout } from './EditStatusStrip.js';
import { EditToolbar } from './EditToolbar.js';
import type { LayerOption } from './EditToolbar.js';
import { OperationBar } from './OperationBar.js';
import { canSwitchMode } from './modes.js';
import type { EditMode } from './modes.js';
import { buildRegistry } from './registry.js';
import type { Handlers } from './registry.js';
import type { CommandDef } from './types.js';
import { deleteVerticesCommand } from './vertexCommands.js';

export interface EditSurfacesProps {
  /** Layers offered by the active-layer selector, in the layer tree's order. */
  layers: LayerOption[];
  /** The active layer, or null before one is chosen. */
  activeLayerId: string | null;
  onActiveLayer(layerId: string): void;
  onSave(): void;
  onDiscard(): void;
  /** Cursor position in the working CRS, formatted by the caller. */
  cursor: string | null;
  crsLabel: string;
  snap: SnapReadout | null;
  snapClamped?: 'floor' | 'ceiling' | null;
  /** Surfaces a refused operation — the same channel the map handlers use. */
  onError?: (message: string) => void;
  /** Open the command palette. Owned by the caller because `mod+k` is a
   *  global shortcut, not an editing one. */
  paletteOpen: boolean;
  onPaletteOpenChange(open: boolean): void;
}

/**
 * A hook rather than a component: it returns four independently placed
 * pieces — the toolbar goes under the main one, the strip replaces the status
 * bar, the operation bar floats over the map — and a component that rendered
 * all four together would have to know the shell's layout.
 */
export interface EditSurfaces {
  toolbar: ReactNode;
  statusStrip: ReactNode;
  operationBar: ReactNode;
  palette: ReactNode;
}

export function useEditSurfaces(props: EditSurfacesProps): EditSurfaces {
  const store = useEditStore();
  const state = editState(store);

  const commands = useMemo<CommandDef[]>(
    () => buildRegistry(handlersFor(props)),
    // Rebuilt when the callbacks change, which is once. The handlers read the
    // store through `getState()` rather than closing over it, so a stale
    // registry cannot act on stale state.
    [props],
  );

  return {
    toolbar: (
      <EditToolbar
        state={state}
        mode={store.mode.mode}
        selectTool={store.mode.selectTool}
        commands={commands}
        dirtyCount={store.session?.dirty.size ?? 0}
        {...(props.snapClamped !== undefined ? { snapClamped: props.snapClamped } : {})}
        layers={props.layers}
        onMode={(mode: EditMode) => store.dispatch({ type: 'setMode', mode })}
        onSelectTool={(tool) => store.dispatch({ type: 'setSelectTool', tool })}
        onRun={(command) => void command.run(state)}
        onActiveLayer={props.onActiveLayer}
        canSwitchMode={canSwitchMode(store.mode)}
      />
    ),
    statusStrip: (
      <EditStatusStrip
        cursor={props.cursor}
        crsLabel={props.crsLabel}
        snap={props.snap}
        snapDisabled={!store.snap.enabled}
        measurement={null}
        selectedCount={store.mode.selectedFeatureIds.length}
        onClearSelection={() => store.dispatch({ type: 'clearSelection' })}
      />
    ),
    operationBar: store.mode.operationActive ? (
      <OperationBar
        title={labelFor(store.mode.mode)}
        hint={hintFor(store.mode.mode)}
        onApply={() => store.dispatch({ type: 'operationResolved' })}
        onCancel={() => store.dispatch({ type: 'operationResolved' })}
      />
    ) : null,
    palette: (
      <CommandPalette
        open={props.paletteOpen}
        state={state}
        commands={commands}
        onRun={(command) => {
          props.onPaletteOpenChange(false);
          void command.run(state);
        }}
        onClose={() => props.onPaletteOpenChange(false)}
      />
    ),
  };
}

/** What the operation bar calls the operation in flight. */
function labelFor(mode: EditMode): string {
  if (mode === 'vertex') return 'Move Vertex';
  if (mode === 'vertex-add') return 'Add Vertex';
  if (mode.startsWith('draw-')) return 'Draw';
  return mode.charAt(0).toUpperCase() + mode.slice(1);
}

/** One line saying what to do next. Required by §10.2, and rightly: a modal
 *  mode with no instruction is the state where a user clicks once, nothing
 *  visible happens, and they leave. */
function hintFor(mode: EditMode): string {
  if (mode === 'vertex') return 'Drag the handle to move the vertex. Esc to cancel.';
  if (mode === 'vertex-add') {
    return 'Click the boundary of the selected feature to add a vertex. Esc to cancel.';
  }
  return 'Enter to apply, Esc to cancel.';
}

/**
 * Command id to behaviour.
 *
 * Only the ones that are built. The rest stay registered and run as no-ops —
 * §8 is explicit that hiding an unimplemented command makes the menu a moving
 * target as features arrive, and disabling it says the *state* is wrong when
 * the state is fine.
 */
function handlersFor(props: EditSurfacesProps): Handlers {
  const store = () => useEditStore.getState();

  return {
    'session.save': () => props.onSave(),
    'session.discard': () => props.onDiscard(),

    'edit.undo': () => store().undo(),
    'edit.redo': () => store().redo(),

    'select.none': () => store().dispatch({ type: 'clearSelection' }),
    'select.mode.click': () => store().dispatch({ type: 'setSelectTool', tool: 'click' }),
    'select.mode.rectangle': () =>
      store().dispatch({ type: 'setSelectTool', tool: 'rectangle' }),
    'select.mode.lasso': () => store().dispatch({ type: 'setSelectTool', tool: 'lasso' }),

    'vertex.mode': () => store().dispatch({ type: 'setMode', mode: 'vertex' }),
    'vertex.add': () => store().dispatch({ type: 'setMode', mode: 'vertex-add' }),
    'vertex.delete': () => {
      const live = store();
      if (!live.session) return;
      try {
        live.applyCommand(deleteVerticesCommand(live.session, selectedVertices(live)));
      } catch (error) {
        props.onError?.(error instanceof Error ? error.message : String(error));
      }
    },
    'vertex.selectAll': () => {
      // Handled where the geometry is: the surfaces know which features are
      // selected and not how many vertices each has.
      props.onError?.('Select All Vertices is not built yet.');
    },

    'snap.enable': () => store().setSnap({ enabled: !store().snap.enabled }),
    'snap.topological': () => store().toggle('topologicalEditing'),
    'snap.angle': () => store().toggle('angleConstraint'),

    'view.showVertices': () => store().toggle('showVertices'),
    'view.showMeasurements': () => store().toggle('showMeasurements'),
    'view.showValidationErrors': () => store().toggle('showValidationErrors'),
  };
}
