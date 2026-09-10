/**
 * The formatting dialog with its data attached. `07-frontend.md` §5.5, §6.2.
 *
 * Kept separate for the same reason `AttributePanel` is kept apart from
 * `AttributeTable`: §5.5 wants panels detachable into a second window later,
 * and a component that fetches its own data assumes a query client and an API
 * in scope. `FormattingDialog` stays a pure component that takes a symbology
 * and returns one.
 *
 * **The summary is fetched for the column being coloured by, not for the
 * layer.** A distinct-values query over half a million features is expensive
 * and pointless for the thirty-nine columns nobody is looking at; switching the
 * column is what triggers the fetch, and the answer is cached for as long as
 * somebody is likely to keep picking colours.
 */

import type { Palette, Symbology } from '@webmap/style-model';
import type { FontFamily } from '@webmap/ui';
import { useMemo } from 'react';

import type { ApiClient } from '../api/client.js';
import { useAttributeSummary } from '../api/sessions.js';
import { FormattingDialog } from './FormattingDialog.js';
import type { AttributeSummary } from './FormattingDialog.js';
import { colouredColumn } from './FormattingDialog.js';

export interface FormattingDialogContainerProps {
  api: ApiClient | undefined;
  datasetId: string;
  layerName: string;
  symbology: Symbology;
  onChange(symbology: Symbology): void;
  fields: Array<{ name: string; type: 'text' | 'number' }>;
  palettes: Record<string, Palette>;
  fonts: FontFamily[];
}

export function FormattingDialogContainer(props: FormattingDialogContainerProps) {
  const { api, datasetId, symbology, ...rest } = props;
  const column = colouredColumn(symbology);
  const query = useAttributeSummary(api!, api ? datasetId : null, column);

  const summary = useMemo<AttributeSummary | undefined>(() => {
    const data = query.data;
    if (!data) return undefined;
    if (data.kind === 'text') {
      return {
        // The colour is filled in by the dialog from the existing symbology;
        // the endpoint knows counts, not colours, and inventing one here would
        // overwrite a palette somebody had already chosen.
        categories: data.categories.map((entry) => ({
          value: entry.value,
          color: '#cccccc',
          count: entry.count,
        })),
        remaining: data.remaining,
        refused: data.refused,
      };
    }
    return { histogram: data.histogram, domain: data.domain };
  }, [query.data]);

  return <FormattingDialog symbology={symbology} summary={summary} {...rest} />;
}
