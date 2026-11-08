'use client';
import { useState } from 'react';
import { api } from '../lib/api';
import type { Session } from '../lib/types';
import { Button, Card, ErrorBanner, Field, Input } from './ui';

/** Sign-in against the auth-service (same credentials as the recruiter app; admin access is enforced server-side). */
export default function LoginView({ onSignedIn }: { onSignedIn: (s: Session) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await api.login(username, password));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <Card className="w-full max-w-sm">
        <h1 className="mb-1 text-lg font-semibold text-slate-100">ATS Platform Admin</h1>
        <p className="mb-4 text-sm text-slate-400">Sign in to manage skills, policies and users.</p>
        <ErrorBanner error={error} onClose={() => setError(null)} />
        <form onSubmit={submit} className="space-y-3">
          <Field label="Username">
            <Input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" />
          </Field>
          <Field label="Password">
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
          </Field>
          <Button type="submit" variant="primary" disabled={busy || !username || !password}>
            {busy ? 'Signing in...' : 'Sign in'}
          </Button>
        </form>
      </Card>
    </div>
  );
}
