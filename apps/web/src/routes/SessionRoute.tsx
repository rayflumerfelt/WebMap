/**
 * The route Claude links to. `07-frontend.md` §8.
 *
 * Loads a session by short code, pours it into the store, starts autosave, and
 * renders the shell. It is the only place that knows both the API and the
 * store, which is what keeps `App` free of either.
 *
 * **Progressive rather than blocking.** A geologist arriving from a Claude
 * link should see something within a second, not a spinner for the full load:
 * the map opens at the session's view as soon as that is known, and layers
 * appear as their tile tokens are minted.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import { App } from '../App.js';
import { ApiClient } from '../api/client.js';
import { useProject, useSession } from '../api/sessions.js';
import type { SessionDto } from '../api/sessions.js';
import { startAutosave } from '../stores/autosave.js';
import { snapshotOf, useSessionStore } from '../stores/sessionStore.js';
import type { SessionState } from '../stores/sessionStore.js';
import { SessionError } from './SessionError.js';
import { MapSkeleton } from './MapSkeleton.js';

export interface SessionRouteProps {
  shortCode: string;
  api?: ApiClient;
}

export function SessionRoute({ shortCode, api: injected }: SessionRouteProps) {
  const api = useMemo(() => injected ?? new ApiClient(), [injected]);
  const session = useSession(api, shortCode);
  const project = useProject(api, session.data?.project_id);
  const [tileTokens, setTileTokens] = useState<Record<string, string>>({});

  // Pour the loaded document into the store, once per session id. Keyed on the
  // id rather than on the object: React Query hands back a new object
  // reference on every refetch, and reloading the store from one would discard
  // whatever the user had changed since.
  const loadedId = useRef<string | null>(null);
  useEffect(() => {
    const data = session.data;
    if (!data || loadedId.current === data.id) return;
    loadedId.current = data.id;
    useSessionStore.getState().load(toStoreSession(data));
  }, [session.data]);

  // Mint a scoped tile token per dataset. Sequential rather than parallel over
  // a large session would be slow, but a burst of one request per layer is
  // what the endpoint is for — the check happens once per token, not per tile.
  useEffect(() => {
    const layers = session.data?.layers;
    if (!layers?.length) return;
    let cancelled = false;

    void Promise.all(
      layers.map(async (layer) => {
        try {
          const minted = await api.post<{ token: string }>(
            `/datasets/${layer.dataset_id}/tile-token`,
          );
          return [layer.dataset_id, minted.token] as const;
        } catch {
          // A layer whose token cannot be minted simply does not draw. The
          // session still opens with the rest — one restricted or missing
          // dataset must not blank a map that is otherwise fine.
          return null;
        }
      }),
    ).then((results) => {
      if (cancelled) return;
      setTileTokens(Object.fromEntries(results.filter((r) => r !== null)));
    });

    return () => {
      cancelled = true;
    };
  }, [api, session.data?.layers]);

  // Autosave. Started once the session is in the store, stopped on unmount so
  // a navigation away does not leave a timer writing to a session nobody is
  // looking at.
  useEffect(() => {
    const data = session.data;
    if (!data) return;

    let expectedUpdatedAt = data.updated_at;
    const handle = startAutosave<SessionState>({
      subscribe: (listener) => useSessionStore.subscribe(listener),
      getState: () => useSessionStore.getState(),
      isDirty: (state) => state.dirty && state.sessionId === data.id,
      save: async (state) => {
        const result = await api.patch<{ updated_at: string }>(
          `/sessions/${data.id}`,
          { ...snapshotOf(state), expected_updated_at: expectedUpdatedAt },
          { autosave: 'true' },
        );
        // Advance the guard so the next save is not refused by our own write.
        expectedUpdatedAt = result.updated_at;
      },
      onSaved: () => useSessionStore.getState().markSaved(),
      onConflict: () => useSessionStore.getState().markConflict(),
      onError: () => {
        // Left dirty on purpose: the next edit, or an explicit save, tries
        // again. A transient failure is not worth interrupting anyone over,
        // and the status bar already says "Unsaved changes…".
      },
    });

    const onBeforeUnload = () => void handle.flush();
    globalThis.addEventListener('beforeunload', onBeforeUnload);

    return () => {
      globalThis.removeEventListener('beforeunload', onBeforeUnload);
      handle.stop();
    };
  }, [api, session.data]);

  if (session.isLoading) return <MapSkeleton shortCode={shortCode} />;
  if (session.error) {
    return (
      <SessionError
        error={session.error}
        shortCode={shortCode}
        onRetry={() => void session.refetch()}
      />
    );
  }
  if (!session.data) return <MapSkeleton shortCode={shortCode} />;

  return (
    <>
      {session.data.hidden_layer_count > 0 ? (
        <HiddenLayerNotice count={session.data.hidden_layer_count} />
      ) : null}
      <App
        api={api}
        featureCounts={Object.fromEntries(
          session.data.layers.map((layer) => [layer.dataset_id, layer.dataset.feature_count]),
        )}
        // What an edit session is opened against. `09` §5.3 puts optimistic
        // concurrency on this pointer alone, so editing waits for it rather
        // than assuming a version.
        datasetVersions={Object.fromEntries(
          session.data.layers.map((layer) => [layer.dataset_id, layer.dataset.version]),
        )}
        projectName={project.data?.name ?? 'WebMap'}
        crsLabel={
          project.data
            ? `EPSG:${project.data.analysis_srid}`
            : 'Loading coordinate system…'
        }
        {...(project.data?.crs_wkt ? { crsWkt: project.data.crs_wkt } : {})}
        analysisUnit={project.data?.horizontal_unit ?? 'usft'}
        tileTokenFor={(datasetId) => tileTokens[datasetId]}
        tileUrlFor={(datasetId) => {
          const token = tileTokens[datasetId];
          // No token yet: point at the bearer-authenticated path, which the
          // browser cannot use for tiles. The layer simply does not draw until
          // the token arrives — a second at most, and better than a URL that
          // 401s on every tile and fills the console.
          return token
            ? `/api/v1/tiles/${datasetId}/{z}/{x}/{y}.mvt?token=${encodeURIComponent(token)}`
            : `/api/v1/tiles/${datasetId}/{z}/{x}/{y}.mvt`;
        }}
      />
    </>
  );
}

/**
 * Say how many layers were withheld, and why.
 *
 * The sessions service drops layers this principal cannot read and counts
 * them. Showing the count is the third of the three things that decision
 * requires: returning them would leak, refusing the session would be
 * disproportionate, and dropping them silently would leave the reader
 * wondering why the map looks wrong.
 */
