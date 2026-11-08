'use client';
// Users & roles (delegated to the auth-service through registry-service). registry-service exposes invite/create/set-role/
// deactivate and a pending-invites list, but no "list active users" tool, so role changes and deactivation take a
// user id (and the invite flow returns a copyable link - no mail server needed yet). The server forces every write to
// the caller's own client and refuses escalation beyond the caller's role.
import { useCallback, useEffect, useState } from 'react';
import { factory } from '../lib/api';
import { Button, Card, CopyLink, Empty, ErrorBanner, Field, Input, Select } from './ui';

const ROLES = ['recruiter', 'viewer', 'admin', 'client_admin'];

export default function UsersTab() {
  const [error, setError] = useState<unknown>(null);
  const [invites, setInvites] = useState<any[]>([]);
  const [inviteLink, setInviteLink] = useState<string | null>(null);
  const [inviteForm, setInviteForm] = useState({ email: '', role: 'recruiter' });
  const [createForm, setCreateForm] = useState({ username: '', email: '', role: 'recruiter' });
  const [roleForm, setRoleForm] = useState({ userId: '', role: 'recruiter' });
  const [deactivateId, setDeactivateId] = useState('');

  const loadInvites = useCallback(async () => {
    try {
      const res = await factory<{ items?: any[] }>('list_invites');
      setInvites(res.items || []);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    void loadInvites();
  }, [loadInvites]);

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setInviteLink(null);
    try {
      const res = await factory<{ inviteLink?: string }>('invite_user', {
        email: inviteForm.email,
        role: inviteForm.role,
      });
      if (res.inviteLink) setInviteLink(res.inviteLink);
      setInviteForm({ email: '', role: 'recruiter' });
      await loadInvites();
    } catch (err) {
      setError(err);
    }
  }

  async function revoke(id: string) {
    setError(null);
    try {
      await factory('revoke_invite', { inviteId: id });
      await loadInvites();
    } catch (e) {
      setError(e);
    }
  }

  async function createUser(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await factory('create_user', {
        username: createForm.username,
        email: createForm.email || undefined,
        role: createForm.role,
      });
      setCreateForm({ username: '', email: '', role: 'recruiter' });
    } catch (err) {
      setError(err);
    }
  }

  async function setRole(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await factory('set_user_role', { userId: Number(roleForm.userId), role: roleForm.role });
    } catch (err) {
      setError(err);
    }
  }

  async function deactivate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await factory('deactivate_user', { userId: Number(deactivateId) });
      setDeactivateId('');
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="space-y-4">
      <ErrorBanner error={error} onClose={() => setError(null)} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card>
          <h2 className="mb-2 text-sm font-semibold text-slate-200">Invite a user</h2>
          <form onSubmit={invite} className="space-y-2">
            <Field label="Email">
              <Input
                type="email"
                value={inviteForm.email}
                onChange={(e) => setInviteForm({ ...inviteForm, email: e.target.value })}
              />
            </Field>
            <Field label="Role">
              <Select value={inviteForm.role} onChange={(e) => setInviteForm({ ...inviteForm, role: e.target.value })}>
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </Select>
            </Field>
            <Button type="submit" variant="primary" disabled={!inviteForm.email}>
              Create invite
            </Button>
          </form>
          {inviteLink ? (
            <div className="mt-3">
              <p className="mb-1 text-xs text-slate-400">Share this link (single use, expiring):</p>
              <CopyLink value={inviteLink} />
            </div>
          ) : null}

          <h3 className="mb-2 mt-4 text-xs font-semibold uppercase text-slate-500">Pending invites</h3>
          {invites.length === 0 ? (
            <Empty>No pending invites.</Empty>
          ) : (
            <ul className="space-y-1">
              {invites.map((inv) => (
                <li
                  key={inv.id}
                  className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/50 px-2 py-1.5 text-sm"
                >
                  <span>
                    {inv.email} <span className="text-xs text-slate-500">({inv.role})</span>
                  </span>
                  <Button variant="ghost" onClick={() => void revoke(inv.id)}>
                    Revoke
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="space-y-4">
          <Card>
            <h2 className="mb-2 text-sm font-semibold text-slate-200">Create user</h2>
            <form onSubmit={createUser} className="space-y-2">
              <Field label="Username">
                <Input
                  value={createForm.username}
                  onChange={(e) => setCreateForm({ ...createForm, username: e.target.value })}
                />
              </Field>
              <Field label="Email (optional)">
                <Input
                  value={createForm.email}
                  onChange={(e) => setCreateForm({ ...createForm, email: e.target.value })}
                />
              </Field>
              <Field label="Role">
                <Select value={createForm.role} onChange={(e) => setCreateForm({ ...createForm, role: e.target.value })}>
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button type="submit" variant="primary" disabled={!createForm.username}>
                Create user
              </Button>
            </form>
          </Card>

          <Card>
            <h2 className="mb-2 text-sm font-semibold text-slate-200">Change role / deactivate</h2>
            <form onSubmit={setRole} className="mb-3 flex items-end gap-2">
              <Field label="User id">
                <Input value={roleForm.userId} onChange={(e) => setRoleForm({ ...roleForm, userId: e.target.value })} />
              </Field>
              <Field label="Role">
                <Select value={roleForm.role} onChange={(e) => setRoleForm({ ...roleForm, role: e.target.value })}>
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </Select>
              </Field>
              <Button type="submit" disabled={!roleForm.userId}>
                Set role
              </Button>
            </form>
            <form onSubmit={deactivate} className="flex items-end gap-2">
              <Field label="Deactivate user id">
                <Input value={deactivateId} onChange={(e) => setDeactivateId(e.target.value)} />
              </Field>
              <Button type="submit" variant="danger" disabled={!deactivateId}>
                Deactivate
              </Button>
            </form>
          </Card>
        </div>
      </div>
    </div>
  );
}
