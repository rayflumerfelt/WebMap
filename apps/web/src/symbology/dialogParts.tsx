/**
 * Layout for the formatting dialog. `07-frontend.md` §5.1, §5.3, §6.2.
 *
 * Desktop is the base case, so a section is a heading and a stack — no
 * accordion, no tabs. The whole dialog is meant to be legible at once on a
 * 1440 px screen: a geologist comparing a fill colour against a label colour
 * should not have to remember one while opening the other, which is precisely
 * what collapsing sections costs.
 *
 * `<details>` is used anyway for the sections a layer usually does not touch,
 * open by default, so the structure survives a small viewport without any
 * `max-width` query — the desktop-first rule holds with no exception.
 */

import type { CSSProperties, ReactNode } from 'react';

export const panel: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 14,
  fontSize: 12,
  padding: 12,
  overflowY: 'auto',
};

export const fieldRow: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  minHeight: 'var(--row-h, 26px)',
};

const heading: CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
  color: '#4a4f57',
  cursor: 'default',
  padding: '2px 0',
};

export function Section({
  title,
  children,
  collapsible = false,
}: {
  title: string;
  children: ReactNode;
  /** Only for sections a layer usually leaves alone. Open by default either
   *  way — a collapsed section is a section people do not know exists. */
  collapsible?: boolean;
}) {
  if (!collapsible) {
    return (
      <section style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <h3 style={heading}>{title}</h3>
        {children}
      </section>
    );
  }

  return (
    <details open style={{ display: 'flex', flexDirection: 'column' }}>
      <summary style={{ ...heading, cursor: 'pointer' }}>{title}</summary>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, paddingTop: 6 }}>
        {children}
      </div>
    </details>
  );
}
