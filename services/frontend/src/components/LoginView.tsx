'use client';

import React, { useState } from 'react';
import { Bot, LogIn } from 'lucide-react';
import { api, auth } from '../lib/api';
import type { Session } from '../lib/types';
import Tip from './Tip';

/** Sign-in only: users are onboarded per client by an administrator (or the demo seed data). */
export default function LoginView({ onLogin }: { onLogin: (session: Session) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const session = await api.login(username.trim(), password);   // auth cookies are set by the server
      if (!session.projects.some((p) => p.id === auth.projectId()) && session.projects[0]) {
        auth.setProject(session.projects[0].id);
      }
      onLogin(session);
    } catch (err: any) {
      setError(err.message || 'Sign-in failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center bg-slate-950 px-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-2xl border border-slate-800 bg-slate-900 p-8">
        <div className="flex items-center gap-2">
          <Bot className="h-6 w-6 text-blue-400" />
          <h1 className="text-lg font-bold">ATS Screening Assistant</h1>
        </div>
        <p className="text-xs text-slate-400">Sign in to your recruiter workspace.</p>
        <input
          className="w-full rounded-xl border border-slate-800 bg-slate-950 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          placeholder="Username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)}
          required minLength={3} maxLength={64}
        />
        <input
          type="password"
          className="w-full rounded-xl border border-slate-800 bg-slate-950 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          placeholder="Password" autoComplete="current-password" value={password}
          onChange={(e) => setPassword(e.target.value)} required minLength={8} maxLength={128}
        />
        {error && <p className="text-xs text-red-400">{error}</p>}
        <Tip label="Sign in with your recruiter account">
          <button disabled={busy} aria-label="Sign in"
                  className="flex w-80 max-w-full items-center justify-center gap-2 rounded-xl bg-blue-600 py-2.5 text-sm font-semibold hover:bg-blue-700 disabled:bg-slate-700">
            <LogIn className="h-4 w-4" /> {busy ? 'Please wait...' : 'Sign in'}
          </button>
        </Tip>
      </form>
    </div>
  );
}
