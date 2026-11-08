// Browser-side API client. Every request goes through the Next.js proxy at <basePath>/api/backend/* (see
// app/api/backend/[...path]/route.ts); the proxy, not the browser, holds the token. Auth is httpOnly cookies set by
// the auth-service; access tokens are short-lived and refreshed silently (on a timer and once on any 401, then the
// request is retried). Tenancy: X-Client-Id on every registry-service call (a super admin may act in another client).
import type { ApiErrorBody, Session } from './types';

const BASE = `${process.env.NEXT_PUBLIC_BASE_PATH || ''}/api/backend/v1`;
const SESSION_KEY = 'admin.session';     // non-sensitive profile (user, client, projects) for fast reloads
const ASSUME_KEY = 'admin.assumeClient'; // super-admin "act in client X" selection (client id)
export const LOGOUT_EVENT = 'admin:logout';

const read = (key: string): string | null => {
  try {
    return typeof window === 'undefined' ? null : localStorage.getItem(key);
  } catch {
    return null;
  }
};
const write = (key: string, value: string | null) => {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the session still works for this page */
  }
};

export const auth = {
  session: (): Session | null => {
    try {
      return JSON.parse(read(SESSION_KEY) || 'null');
    } catch {
      return null;
    }
  },
  remember: (session: Session) => write(SESSION_KEY, JSON.stringify(session)),
  forget: () => {
    write(SESSION_KEY, null);
    write(ASSUME_KEY, null);
  },
  assumedClientId: (): number | null => Number(read(ASSUME_KEY)) || null,
  setAssumedClient: (id: number | null) => write(ASSUME_KEY, id === null ? null : String(id)),
  // The client id sent as X-Client-Id: the assumed client (super admin) or the user's own client.
  activeClientId: (): number | null => {
    const assumed = auth.assumedClientId();
    if (assumed) return assumed;
    return auth.session()?.client.id ?? null;
  },
};

export class ApiError extends Error {
  constructor(public status: number, public body: ApiErrorBody) {
    super(body.hint ? `${body.message} ${body.hint}` : body.message);
  }
}

const tenantHeaders = (): Record<string, string> => {
  const clientId = auth.activeClientId();
  return clientId ? { 'X-Client-Id': String(clientId) } : {};
};

// ---------------------------------------------------------------- silent refresh
let refreshing: Promise<boolean> | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

function signedOut() {
  if (refreshTimer) clearTimeout(refreshTimer);
  auth.forget();
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(LOGOUT_EVENT));
}

/** Schedule a silent refresh at 80% of the access-token lifetime. */
export function scheduleRefresh(expiresIn?: number | null) {
  if (refreshTimer) clearTimeout(refreshTimer);
  if (!expiresIn) return;
  refreshTimer = setTimeout(() => void refreshSession(), Math.max(10, expiresIn * 0.8) * 1000);
}

/** Rotate the auth cookies. Concurrent callers share one request. Returns false when the user must sign in. */
export function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = fetch(`${BASE}/auth/refresh`, { method: 'POST', headers: { 'X-Requested-With': 'admin-ui' } })
      .then(async (res) => {
        if (!res.ok) return false;
        const session = (await res.json()) as Session;
        auth.remember(session);
        scheduleRefresh(session.expiresIn);
        return true;
      })
      .catch(() => false)
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

async function errorFrom(res: Response): Promise<ApiError> {
  let body: ApiErrorBody = { code: 'SYS-INT-0001', message: res.statusText || 'Request failed', hint: '', retryable: false };
  try {
    body = (await res.json()).error ?? body;
  } catch {
    /* not JSON */
  }
  return new ApiError(res.status, body);
}

async function request(path: string, init: RequestInit = {}, retried = false): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set('X-Requested-With', 'admin-ui');
  Object.entries(tenantHeaders()).forEach(([k, v]) => headers.set(k, v));
  const res = await fetch(`${BASE}/${path}`, { ...init, headers, credentials: 'same-origin' });
  if (res.status === 401 && !path.startsWith('auth/')) {
    // The retryable 401 pattern: refresh once, then retry the original request.
    if (!retried && (await refreshSession())) return request(path, init, true);
    signedOut();
  }
  if (!res.ok) throw await errorFrom(res);
  return res;
}

const jsonRequest = async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
  const res = await request(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init.headers || {}) } });
  return res.status === 204 ? (undefined as T) : res.json();
};

/** POST an registry-service tool and return its JSON result. `body` is sent camelCase, as the tool's schema expects. */
export const factory = <T = any,>(tool: string, body: Record<string, unknown> = {}): Promise<T> =>
  jsonRequest<T>(`factory/${tool}`, { method: 'POST', body: JSON.stringify(body) });

/** Proxy-invoke a tool on another registered MCP server through registry-service (within policy). */
export const mcp = <T = any,>(server: string, tool: string, body: Record<string, unknown> = {}): Promise<T> =>
  jsonRequest<T>(`mcp/${server}/${tool}`, { method: 'POST', body: JSON.stringify(body) });

export const api = {
  async login(username: string, password: string): Promise<Session> {
    const session = await jsonRequest<Session>('auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    auth.remember(session);
    scheduleRefresh(session.expiresIn);
    return session;
  },
  async logout(): Promise<void> {
    try {
      await jsonRequest<void>('auth/logout', { method: 'POST' });
    } finally {
      signedOut();
    }
  },
  me: () => jsonRequest<Session>('auth/me'),
  /** Accept an invite (public: the invitee has no session yet). Signs them in on success. */
  acceptInvite: (token: string, password: string) =>
    jsonRequest<Session>('auth/invite/accept', { method: 'POST', body: JSON.stringify({ token, password }) }),
  factory,
  mcp,
};
