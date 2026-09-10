/**
 * Import and export a palette. `07-frontend.md` §6.3, `08` §5.1.
 *
 * **Geologists have existing palettes and will insist on using them.** A tool
 * that supports only hand-built ramps gets rejected, so this control accepts
 * Surfer `.clr`, GMT `.cpt`, a QGIS ramp `.xml` and WebMap's own `.json`.
 *
 * **It parses nothing.** The file's text goes to `onImport` and the parsing
 * happens server-side, in `webmap_core.style.palette_io` — one implementation
 * rather than a TypeScript copy that drifts from it. A `.cpt` with hard breaks,
 * named colours and B/F/N lines is exactly the kind of format where two
 * implementations disagree quietly, and the disagreement shows up as a map
 * that looks slightly wrong.
 *
 * Export is a download the browser makes from a Blob, so it works without a
 * round trip for the format WebMap already holds. Formats that need conversion
 * are the caller's business: `onExport` returns the text.
 */

import { useId, useRef, useState } from 'react';
import { caution, control, ghostButton, hint, label, row, stack } from './styles.js';

export type PaletteFormat = 'clr' | 'cpt' | 'xml' | 'json';

export interface PaletteIOProps {
  /** Called with the file's text and its detected format. */
  onImport(text: string, format: PaletteFormat, filename: string): Promise<void> | void;
  /** Produce the export text for a format. */
  onExport(format: PaletteFormat): Promise<string> | string;
  /** Suggested download name, without an extension. */
  name: string;
  /** Set while an import is in flight, and cleared by the caller. */
  error?: string | null;
  className?: string;
}

const ACCEPT = '.clr,.cpt,.xml,.json';

const FORMATS: Array<{ id: PaletteFormat; name: string; source: string }> = [
  { id: 'clr', name: 'Surfer colour spec (.clr)', source: 'Golden Software Surfer' },
  { id: 'cpt', name: 'GMT colour palette (.cpt)', source: 'GMT, cpt-city' },
  { id: 'xml', name: 'QGIS colour ramp (.xml)', source: 'QGIS style exports' },
  { id: 'json', name: 'WebMap palette (.json)', source: 'Round-trip' },
];

/** The format a filename implies, or null when the extension is unknown. */
export function formatOf(filename: string): PaletteFormat | null {
  const extension = filename.toLowerCase().split('.').pop() ?? '';
  return FORMATS.some((format) => format.id === extension)
    ? (extension as PaletteFormat)
    : null;
}

export function PaletteIO({ onImport, onExport, name, error, className }: PaletteIOProps) {
  const id = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const [exportFormat, setExportFormat] = useState<PaletteFormat>('json');
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const handleFile = async (file: File) => {
    const format = formatOf(file.name);
    if (!format) {
      setLocalError(
        `${file.name} is not a palette file. Supported: .clr (Surfer), .cpt (GMT), ` +
          `.xml (QGIS) and .json (WebMap).`,
      );
      return;
    }
    setLocalError(null);
    setBusy(true);
    try {
      await onImport(await file.text(), format, file.name);
    } finally {
      setBusy(false);
      // Cleared so re-importing the *same* file after fixing it fires a change
      // event; without this the second attempt silently does nothing.
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  const download = async () => {
    setBusy(true);
    try {
      const text = await onExport(exportFormat);
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${name}.${exportFormat}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={className} style={stack}>
      <div
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          const file = event.dataTransfer.files[0];
          if (file) void handleFile(file);
        }}
        style={{
          border: '1px dashed #c9ccd1',
          borderRadius: 3,
          padding: '10px 12px',
          textAlign: 'center',
          background: '#fafbfc',
        }}
      >
        <input
          ref={fileInput}
          id={`${id}-file`}
          type="file"
          accept={ACCEPT}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void handleFile(file);
          }}
          style={{ position: 'absolute', width: 1, height: 1, opacity: 0, pointerEvents: 'none' }}
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => fileInput.current?.click()}
          style={ghostButton}
        >
          {busy ? 'Reading…' : 'Import a palette'}
        </button>
        <p style={{ ...hint, marginTop: 6 }}>
          or drop a .clr, .cpt, .xml or .json file here
        </p>
      </div>

      <div style={row}>
        <label htmlFor={`${id}-format`} style={label}>
          Export as
        </label>
        <select
          id={`${id}-format`}
          value={exportFormat}
          onChange={(event) => setExportFormat(event.target.value as PaletteFormat)}
          style={{ ...control, width: 208 }}
        >
          {FORMATS.map((format) => (
            <option key={format.id} value={format.id}>
              {format.name}
            </option>
          ))}
        </select>
        <button type="button" disabled={busy} onClick={() => void download()} style={ghostButton}>
          Export
        </button>
      </div>

      {localError ?? error ? <p style={caution}>{localError ?? error}</p> : null}
    </div>
  );
}
