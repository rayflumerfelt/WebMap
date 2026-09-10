/**
 * Saving feature edits. `09-editing.md` §5.3, §13.
 *
 * One request per save, not per change. The dirty buffer already coalesces —
 * a feature moved four times is one delta from its original (§13) — so the
 * payload is derived from the buffer rather than accumulated as edits happen.
 *
 * The interesting part is the id. MapLibre hands back `string | number`, the
 * session keys on strings, and the feature object stores `int64`. The
 * conversion happens here, once, and refuses rather than coercing: `Number()`
 * on a non-numeric id gives `NaN`, and a save carrying `NaN` would be a request
 * the server rejects with a message about a feature that does not exist.
 */

import type { ApiClient } from './client.js';
import type { Feature, FeatureDelta } from '../editing/session.js';

export interface FeatureEditDto {
  feature_id: number;
  geometry?: unknown;
  properties?: Record<string, unknown>;
  deleted?: boolean;
}

export interface SaveEditsResponse {
  version: number;
  feature_count: number;
  bbox_4326: number[];
}

/**
 * The dirty buffer as the request body.
 *
 * A delete carries no new state — the endpoint refuses a change that is both —
 * and an edit carries the whole feature, because properties are replaced whole
 * rather than merged (a merge cannot express clearing a field).
 */
export function toEditPayload(deltas: readonly FeatureDelta[]): FeatureEditDto[] {
  return deltas.map((delta) => {
    const featureId = Number(delta.featureId);
    if (!Number.isInteger(featureId)) {
      throw new Error(
        `Feature id '${delta.featureId}' is not a whole number, so it cannot be ` +
          `saved: feature objects address features by int64 id. A non-numeric id ` +
          `means the tile source's \`promoteId\` names the wrong property.`,
      );
    }

    if (delta.after === null) return { feature_id: featureId, deleted: true };
    return {
      feature_id: featureId,
      geometry: delta.after.geometry,
      properties: delta.after.properties,
    };
  });
}

/**
 * Write a new feature version.
 *
 * `baseVersion` is the version the session was opened against. A 409 means
 * somebody saved first — the caller's edits are still in its buffer, and §5.3's
 * Refresh rebases them onto what landed.
 */
export function saveFeatureEdits(
  api: ApiClient,
  datasetId: string,
  baseVersion: number,
  deltas: readonly FeatureDelta[],
): Promise<SaveEditsResponse> {
  return api.post<SaveEditsResponse>(`/features/${datasetId}/edits`, {
    base_version: baseVersion,
    edits: toEditPayload(deltas),
  });
}

/** What `GET /features/{id}.geojson` returns. */
interface FeatureCollectionDto {
  type: 'FeatureCollection';
  features: Array<{
    id: number | string;
    geometry: unknown;
    properties: Record<string, unknown> | null;
  }>;
}

/**
 * The editable working set: the layer's exact geometry. `09-editing.md` §17,
 * §3.3.
 *
 * **Exact, not tile.** Everything committed is computed from these — a vertex
 * moved against tile geometry is a vertex moved against a simplified copy, and
 * the difference is a boundary that no longer matches its neighbour.
 *
 * The endpoint refuses above 5,000 features rather than truncating, which is
 * the cap §17 asks for and the message says so. A silently partial layer is
 * worse than an error because it looks like the data.
 *
 * `token` is the per-dataset tile token: this endpoint shares the tile
 * principal, because it is the same object read by the same means.
 */
export async function fetchWorkingSet(
  api: ApiClient,
  datasetId: string,
  token?: string | undefined,
): Promise<Feature[]> {
  const collection = await api.get<FeatureCollectionDto>(
    `/features/${datasetId}.geojson`,
    token ? { token } : undefined,
  );

  return collection.features.map((feature) => ({
    // The session keys on strings; the object stores int64. One conversion,
    // here, matching the one `toEditPayload` reverses on the way out.
    id: String(feature.id),
    geometry: feature.geometry,
    properties: feature.properties ?? {},
  }));
}
