// Server-side proxy: browser -> Next.js -> orchestrator (cluster-internal URL). Streams responses (SSE) through,
// forwards auth cookies both ways, and rejects oversized uploads before reading them.
import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const BACKEND = (process.env.INTERNAL_BACKEND_URL || 'http://localhost:5000').replace(/\/+$/, '');
const MAX_REQUEST_BYTES = Number(process.env.MAX_REQUEST_MB || 25) * 1024 * 1024;
const FORWARD_REQUEST = ['authorization', 'content-type', 'accept', 'cookie', 'x-client-id', 'x-project-id',
                         'x-request-id', 'x-requested-with'];
const FORWARD_RESPONSE = ['content-type', 'content-disposition', 'cache-control', 'www-authenticate', 'retry-after',
                          'x-request-id'];

const errorResponse = (status: number, code: string, message: string, hint: string, retryable = false) =>
  Response.json({ error: { code, message, hint, retryable } }, { status });

async function proxy(req: NextRequest, { params }: { params: { path: string[] } }) {
  const declared = Number(req.headers.get('content-length') || 0);
  if (declared > MAX_REQUEST_BYTES) {
    return errorResponse(413, 'VAL-LIM-0001', 'The request is too large.', 'Send smaller files (max 10 MB each).');
  }
  const url = `${BACKEND}/api/${params.path.map(encodeURIComponent).join('/')}${req.nextUrl.search}`;
  const headers = new Headers();
  for (const name of FORWARD_REQUEST) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = !['GET', 'HEAD'].includes(req.method);
  try {
    const body = hasBody ? await req.arrayBuffer() : undefined;
    if (body && body.byteLength > MAX_REQUEST_BYTES) {
      return errorResponse(413, 'VAL-LIM-0001', 'The request is too large.', 'Send smaller files (max 10 MB each).');
    }
    const upstream = await fetch(url, {
      method: req.method,
      headers,
      body,
      cache: 'no-store',
      redirect: 'manual',
      signal: req.signal, // browser aborts (Stop button, closed tab) reach the orchestrator, which cancels the turn
    });
    const out = new Headers();
    for (const name of FORWARD_RESPONSE) {
      const value = upstream.headers.get(name);
      if (value) out.set(name, value);
    }
    for (const cookie of upstream.headers.getSetCookie()) out.append('set-cookie', cookie);
    if (out.get('content-type')?.includes('text/event-stream')) {
      out.set('Cache-Control', 'no-cache, no-transform');
      out.set('X-Accel-Buffering', 'no');
    }
    return new Response(upstream.body, { status: upstream.status, headers: out });
  } catch {
    return errorResponse(502, 'SYS-UNA-0001', 'A dependent service is temporarily unavailable.', 'Retry in a few seconds.', true);
  }
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE, proxy as PUT };
