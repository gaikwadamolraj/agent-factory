'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, KeyRound, UserPlus } from 'lucide-react';
import { api } from '../lib/api';
import type { AdminUser, AssignableRole } from '../lib/types';
import Tip from './Tip';

interface Props {
  selfId: number;
  clientName: string;
  onClose: () => void;
}

const input = 'rounded-lg border border-slate-800 bg-slate-950 px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500';

export default function AdminUsers({ selfId, clientName, onClose }: Props) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [form, setForm] = useState({ username: '', email: '', password: '', role: 'recruiter' as AssignableRole });

  const load = useCallback(async () => {
    try { setUsers(await api.listUsers()); } catch (e: any) { setError(e.message); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const run = async (action: () => Promise<unknown>, done: string) => {
    setError(''); setNotice('');
    try { await action(); setNotice(done); await load(); } catch (e: any) { setError(e.message); }
  };

  const create = (e: React.FormEvent) => {
    e.preventDefault();
    void run(async () => {
      await api.createUser({ ...form, email: form.email.trim() || undefined });
      setForm({ username: '', email: '', password: '', role: 'recruiter' });
    }, `User ${form.username} created. They can sign in now and see all projects of ${clientName}.`);
  };

  const resetPassword = (u: AdminUser) => {
    const password = prompt(`New password for ${u.username} (at least 8 characters):`);
    if (password) void run(() => api.updateUser(u.id, { password }), `Password of ${u.username} changed.`);
  };

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-slate-900 px-6 py-3">
        <button onClick={onClose} aria-label="Back to chats" className="flex items-center gap-1 text-sm text-slate-400 hover:text-slate-100">
          <ArrowLeft className="h-4 w-4" /> Back to chats
        </button>
        <h2 className="font-semibold">Users of {clientName}</h2>
      </header>

      <div className="flex-1 space-y-6 overflow-y-auto px-6 py-4">
        <p className="text-xs text-slate-500">
          <b>Admin</b>: screens candidates and manages users. <b>Recruiter</b>: screens candidates only.
          Role and access changes apply within a few minutes (on the user&apos;s next session refresh).
        </p>
        {error && <p className="text-sm text-red-400">{error}</p>}
        {notice && <p className="text-sm text-emerald-400">{notice}</p>}

        <form onSubmit={create} className="flex flex-wrap items-end gap-2 rounded-xl border border-slate-800 bg-slate-900/40 p-3">
          <input className={input} placeholder="Username" required minLength={3} maxLength={64} pattern="[A-Za-z0-9_.@\-]+"
                 value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
          <input className={input} placeholder="Email (optional)" type="email"
                 value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          <input className={input} placeholder="Password (min 8)" type="password" required minLength={8} maxLength={128}
                 autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          <select className={input} value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as AssignableRole })}>
            <option value="recruiter">Recruiter</option>
            <option value="admin">Admin</option>
          </select>
          <button type="submit" className="flex items-center gap-1 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-700">
            <UserPlus className="h-4 w-4" /> Add user
          </button>
        </form>

        <table className="w-full text-left text-sm">
          <thead className="text-xs uppercase tracking-wider text-slate-500">
            <tr><th className="py-2">User</th><th>Role</th><th>Status</th><th>Last sign-in</th><th /></tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const self = u.id === selfId;
              return (
                <tr key={u.id} className="border-t border-slate-900">
                  <td className="py-2">
                    <div>{u.username}{self && <span className="text-slate-500"> (you)</span>}</div>
                    {u.email && <div className="text-xs text-slate-500">{u.email}</div>}
                  </td>
                  <td>
                    <select className={input} value={u.role} disabled={self}
                            onChange={(e) => void run(() => api.updateUser(u.id, { role: e.target.value as AssignableRole }),
                                                      `${u.username} is now ${e.target.value}.`)}>
                      <option value="recruiter">Recruiter</option>
                      <option value="admin">Admin</option>
                    </select>
                  </td>
                  <td>
                    <button disabled={self}
                            onClick={() => void run(() => api.updateUser(u.id, { isActive: !u.isActive }),
                                                    `${u.username} ${u.isActive ? 'disabled' : 'enabled'}.`)}
                            className={`rounded-lg px-2 py-1 text-xs disabled:opacity-60 ${u.isActive ? 'bg-emerald-900/50 text-emerald-300' : 'bg-slate-800 text-slate-400'}`}>
                      {u.isActive ? 'Active' : 'Disabled'}
                    </button>
                  </td>
                  <td className="text-xs text-slate-500">{u.lastLoginAt ? new Date(u.lastLoginAt).toLocaleString() : 'never'}</td>
                  <td className="text-right">
                    <Tip label="Set a new password for this user">
                      <button aria-label="Reset password" onClick={() => resetPassword(u)} className="text-slate-500 hover:text-slate-200">
                        <KeyRound className="h-4 w-4" />
                      </button>
                    </Tip>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="text-xs text-slate-500">You cannot change your own role or disable yourself; another admin must do that.</p>
      </div>
    </div>
  );
}
