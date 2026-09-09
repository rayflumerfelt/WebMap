/**
 * Layer tree. `07-frontend.md` §6.
 *
 * Dense by design (§5.3): 26 px rows, so twenty layers are visible without
 * scrolling. A tree showing ten comfortably is less useful than one showing
 * twenty, because comparing layers is the reason the panel exists.
 *
 * Two obligations follow from that density and from §10:
 *
 * - **Every hover-revealed action also appears on focus and is tab-reachable.**
 *   A hover-only affordance is invisible to a keyboard user, and at 26 px it
 *   is nearly invisible to a mouse user in a hurry too.
 * - **Reordering works from the keyboard.** Drag-and-drop is the fast path,
 *   not the only path — `Alt+ArrowUp`/`Alt+ArrowDown` move the focused layer.
 *
 * Each row's swatch is rendered from the *compiled* style rather than from the
 * symbology model, so the tree and the map cannot disagree about what a layer
 * looks like.
 */

import type { CompiledLayer, Symbology } from '@webmap/style-model';
import { compileSymbology } from '@webmap/style-model';
import type { Palette } from '@webmap/style-model';
import { useCallback, useId, useState } from 'react';

export interface SessionLayerView {
  id: string;
  datasetId: string;
  name: string;
  symbology: Symbology;
  opacity: number;
  visible: boolean;
}

export interface LayerTreeProps {
  layers: SessionLayerView[];
  palettes: Record<string, Palette>;
  selectedId: string | null;
  onSelect(id: string): void;
  /** Move the layer at `from` to `to`, both indices into `layers`. */
  onReorder(from: number, to: number): void;
  onToggleVisibility(id: string): void;
  onOpacityChange(id: string, opacity: number): void;
  onRemove(id: string): void;
  onZoomTo?(id: string): void;
  /** Basemap layers render in a locked group at the bottom: a user
   *  preference, not a per-session choice, so they are not reorderable
   *  relative to data layers. */
  basemapLayers?: Array<{ id: string; name: string; visible: boolean }>;
  className?: string;
}

