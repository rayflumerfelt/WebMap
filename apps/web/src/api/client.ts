/**
 * The API client. `07-frontend.md` §7.
 *
 * Thin on purpose. The interesting decision here is how failures are
 * represented: as an `ApiError` carrying the status **and the server's own
 * message**, never as a generic "request failed".
 *
 * That matters because the API spends real effort on those messages
 * (`CLAUDE.md` §8 — what happened, why, and what now), and the permission ones
 * name the owner so the conversation can continue (`03-auth-security.md`
 * §3.2). A client that replaced "You have viewer access to 'Wolfcamp A'; ask
 * Ada Chen to grant edit access" with "403 Forbidden" would throw away the
 * only part a geologist can act on.
 */

export interface ApiErrorBody {
  error?: string;
  detail?: string;
}

export class ApiError extends Error {
  readonly status: number;
  /** The server's machine-readable code, e.g. `permission_denied`. */
  readonly code: string | undefined;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }

  /** True when the caller may see the object but not act on it. */
  get isPermission(): boolean {
    return this.status === 403;
  }

  /** True when the object does not exist *or* is invisible — the API does not
   *  distinguish them, because confirming existence is itself a disclosure. */
  get isNotFound(): boolean {
    return this.status === 404;
  }

  get isConflict(): boolean {
    return this.status === 409;
  }
}

/** Query parameters. `undefined` values are dropped rather than sent empty. */
export type QueryParams = Record<string, string | number | undefined>;

export interface ApiOptions {
  baseUrl?: string;
  /** Bearer token. Supplied by the auth layer; this module does not acquire
   *  one, so there is exactly one place that knows how tokens are obtained. */
  token?: string | null;
  fetchImpl?: typeof fetch;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly token: string | null;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiOptions = {}) {
    this.baseUrl = (options.baseUrl ?? '/api/v1').replace(/\/$/, '');
    this.token = options.token ?? null;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  get<T>(path: string, params?: QueryParams): Promise<T> {
    return this.request<T>('GET', path, { ...(params ? { params } : {}) });
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('POST', path, { ...(body !== undefined ? { body } : {}) });
  }

  patch<T>(path: string, body?: unknown, params?: QueryParams): Promise<T> {
    return this.request<T>('PATCH', path, {
      ...(body !== undefined ? { body } : {}),
      ...(params ? { params } : {}),
    });
  }

  delete(path: string): Promise<void> {
    return this.request<void>('DELETE', path, {});
  }

  private async request<T>(
    method: string,
    path: string,
    { body, params }: { body?: unknown; params?: QueryParams },
  ): Promise<T> {
    const url = new URL(`${this.baseUrl}${path}`, globalThis.location?.origin ?? 'http://localhost');
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }

    const headers: Record<string, string> = {
      // `02-data-model.md` §3.12: actor_channel makes "what did Claude do on
      // my behalf" answerable. The browser is always 'web'.
      'X-WebMap-Channel': 'web',
    };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;

    let response: Response;
    try {
      response = await this.fetchImpl(url.toString(), {
        method,
        headers,
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
    } catch {
      // A network failure, not an API failure. Status 0 so callers that branch
      // on status — autosave's conflict check especially — treat it as
      // transient rather than as a refusal.
      //
      // The browser's own error text is deliberately not included: "Failed to
      // fetch" tells a user nothing the sentence below does not, and reads as
      // an internal error rather than as a connection problem.
      throw new ApiError(
        0,
        'Could not reach the WebMap API. Check your connection; any unsaved ' +
          'changes are still here and will be saved when it returns.',
        'network_error',
      );
    }

    if (response.status === 204) return undefined as T;

    if (!response.ok) {
      const parsed = await safeJson(response);
      throw new ApiError(
        response.status,
        // The server's message, verbatim when there is one. See the module
        // docstring for why this is not a generic string.
        parsed?.detail ?? `The API returned ${response.status} for ${method} ${path}.`,
        parsed?.error,
      );
    }

    return (await response.json()) as T;
  }
}

async function safeJson(response: Response): Promise<ApiErrorBody | null> {
  try {
    return (await response.json()) as ApiErrorBody;
  } catch {
    // An error response that is not JSON — a proxy's HTML error page, most
    // likely. The status still carries the meaning.
    return null;
  }
}
