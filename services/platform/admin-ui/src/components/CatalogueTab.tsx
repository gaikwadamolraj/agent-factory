'use client';
// Tool catalogue: the MCP servers registry-service can proxy to (list_catalogue) and an invoke form. A call goes through
// POST /api/v1/mcp/{server}/{tool} on the same auth+policy pipeline (action tool:{server}.{tool}), so a call the
// caller is not allowed to make comes back as a denial.
import { useCallback, useEffect, useState } from 'react';
import { factory, mcp } from '../lib/api';
import type { CatalogueServer } from '../lib/types';
import { Button, Card, Empty, ErrorBanner, Field, Input, Select, TextArea } from './ui';

export default function CatalogueTab() {
  const [servers, setServers] = useState<CatalogueServer[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [form, setForm] = useState({ server: '', tool: '', args: '{}' });
  const [result, setResult] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await factory<{ servers: CatalogueServer[]; version: number }>('list_catalogue');
      setServers(res.servers || []);
      if (res.servers?.length && !form.server) setForm((f) => ({ ...f, server: res.servers[0].name }));
    } catch (e) {
      setError(e);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function invoke(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setResult(null);
    let args: Record<string, unknown>;
    try {
      args = JSON.parse(form.args || '{}');
    } catch {
      setError(new Error('Arguments must be valid JSON.'));
      return;
    }
    try {
      const res = await mcp<{ server: string; tool: string; result: string }>(form.server, form.tool, args);
      setResult(typeof res.result === 'string' ? res.result : JSON.stringify(res, null, 2));
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[280px_1fr]">
      <Card>
        <h2 className="mb-2 text-sm font-semibold text-slate-200">Registered MCP servers</h2>
        {servers.length === 0 ? (
          <Empty>No servers registered.</Empty>
        ) : (
          <ul className="space-y-1 text-sm">
            {servers.map((s) => (
              <li key={s.name} className="rounded border border-slate-800 bg-slate-950/50 px-2 py-1.5">
                <span className="font-mono">{s.name}</span>
                <span className="block text-xs text-slate-500">aud: {s.audience}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <h2 className="mb-2 text-sm font-semibold text-slate-200">Invoke a tool (within policy)</h2>
        <ErrorBanner error={error} onClose={() => setError(null)} />
        <form onSubmit={invoke} className="space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <Field label="Server">
              <Select value={form.server} onChange={(e) => setForm({ ...form, server: e.target.value })}>
                {servers.map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Tool">
              <Input value={form.tool} onChange={(e) => setForm({ ...form, tool: e.target.value })} placeholder="check_text" />
            </Field>
          </div>
          <Field label="Arguments (JSON)">
            <TextArea rows={6} value={form.args} onChange={(e) => setForm({ ...form, args: e.target.value })} />
          </Field>
          <Button type="submit" variant="primary" disabled={!form.server || !form.tool}>
            Invoke
          </Button>
        </form>
        {result !== null ? (
          <pre className="mt-3 overflow-x-auto rounded bg-slate-950 p-3 text-xs text-slate-200">{result}</pre>
        ) : null}
      </Card>
    </div>
  );
}
