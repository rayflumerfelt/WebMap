/**
 * Session and dataset queries. `07-frontend.md` §7, §8.
 *
 * Server state, so TanStack Query owns it (§3) — distinct from the session
 * *document*, which is Zustand's. The split is deliberate and easy to blur:
 * the query holds what the server last said, and the store holds what the user
 * has done since. Autosave is the bridge.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { Symbology } from '@webmap/style-model';

import type { ApiClient } from './client.js';
import type { SessionSnapshot } from '../stores/sessionStore.js';

export interface DatasetRef {
  id: string;
  name: string;
  kind: string;
  geometry_kind: string | null;
  storage_srid: number;
  feature_count: number | null;
  bbox_4326: number[] | null;
  value_min: number | null;
  value_max: number | null;
  version: number;
}

export interface SessionLayerDto {
  dataset_id: string;
  style_template_id: string | null;
  symbology_override: Symbology | null;
  opacity: number;
  visible: boolean;
  z: number;
  dataset: DatasetRef;
}

export interface SessionDto {
  id: string;
  short_code: string;
  name: string | null;
  project_id: string | null;
  layers: SessionLayerDto[];
  view: { center?: [number, number]; zoom?: number; bbox?: number[] };
  updated_at: string;
  /** How many layers were withheld because this reader cannot see them.
   *  Surfaced in the UI rather than swallowed — see the sessions service. */
  hidden_layer_count: number;
}

export const sessionKeys = {
  all: ['sessions'] as const,
  detail: (key: string) => [...sessionKeys.all, 'detail', key] as const,
};

export function useSession(api: ApiClient, key: string | undefined) {
  return useQuery({
    queryKey: sessionKeys.detail(key ?? ''),
    queryFn: () => api.get<SessionDto>(`/sessions/${key}`),
    enabled: Boolean(key),
    // A session document changes only when someone saves it, and this client
    // is usually the one saving. Refetching on window focus would clobber the
    // in-progress local state with a copy of what we ourselves last wrote.
    refetchOnWindowFocus: false,
    retry: (failureCount, error) => {
      // Never retry a refusal: a 403 or 404 will refuse identically next time,
      // and the message is already the answer.
      const status = (error as { status?: number }).status;
      if (status !== undefined && status >= 400 && status < 500) return false;
      return failureCount < 2;
    },
  });
}

export function useSaveSession(api: ApiClient, sessionId: string | null) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      snapshot,
      expectedUpdatedAt,
      autosave,
    }: {
      snapshot: SessionSnapshot;
      expectedUpdatedAt: string | null;
      autosave: boolean;
    }) => {
      if (!sessionId) throw new Error('No session to save.');
      return api.patch<{ updated: boolean; updated_at: string }>(
        `/sessions/${sessionId}`,
        {
          layers: snapshot.layers,
          view: snapshot.view,
          ...(expectedUpdatedAt ? { expected_updated_at: expectedUpdatedAt } : {}),
        },
        // The query parameter suppresses the audit record. The browser saves
        // every few seconds while a user pans; recording each one would bury
        // the events `03` §10 exists to preserve.
        { autosave: autosave ? 'true' : 'false' },
      );
    },
    onSuccess: (result) => {
      if (!sessionId) return;
      // Update the cached `updated_at` in place rather than refetching: the
      // next save needs it for optimistic concurrency, and a refetch would
      // replace the layers the user is still editing.
      queryClient.setQueryData<SessionDto>(sessionKeys.detail(sessionId), (previous) =>
        previous ? { ...previous, updated_at: result.updated_at } : previous,
      );
    },
  });
}

export interface ProjectDto {
  id: string;
  name: string;
  analysis_srid: number;
  horizontal_unit: 'ft' | 'usft' | 'm';
  /** WKT for the analysis CRS, for the status bar's cursor readout. */
  crs_wkt: string;
}

export function useProject(api: ApiClient, projectId: string | null | undefined) {
  return useQuery({
    queryKey: ['projects', 'detail', projectId],
    queryFn: () => api.get<ProjectDto>(`/projects/${projectId}`),
    enabled: Boolean(projectId),
    // A project's CRS cannot change (`projects.update_project` refuses it),
    // so this is as close to immutable as anything the API serves.
    staleTime: Number.POSITIVE_INFINITY,
  });
}

/**
 * A scoped tile token for one dataset. `03-auth-security.md` §6.1.
 *
 * Minted per dataset because a tile URL ends up in devtools, in bug reports
 * and in screenshots — scoping means a leak exposes exactly one layer the
 * leaker could already see.
 */
export function useTileToken(api: ApiClient, datasetId: string | null) {
  return useQuery({
    queryKey: ['tile-token', datasetId],
    queryFn: () =>
      api.post<{ token: string; expires_in: number }>(`/datasets/${datasetId}/tile-token`),
    enabled: Boolean(datasetId),
    // Refreshed at 80% of its life. Letting it expire mid-pan turns every
    // subsequent tile into a 401 and the map into a checkerboard of holes.
    refetchInterval: (query) => (query.state.data ? query.state.data.expires_in * 800 : false),
    staleTime: 0,
  });
}

export interface AttributePage {
  items: Array<Record<string, unknown>>;
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
}

/**
 * A page of a layer's attributes, without geometry.
 *
 * Not the GeoJSON endpoint: that refuses above the switch threshold rather
 * than truncating (06-rendering.md §7.1), which is right for a map source and
 * left a 500k-feature layer with no attribute view at all. This pages, and
 * reads no geometry — a column of porosities costs a column of porosities
 * rather than megabytes of coordinates.
 */
export function useAttributes(
  api: ApiClient,
  datasetId: string | null,
  options: { offset?: number; limit?: number; orderBy?: string; descending?: boolean } = {},
) {
  const { offset = 0, limit = 500, orderBy, descending } = options;

  return useQuery({
    queryKey: ['attributes', datasetId, offset, limit, orderBy, descending],
    queryFn: () =>
      api.get<AttributePage>(`/features/${datasetId}/attributes`, {
        offset,
        limit,
        ...(orderBy ? { order_by: orderBy } : {}),
        ...(descending ? { descending: 'true' } : {}),
      }),
    enabled: Boolean(datasetId),
    // Keeps the previous page on screen while the next loads, so paging does
    // not flash an empty table between pages.
    placeholderData: (previous) => previous,
    staleTime: 5 * 60_000,
    retry: false,
  });
}
