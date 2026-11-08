'use client';
// Policies & groups (the IAM human layer) + a simulate panel. Policies are data: an access_policies document is edited
// as JSON (validated client-side before the call; the server validates it again). Groups carry members. "Simulate"
// asks the engine whether a user may do an action on a resource, without changing anything.
import { useCallback, useEffect, useState } from 'react';
import { factory } from '../lib/api';
import type { Group, ListResult, Policy, SimulateResult } from '../lib/types';
import { Button, Card, Empty, ErrorBanner, Field, Input, Select, TextArea } from './ui';

const SAMPLE_DOCUMENT = JSON.stringify(
  { statements: [{ effect: 'allow', actions: ['skill.read'], resources: ['skill:ats/*'] }] },
  null,
  2,
);

export default function PoliciesTab() {
  const [error, setError] = useState<unknown>(null);
  return (
    <div className="space-y-4">
      <ErrorBanner error={error} onClose={() => setError(null)} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <PoliciesPanel setError={setError} />
        <div className="space-y-4">
          <SimulatePanel setError={setError} />
          <GroupsPanel setError={setError} />
        </div>
      </div>
    </div>
  );
}

function PoliciesPanel({ setError }: { setError: (e: unknown) => void }) {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [form, setForm] = useState({ principalType: 'user', principalId: '', name: '', document: SAMPLE_DOCUMENT, enabled: true });

  const load = useCallback(async () => {
    try {
      const res = await factory<ListResult<Policy>>('list_policies');
      setPolicies(res.items || []);
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
    let document: unknown;
    try {
      document = JSON.parse(form.document);
    } catch {
      setError(new Error('Policy document is not valid JSON.'));
      return;
    }
    try {
      await factory('create_policy', {
        principalType: form.principalType,
        principalId: Number(form.principalId),
        name: form.name || undefined,
        document,
        enabled: form.enabled,
      });
      setForm({ ...form, principalId: '', name: '' });
      await load();
    } catch (err) {
      setError(err);
    }
  }

  async function toggle(p: Policy) {
    setError(null);
    try {
      await factory('update_policy', { policyId: p.id, enabled: !p.enabled });
      await load();
    } catch (e) {
      setError(e);
    }
  }

  async function remove(id: string) {
    if (!window.confirm('Delete this policy?')) return;
    setError(null);
    try {
      await factory('delete_policy', { policyId: id });
      await load();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card>
      <h2 className="mb-2 text-sm font-semibold text-slate-200">Access policies</h2>
      {policies.length === 0 ? (
        <Empty>No policies.</Empty>
      ) : (
        <ul className="mb-4 space-y-1">
          {policies.map((p) => (
            <li
              key={p.id}
              className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/50 px-2 py-1.5 text-sm"
            >
              <span>
                <span className="font-mono text-xs text-slate-400">
                  {p.principalType}:{p.principalId}
                </span>{' '}
                {p.name || '(unnamed)'} {p.enabled ? '' : <span className="text-slate-600">(disabled)</span>}
              </span>
              <span className="flex gap-1">
                <Button variant="ghost" onClick={() => void toggle(p)}>
                  {p.enabled ? 'Disable' : 'Enable'}
                </Button>
                <Button variant="danger" onClick={() => void remove(p.id)}>
                  Delete
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={create} className="space-y-2 border-t border-slate-800 pt-3">
        <p className="text-xs font-semibold uppercase text-slate-500">New policy</p>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Principal type">
            <Select value={form.principalType} onChange={(e) => setForm({ ...form, principalType: e.target.value })}>
              <option value="user">user</option>
              <option value="group">group</option>
              <option value="project">project</option>
              <option value="client">client</option>
            </Select>
          </Field>
          <Field label="Principal id">
            <Input value={form.principalId} onChange={(e) => setForm({ ...form, principalId: e.target.value })} />
          </Field>
        </div>
        <Field label="Name (optional)">
          <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Field label="Document (JSON)">
          <TextArea rows={8} value={form.document} onChange={(e) => setForm({ ...form, document: e.target.value })} />
        </Field>
        <Button type="submit" variant="primary" disabled={!form.principalId}>
          Create policy
        </Button>
      </form>
    </Card>
  );
}

function SimulatePanel({ setError }: { setError: (e: unknown) => void }) {
  const [form, setForm] = useState({ userId: '', action: '', resource: '' });
  const [result, setResult] = useState<SimulateResult | null>(null);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setResult(null);
    try {
      setResult(
        await factory<SimulateResult>('simulate_policy', {
          userId: Number(form.userId),
          action: form.action,
          resource: form.resource,
        }),
      );
    } catch (err) {
      setError(err);
    }
  }

  return (
    <Card>
      <h2 className="mb-2 text-sm font-semibold text-slate-200">Simulate</h2>
      <p className="mb-2 text-xs text-slate-500">Can user X do action Y on resource Z?</p>
      <form onSubmit={run} className="space-y-2">
        <Field label="User id">
          <Input value={form.userId} onChange={(e) => setForm({ ...form, userId: e.target.value })} />
        </Field>
        <Field label="Action">
          <Input
            value={form.action}
            onChange={(e) => setForm({ ...form, action: e.target.value })}
            placeholder="skill.approve"
          />
        </Field>
        <Field label="Resource">
          <Input
            value={form.resource}
            onChange={(e) => setForm({ ...form, resource: e.target.value })}
            placeholder="skill:ats/screening"
          />
        </Field>
        <Button type="submit" variant="primary" disabled={!form.userId || !form.action || !form.resource}>
          Evaluate
        </Button>
      </form>
      {result ? (
        <div
          className={`mt-3 rounded border px-3 py-2 text-sm ${
            result.allowed ? 'border-emerald-800 bg-emerald-950/40 text-emerald-200' : 'border-red-800 bg-red-950/40 text-red-200'
          }`}
        >
          <p className="font-semibold">{result.allowed ? 'ALLOWED' : 'DENIED'}</p>
          <p className="text-xs">
            effect: {result.effect} — {result.reason}
          </p>
          {result.matchedConditions?.length ? (
            <p className="text-xs">conditions: {result.matchedConditions.join(', ')}</p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

function GroupsPanel({ setError }: { setError: (e: unknown) => void }) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [name, setName] = useState('');
  const [memberInput, setMemberInput] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const res = await factory<ListResult<Group>>('list_groups');
      setGroups(res.items || []);
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
      await factory('create_group', { name });
      setName('');
      await load();
    } catch (err) {
      setError(err);
    }
  }

  async function remove(id: string) {
    if (!window.confirm('Delete this group?')) return;
    try {
      await factory('delete_group', { groupId: id });
      await load();
    } catch (e) {
      setError(e);
    }
  }

  async function addMember(id: string) {
    const userId = Number(memberInput[id]);
    if (!Number.isInteger(userId) || userId <= 0) return;
    try {
      await factory('add_group_member', { groupId: id, userId });
      setMemberInput({ ...memberInput, [id]: '' });
      await load();
    } catch (e) {
      setError(e);
    }
  }

  async function removeMember(id: string, userId: number) {
    try {
      await factory('remove_group_member', { groupId: id, userId });
      await load();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card>
      <h2 className="mb-2 text-sm font-semibold text-slate-200">Groups</h2>
      {groups.length === 0 ? (
        <Empty>No groups.</Empty>
      ) : (
        <ul className="mb-3 space-y-2">
          {groups.map((g) => (
            <li key={g.id} className="rounded border border-slate-800 bg-slate-950/50 px-2 py-1.5 text-sm">
              <div className="flex items-center justify-between">
                <span className="font-medium">{g.name}</span>
                <Button variant="danger" onClick={() => void remove(g.id)}>
                  Delete
                </Button>
              </div>
              {g.members?.length ? (
                <div className="mt-1 flex flex-wrap gap-1">
                  {g.members.map((m) => (
                    <button
                      key={m}
                      onClick={() => void removeMember(g.id, m)}
                      title="Remove member"
                      className="rounded bg-slate-800 px-2 py-0.5 text-xs hover:bg-red-900"
                    >
                      {m} x
                    </button>
                  ))}
                </div>
              ) : null}
              <div className="mt-1 flex gap-1">
                <Input
                  value={memberInput[g.id] || ''}
                  onChange={(e) => setMemberInput({ ...memberInput, [g.id]: e.target.value })}
                  placeholder="user id"
                  className="w-24"
                />
                <Button onClick={() => void addMember(g.id)}>Add member</Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={create} className="flex gap-2 border-t border-slate-800 pt-3">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="New group name" />
        <Button type="submit" variant="primary" disabled={!name}>
          Create
        </Button>
      </form>
    </Card>
  );
}