export function LayerTree(props: LayerTreeProps) {
  const {
    layers,
    palettes,
    selectedId,
    onSelect,
    onReorder,
    onToggleVisibility,
    onOpacityChange,
    onRemove,
    onZoomTo,
    basemapLayers = [],
    className,
  } = props;

  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const listId = useId();

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent, index: number, layer: SessionLayerView) => {
      // Alt+Arrow reorders. Plain arrows are left to the browser so a screen
      // reader's own navigation is not hijacked.
      if (event.altKey && event.key === 'ArrowUp' && index > 0) {
        event.preventDefault();
        onReorder(index, index - 1);
        return;
      }
      if (event.altKey && event.key === 'ArrowDown' && index < layers.length - 1) {
        event.preventDefault();
        onReorder(index, index + 1);
        return;
      }
      // §10: every right-click menu is also on Shift+F10 and the context-menu
      // key, with identical contents. Right-click-only is inaccessible.
      if ((event.shiftKey && event.key === 'F10') || event.key === 'ContextMenu') {
        event.preventDefault();
        setMenuFor(layer.id);
        return;
      }
      if (event.key === 'Escape' && menuFor) {
        event.preventDefault();
        setMenuFor(null);
      }
    },
    [layers.length, menuFor, onReorder],
  );

  return (
    <div className={className} style={{ display: 'flex', flexDirection: 'column' }}>
      <ul
        id={listId}
        aria-label="Layers"
        style={{ listStyle: 'none', margin: 0, padding: 0, flex: 1, overflowY: 'auto' }}
      >
        {layers.map((layer, index) => (
          <LayerRow
            key={layer.id}
            layer={layer}
            index={index}
            total={layers.length}
            palettes={palettes}
            selected={layer.id === selectedId}
            menuOpen={menuFor === layer.id}
            dragging={dragIndex === index}
            onSelect={onSelect}
            onKeyDown={handleKeyDown}
            onOpenMenu={setMenuFor}
            onToggleVisibility={onToggleVisibility}
            onOpacityChange={onOpacityChange}
            onRemove={onRemove}
            {...(onZoomTo ? { onZoomTo } : {})}
            onDragStart={() => setDragIndex(index)}
            onDragEnd={() => setDragIndex(null)}
            onDrop={() => {
              if (dragIndex !== null && dragIndex !== index) onReorder(dragIndex, index);
              setDragIndex(null);
            }}
          />
        ))}
      </ul>

      {basemapLayers.length > 0 ? (
        <div
          style={{
            borderTop: '1px solid rgba(36, 46, 57, 0.15)',
            paddingTop: 4,
            marginTop: 4,
            // Visually distinct, and not reorderable relative to data layers.
            opacity: 0.85,
          }}
        >
          <div style={{ fontSize: 10, textTransform: 'uppercase', opacity: 0.7, padding: '0 6px' }}>
            Basemap
          </div>
          <ul aria-label="Basemap layers" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {basemapLayers.map((base) => (
              <li
                key={base.id}
                style={{ height: 'var(--row-h, 26px)', display: 'flex', alignItems: 'center', padding: '0 6px' }}
              >
                <span style={{ fontSize: 12 }}>{base.name}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

interface LayerRowProps {
  layer: SessionLayerView;
  index: number;
  total: number;
  palettes: Record<string, Palette>;
  selected: boolean;
  menuOpen: boolean;
  dragging: boolean;
  onSelect(id: string): void;
  onKeyDown(event: React.KeyboardEvent, index: number, layer: SessionLayerView): void;
  onOpenMenu(id: string | null): void;
  onToggleVisibility(id: string): void;
  onOpacityChange(id: string, opacity: number): void;
  onRemove(id: string): void;
  onZoomTo?(id: string): void;
  onDragStart(): void;
  onDragEnd(): void;
  onDrop(): void;
}

function LayerRow(props: LayerRowProps) {
  const { layer, index, total, selected, menuOpen } = props;
  const [hovered, setHovered] = useState(false);
  const [focusWithin, setFocusWithin] = useState(false);

  // §10: hover-revealed actions must also appear on focus. Selection counts
  // too — the selected row's actions stay visible, because that is the row
  // the user is working on.
  const showActions = hovered || focusWithin || selected;

  return (
    <li
      style={{ position: 'relative' }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocusWithin(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node)) setFocusWithin(false);
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        props.onOpenMenu(layer.id);
      }}
      draggable
      onDragStart={props.onDragStart}
      onDragEnd={props.onDragEnd}
      onDragOver={(event) => event.preventDefault()}
      onDrop={props.onDrop}
    >
      <div
        role="option"
        aria-selected={selected}
        aria-label={`${layer.name}, layer ${index + 1} of ${total}`}
        tabIndex={0}
        onClick={() => props.onSelect(layer.id)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            props.onSelect(layer.id);
            return;
          }
          props.onKeyDown(event, index, layer);
        }}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          height: 'var(--row-h, 26px)',
          padding: '0 6px',
          fontSize: 12,
          cursor: 'default',
          background: selected ? 'rgba(43, 147, 179, 0.16)' : 'transparent',
          opacity: props.dragging ? 0.5 : 1,
        }}
      >
        <VisibilityToggle
          visible={layer.visible}
          name={layer.name}
          onToggle={() => props.onToggleVisibility(layer.id)}
        />
        <Swatch layer={layer} palettes={props.palettes} />
        <span
          style={{
            flex: 1,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            // Colour is never the only channel (§10): a hidden layer is dimmed
            // *and* its toggle reads "hidden" to a screen reader.
            opacity: layer.visible ? 1 : 0.45,
          }}
        >
          {layer.name}
        </span>

        {showActions ? (
          <RowActions
            layer={layer}
            onOpacityChange={props.onOpacityChange}
            onRemove={props.onRemove}
          />
        ) : null}
      </div>

      {menuOpen ? (
        <ContextMenu
          layer={layer}
          onClose={() => props.onOpenMenu(null)}
          onRemove={props.onRemove}
          {...(props.onZoomTo ? { onZoomTo: props.onZoomTo } : {})}
        />
      ) : null}
    </li>
  );
}

function VisibilityToggle({
  visible,
  name,
  onToggle,
}: {
  visible: boolean;
  name: string;
  onToggle(): void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={visible}
      aria-label={`${visible ? 'Hide' : 'Show'} ${name}`}
      onClick={(event) => {
        event.stopPropagation();
        onToggle();
      }}
      style={{
        // --hit-slop: the clickable area exceeds the painted one. Precision at
        // 26 px rows depends on it (§10).
        padding: 'var(--hit-slop, 4px)',
        margin: 'calc(-1 * var(--hit-slop, 4px))',
        border: 0,
        background: 'transparent',
        cursor: 'pointer',
        lineHeight: 0,
      }}
    >
      <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true">
        {visible ? (
          <path
            d="M8 3C4.5 3 1.7 5.6 1 8c.7 2.4 3.5 5 7 5s6.3-2.6 7-5c-.7-2.4-3.5-5-7-5zm0 8a3 3 0 110-6 3 3 0 010 6z"
            fill="currentColor"
          />
        ) : (
          <path
            d="M2 2l12 12M8 3c3.5 0 6.3 2.6 7 5a9 9 0 01-2 3M5 5.5A9 9 0 001 8c.7 2.4 3.5 5 7 5a8 8 0 003-.6"
            stroke="currentColor"
            strokeWidth="1.5"
            fill="none"
          />
        )}
      </svg>
    </button>
  );
}