function HiddenLayerNotice({ count }: { count: number }) {
  return (
    <div
      role="status"
      style={{
        position: 'fixed',
        top: 8,
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 20,
        padding: '6px 12px',
        fontSize: 12,
        background: 'rgba(255, 255, 255, 0.95)',
        border: '1px solid var(--chrome-border)',
        borderRadius: 2,
      }}
    >
      {count} {count === 1 ? 'layer is' : 'layers are'} not shown because you do not have
      access. Ask whoever shared this session for access to see {count === 1 ? 'it' : 'them'}.
    </div>
  );
}

function toStoreSession(data: SessionDto) {
  return {
    id: data.id,
    short_code: data.short_code,
    layers: data.layers.map((layer) => ({
      // The dataset id doubles as the layer id: a session holds one layer per
      // dataset (the service refuses duplicates), so a separate identifier
      // would be a second name for the same thing.
      id: layer.dataset_id,
      datasetId: layer.dataset_id,
      name: layer.dataset.name,
      symbology: layer.symbology_override ?? defaultSymbologyFor(layer.dataset.geometry_kind),
      opacity: layer.opacity,
      visible: layer.visible,
    })),
    view: {
      center: (data.view.center ?? [-102.08, 31.99]) as [number, number],
      zoom: data.view.zoom ?? 9,
    },
  };
}

/**
 * A symbology for a layer that has none stored.
 *
 * Deliberately plain, and deliberately *something*: a layer with no symbology
 * would compile to no MapLibre layers and be invisible, which reads as a
 * broken session rather than as an unstyled layer.
 */
function defaultSymbologyFor(geometryKind: string | null) {
  if (geometryKind === 'polygon' || geometryKind === 'multipolygon') {
    return {
      type: 'single' as const,
      symbol: {
        geometry: 'polygon' as const,
        fillColor: '#88a0c8',
        fillOpacity: 0.5,
        outlineColor: '#213547',
        outlineWidth: 1,
      },
    };
  }
  if (geometryKind === 'linestring' || geometryKind === 'multilinestring') {
    return {
      type: 'single' as const,
      symbol: {
        geometry: 'line' as const,
        color: '#c0392b',
        width: 1.5,
        opacity: 1,
        cap: 'round' as const,
        join: 'round' as const,
      },
    };
  }
  return {
    type: 'single' as const,
    symbol: {
      geometry: 'point' as const,
      marker: 'circle' as const,
      size: 4,
      color: '#2b93b3',
      strokeColor: '#ffffff',
      strokeWidth: 1,
      opacity: 1,
    },
  };
}
