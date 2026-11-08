'use client';
// Clients & projects. A super admin sees every client and can pick one to act in (the assume-client switcher sets the
// X-Client-Id sent on every later call, audited server-side). A client admin manages their own client's projects and
// membership. Client-level tools are super-admin only and the server enforces that; the UI just hides what the role
// cannot use.
import { useCallback, useEffect, useState } from 'react';
import { auth, factory } from '../lib/api';
import type { Client, ListResult, Project, Session } from '../lib/types';
import { Button, Card, Empty, ErrorBanner, Field, Input } from './ui';

export default function ClientsTab({ session, onAssume }: { session: Session; onAssume: () => void }) {
  const [error, setError] = useState<unknown>(null);
  const isSuperAdmin = session.user.role === 'super_admin';
  return (
    <div className="space-y-4">
      <ErrorBanner error={error} onClose={() => setError(null)} />
      {isSuperAdmin ? <ClientsPanel setError={setError} onAssume={onAssume} /> : null}
      <ProjectsPanel setError={setError} />
    </div>
  );
}

function ClientsPanel({ setError, onAssume }: { setError: (e: unknown) => void; onAssume: () => void }) {
  const [clients, setClients] = useState<Client[]>([]);
  const [form, setForm] = useState({ slug: '', name: '' });
  const assumed = auth.assumedClientId();

  const load = useCallback(async () => {
    try {
      const res = await factory<ListResult<Client>>('list_clients');
      setClients(res.items || []);
    } catch (e) {
      setError(e);
    }
  }, [setError]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await factory('create_client', form);
      setForm({ slug: '', name: '' });
      await load();
    } catch (err) {
      setError(err);
    }
  }

  async function assume(id: number | null) {
    setError(null);
    try {
      if (id !== null) await factory('assume_client', { clientId: id });
      auth.setAssumedClient(id);
      onAssume();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card>
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">Clients (super admin)</h2>
        {assumed ? (
          <Button variant="ghost" onClick={() => void assume(null)}>
            Stop acting in client {assumed}
          </Button>
        ) : null}
      </div>
      {clients.length === 0 ? (
        <Empty>No clients.</Empty>
      ) : (
        <ul className="mb-3 space-y-1">
          {clients.map((c) => (
            <li
              key={c.id}
              className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/50 px-2 py-1.5 text-sm"
            >
              <span>
                {c.name} <span className="text-xs text-slate-500">({c.slug})</span>
                {c.isActive ? '' : <span className="text-slate-600"> inactive</span>}
              </span>
              <Button
                variant={assumed === c.id ? 'primary' : 'ghost'}
                onClick={() => void assume(c.id)}
              >
                {assumed === c.id ? 'Acting here' : 'Act in'}
              </Button>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={create} className="flex items-end gap-2 border-t border-slate-800 pt-3">
        <Field label="Slug">
          <Input value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} placeholder="globex" />
        </Field>
        <Field label="Name">
          <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Button type="submit" variant="primary" disabled={!form.slug || !form.name}>
          Create client
        </Button>
      </form>
    </Card>
  );
}

function ProjectsPanel({ setError }: { setError: (e: unknown) => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [form, setForm] = useState({ slug: '', name: '', description: '' });
  const [member, setMember] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    try {
      const res = await factory<ListResult<Project>>('list_projects');
      setProjects(res.items || []);
    } catch (e) {
      setError(e);
    }
  }, [setError]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await factory('create_project', form);
      setForm({ slug: '', name: '', description: '' });
      await load();
    } catch (err) {
      setError(err);
    }
  }

  async function addMember(projectId: number) {
    const userId = Number(member[projectId]);
    if (!Number.isInteger(userId) || userId <= 0) return;
    try {
      await factory('add_project_member', { projectId, userId });
      setMember({ ...member, [projectId]: '' });
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card>
      <h2 className="mb-2 text-sm font-semibold text-slate-200">Projects</h2>
      {projects.length === 0 ? (
        <Empty>No projects in this client.</Empty>
      ) : (
        <ul className="mb-3 space-y-2">
          {projects.map((p) => (
            <li key={p.id} className="rounded border border-slate-800 bg-slate-950/50 px-2 py-1.5 text-sm">
              <div className="flex items-center justify-between">
                <span>
                  <span className="font-medium">{p.name}</span>{' '}
                  <span className="text-xs text-slate-500">id {p.id}</span>
                </span>
              </div>
              <div className="mt-1 flex gap-1">
                <Input
                  value={member[p.id] || ''}
                  onChange={(e) => setMember({ ...member, [p.id]: e.target.value })}
                  placeholder="user id"
                  className="w-24"
                />
                <Button onClick={() => void addMember(p.id)}>Add member</Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={create} className="space-y-2 border-t border-slate-800 pt-3">
        <p className="text-xs font-semibold uppercase text-slate-500">New project</p>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Slug">
            <Input value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} />
          </Field>
          <Field label="Name">
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
        </div>
        <Field label="Description">
          <Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </Field>
        <Button type="submit" variant="primary" disabled={!form.slug || !form.name}>
          Create project
        </Button>
      </form>
    </Card>
  );
}
