// Server-side proxy: browser -> Next.js -> cluster-internal services. The browser never holds a token; tokens live
// only in httpOnly cookies set by the auth-service. This proxy is the security boundary:
//
//   v1/auth/*  -> auth-service  (login / refresh / logout / me / invite accept). Cookies are forwarded both ways,
//                 so the auth-service sets/rotates the httpOnly session cookies on the browser.
//   everything else (v1/factory/*, v1/mcp/*, v1/notifications) -> registry-service, which authenticates from an
//                 `Authorization: Bearer <access token>` header. We read the access-token cookie server-side and
//                 turn it into that header, so the token still never reaches the browser's JavaScript.
//
// The UI code sees one API under <basePath>/api/backend and does not know about the split. registry-service is NOT a
// security boundary reachable from the browser: it is only ever called from here.
import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const FACTORY = (process.env.INTERNAL_REGISTRY_URL || 'http://localhost:8005').replace(/\/+$/, '');
const AUTH = (process.env.INTERNAL_AUTH_URL || 'http://localhost:8004').replace(/\/+$/, '');
const AUTH_PREFIXES = ['v1/auth/'];
const ACCESS_COOKIE = 'ats_access';   // agent_common.constants.COOKIE_ACCESS_TOKEN
const MAX_REQUEST_BYTES = Number(process.env.MAX_REQUEST_MB || 5) * 1024 * 1024;

// Headers forwarded to the backend (never X-Auth-*: registry-service trusts only the verified token).
const FORWARD_REQUEST = ['content-type', 'accept', 'x-client-id', 'x-request-id', 'x-requested-with'];
const FORWARD_RESPONSE = ['content-type', 'content-disposition', 'cache-control', 'www-authenticate', 'retry-after',
                          'x-request-id'];

const isAuthPath = (path: string) => AUTH_PREFIXES.some((p) => path.startsWith(p));

const errorResponse = (status: number, code: string, message: string, hint: string, retryable = false) =>
  Response.json({ error: { code, message, hint, retryable } }, { status });

async function proxy(req: NextRequest, { params }: { params: { path: string[] } }) {
  const declared = Number(req.headers.get('content-length') || 0);
  if (declared > MAX_REQUEST_BYTES) {
    return errorResponse(413, 'VAL-LIM-0001', 'The request is too large.', 'Send a smaller payload.');
  }
  const path = params.path.map(encodeURIComponent).join('/');
  const toAuth = isAuthPath(path);
  const url = `${(toAuth ? AUTH : FACTORY)}/api/${path}${req.nextUrl.search}`;

  const headers = new Headers();
  for (const name of FORWARD_REQUEST) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (toAuth) {
    // The auth-service authenticates (refresh/logout/me) from the httpOnly cookies, so pass them straight through.
    const cookie = req.headers.get('cookie');
    if (cookie) headers.set('cookie', cookie);
  } else {
    // registry-service authenticates from the bearer token: lift it out of the access-token cookie.
    const token = req.cookies.get(ACCESS_COOKIE)?.value;
    if (token) headers.set('authorization', `Bearer ${token}`);
  }

  const hasBody = !['GET', 'HEAD'].includes(req.method);
  try {
    const body = hasBody ? await req.arrayBuffer() : undefined;
    if (body && body.byteLength > MAX_REQUEST_BYTES) {
      return errorResponse(413, 'VAL-LIM-0001', 'The request is too large.', 'Send a smaller payload.');
    }
    const upstream = await fetch(url, {
      method: req.method,
      headers,
      body,
      cache: 'no-store',
      redirect: 'manual',
      signal: req.signal,   // browser aborts (closed tab) reach the backend
    });
    const out = new Headers();
    for (const name of FORWARD_RESPONSE) {
      const value = upstream.headers.get(name);
      if (value) out.set(name, value);
    }
    // Only the auth-service sets cookies (the session); pass them back to the browser.
    for (const cookie of upstream.headers.getSetCookie()) out.append('set-cookie', cookie);
    return new Response(upstream.body, { status: upstream.status, headers: out });
  } catch {
    return errorResponse(502, 'SYS-UNA-0001', 'A dependent service is temporarily unavailable.',
                         'Retry in a few seconds.', true);
  }
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE, proxy as PUT };
