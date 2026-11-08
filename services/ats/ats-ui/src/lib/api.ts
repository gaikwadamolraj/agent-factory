// Browser-side API client for the orchestrator's /api/v1 (through the Next.js proxy at <basePath>/api/backend/*).
// Auth: httpOnly cookies set by the server (JavaScript never sees a token). Access tokens are short-lived and
// refreshed silently: on a timer before expiry and once on any 401, then the request is retried.
// Tenancy: X-Client-Id / X-Project-Id on every call. CSRF: every call sends X-Requested-With.
import type { AdminUser, ApiErrorBody, AssignableRole, ChatDetail, ChatSummary, DocumentKind, DocumentText, Run, Session, TurnEvent } from './types';

const BASE = `${process.env.NEXT_PUBLIC_BASE_PATH || ''}/api/backend/v1`;
const SESSION_KEY = 'ats.session';   // non-sensitive profile (user, client, projects) for fast reloads
const PROJECT_KEY = 'ats.projectId';
export const LOGOUT_EVENT = 'ats:logout';
export const MAX_FILE_MB = 10;

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
  projectId: (): number | null => Number(read(PROJECT_KEY)) || null,
  setProject: (id: number) => write(PROJECT_KEY, String(id)),
  remember: (session: Session) => write(SESSION_KEY, JSON.stringify(session)),
  forget: () => write(SESSION_KEY, null),
};

export class ApiError extends Error {
  constructor(public status: number, public body: ApiErrorBody) {
    super(body.hint ? `${body.message} ${body.hint}` : body.message);
  }
}

const tenantHeaders = (): Record<string, string> => {
  const session = auth.session();
  const projectId = auth.projectId();
  return session && projectId ? { 'X-Client-Id': String(session.client.id), 'X-Project-Id': String(projectId) } : {};
};

// ---------------------------------------------------------------- silent refresh
let refreshing: Promise<boolean> | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

function signedOut() {
  if (refreshTimer) clearTimeout(refreshTimer);
  auth.forget();
  window.dispatchEvent(new Event(LOGOUT_EVENT));
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
    refreshing = fetch(`${BASE}/auth/refresh`, { method: 'POST', headers: { 'X-Requested-With': 'ats-ui' } })
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
  headers.set('X-Requested-With', 'ats-ui');
  Object.entries(tenantHeaders()).forEach(([k, v]) => headers.set(k, v));
  const res = await fetch(`${BASE}/${path}`, { ...init, headers, credentials: 'same-origin' });
  if (res.status === 401 && !path.startsWith('auth/')) {
    if (!retried && (await refreshSession())) return request(path, init, true);
    signedOut();
  }
  if (!res.ok) throw await errorFrom(res);
  return res;
}

const json = async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
  const res = await request(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init.headers || {}) } });
  return res.status === 204 ? (undefined as T) : res.json();
};

export const api = {
  async login(username: string, password: string): Promise<Session> {
    const session = await json<Session>('auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    auth.remember(session);
    scheduleRefresh(session.expiresIn);
    return session;
  },
  async logout(): Promise<void> {
    try {
      await json<void>('auth/logout', { method: 'POST' });
    } finally {
      signedOut();
    }
  },
  me: () => json<Session>('auth/me'),
  listUsers: () => json<AdminUser[]>('admin/users'),
  createUser: (body: { username: string; email?: string; password: string; role: AssignableRole }) =>
    json<AdminUser>('admin/users', { method: 'POST', body: JSON.stringify(body) }),
  updateUser: (id: number, body: { role?: AssignableRole; isActive?: boolean; password?: string }) =>
    json<AdminUser>(`admin/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  listChats: () => json<ChatSummary[]>('chats'),
  createChat: () => json<ChatSummary>('chats', { method: 'POST', body: '{}' }),
  getChat: (id: string) => json<ChatDetail>(`chats/${id}`),
  updateChat: (id: string, body: { title?: string; jobDescription?: string }) =>
    json<ChatSummary>(`chats/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteChat: (id: string) => json<void>(`chats/${id}`, { method: 'DELETE' }),
  abortTurn: (chatId: string, turnId: string) =>
    json<{ turnId: string; aborted: boolean }>(`chats/${chatId}/turns/${turnId}/abort`, { method: 'POST' }),
  documentText: (chatId: string, kind: DocumentKind) => json<DocumentText>(`chats/${chatId}/documents/${kind}/text`),

  /** One durable run from the orchestrator's projection. Reads never reach the workflow engine. */
  getRun: (id: string) => json<Run>(`runs/${encodeURIComponent(id)}`),
  listRuns: (query: { journey?: string; status?: string; limit?: number } = {}) => {
    const params = new URLSearchParams(
      Object.entries(query).filter(([, value]) => value !== undefined).map(([k, v]) => [k, String(v)]),
    );
    const query_string = params.toString();
    return json<Run[]>(`runs${query_string ? `?${query_string}` : ''}`);
  },
  cancelRun: (id: string) =>
    json<{ id: string; cancelled: boolean }>(`runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  /** Original file as a blob URL (revoke it when done). */
  async documentFileUrl(chatId: string, kind: DocumentKind): Promise<string> {
    const res = await request(`chats/${chatId}/documents/${kind}/file`);
    return URL.createObjectURL(await res.blob());
  },

  /** Sends a message and calls onEvent for every Server-Sent Event until the turn ends. */
  async sendMessage(chatId: string, form: FormData, onEvent: (e: TurnEvent) => void, signal?: AbortSignal): Promise<void> {
    const res = await request(`chats/${chatId}/messages`, { method: 'POST', body: form, signal });
    if (!res.body) throw new Error('The server did not return a stream.');
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finished = false;
    const handle = (raw: string) => {
      const data = raw
        .split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).trimStart())
        .join('\n');
      if (!data) return;
      try {
        const event = JSON.parse(data) as TurnEvent;
        if (['message', 'aborted', 'error'].includes(event.type)) finished = true;
        onEvent(event);
      } catch {
        /* ignore malformed event */
      }
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      const events = buffer.split('\n\n');
      buffer = events.pop() ?? '';
      events.forEach(handle);
    }
    if (buffer.trim()) handle(buffer);
    if (!finished && !signal?.aborted) {
      throw new Error('The connection closed before the answer arrived (proxy or ingress timeout?).');
    }
  },
};