/**
 * A swatch rendered from the compiled style, not from the symbology model.
 *
 * §6: "so the tree and the map cannot disagree." Reading the model directly
 * would work today and drift the first time the compiler makes a decision the
 * model does not spell out — which it already does for graduated fills, where
 * the colours come from sampling a ramp.
 */
function Swatch({
  layer,
  palettes,
}: {
  layer: SessionLayerView;
  palettes: Record<string, Palette>;
}) {
  let colour = '#8e9dad';
  try {
    const compiled = compileSymbology(layer.symbology, {
      sourceId: 'swatch',
      palettes,
      idPrefix: 'swatch',
    });
    colour = firstColour(compiled) ?? colour;
  } catch {
    // A symbology the compiler refuses — a missing palette, mismatched breaks
    // — must not take the whole panel down with it. The neutral swatch is the
    // signal; the error itself surfaces where the map fails to draw.
  }

  return (
    <span
      aria-hidden="true"
      style={{
        width: 12,
        height: 12,
        flex: '0 0 auto',
        background: colour,
        border: '1px solid rgba(36, 46, 57, 0.35)',
      }}
    />
  );
}

function firstColour(layers: CompiledLayer[]): string | undefined {
  for (const layer of layers) {
    for (const key of ['fill-color', 'line-color', 'circle-color', 'text-color']) {
      const value = layer.paint?.[key];
      if (typeof value === 'string') return value;
      // A data-driven expression: take its first literal colour, which is the
      // lowest class — the same one the legend shows first.
      if (Array.isArray(value)) {
        const literal = value.find((item) => typeof item === 'string' && item.startsWith('#'));
        if (typeof literal === 'string') return literal;
      }
    }
  }
  return undefined;
}

function RowActions({
  layer,
  onOpacityChange,
  onRemove,
}: {
  layer: SessionLayerView;
  onOpacityChange(id: string, opacity: number): void;
  onRemove(id: string): void;
}) {
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={layer.opacity}
        aria-label={`Opacity of ${layer.name}`}
        onClick={(event) => event.stopPropagation()}
        onChange={(event) => onOpacityChange(layer.id, Number(event.target.value))}
        style={{ width: 56, height: 12 }}
      />
      <button
        type="button"
        aria-label={`Remove ${layer.name}`}
        onClick={(event) => {
          event.stopPropagation();
          onRemove(layer.id);
        }}
        style={{
          padding: 'var(--hit-slop, 4px)',
          margin: 'calc(-1 * var(--hit-slop, 4px))',
          border: 0,
          background: 'transparent',
          cursor: 'pointer',
          fontSize: 12,
          lineHeight: 1,
        }}
      >
        ×
      </button>
    </span>
  );
}

function ContextMenu({
  layer,
  onClose,
  onRemove,
  onZoomTo,
}: {
  layer: SessionLayerView;
  onClose(): void;
  onRemove(id: string): void;
  onZoomTo?(id: string): void;
}) {
  return (
    <ul
      role="menu"
      aria-label={`${layer.name} actions`}
      onKeyDown={(event) => {
        if (event.key === 'Escape') onClose();
      }}
      style={{
        position: 'absolute',
        zIndex: 10,
        left: 24,
        top: '100%',
        minWidth: 150,
        margin: 0,
        padding: 2,
        listStyle: 'none',
        background: '#ffffff',
        border: '1px solid rgba(36, 46, 57, 0.25)',
        boxShadow: '0 2px 8px rgba(36, 46, 57, 0.18)',
        fontSize: 12,
      }}
    >
      {onZoomTo ? (
        <MenuItem
          label="Zoom to layer"
          onSelect={() => {
            onZoomTo(layer.id);
            onClose();
          }}
        />
      ) : null}
      <MenuItem
        label="Remove layer"
        onSelect={() => {
          onRemove(layer.id);
          onClose();
        }}
      />
    </ul>
  );
}

function MenuItem({ label, onSelect }: { label: string; onSelect(): void }) {
  return (
    <li>
      <button
        type="button"
        role="menuitem"
        onClick={onSelect}
        style={{
          display: 'block',
          width: '100%',
          textAlign: 'left',
          padding: '4px 8px',
          border: 0,
          background: 'transparent',
          cursor: 'pointer',
          font: 'inherit',
        }}
      >
        {label}
      </button>
    </li>
  );
}
