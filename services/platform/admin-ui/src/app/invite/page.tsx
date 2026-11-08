'use client';
// Accept-invite page: the target of the invite link the auth-service builds (INVITE_BASE_URL = /agent-factory/invite?token=).
// The invitee sets their own password; on success the auth-service signs them in (sets cookies) and we land in the
// console. The token is read from the URL on the client (no useSearchParams, so the page needs no Suspense boundary).
import { useEffect, useState } from 'react';
import { api, auth, scheduleRefresh } from '../../lib/api';
import { Button, Card, ErrorBanner, Field, Input } from '../../components/ui';

export default function InvitePage() {
  const [token, setToken] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    try {
      setToken(new URLSearchParams(window.location.search).get('token') || '');
    } catch {
      /* no token */
    }
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError(new Error('The passwords do not match.'));
      return;
    }
    setBusy(true);
    try {
      const session = await api.acceptInvite(token, password);
      auth.remember(session);
      scheduleRefresh(session.expiresIn);
      setDone(true);
      const base = process.env.NEXT_PUBLIC_BASE_PATH || '';
      window.location.href = `${base}/`;
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <Card className="w-full max-w-sm">
        <h1 className="mb-1 text-lg font-semibold text-slate-100">Accept your invitation</h1>
        <p className="mb-4 text-sm text-slate-400">Set a password to activate your account.</p>
        <ErrorBanner error={error} onClose={() => setError(null)} />
        {!token ? (
          <p className="text-sm text-amber-300">This link is missing its token. Ask your administrator for a new invite.</p>
        ) : done ? (
          <p className="text-sm text-emerald-300">Done. Signing you in...</p>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            <Field label="New password">
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
              />
            </Field>
            <Field label="Confirm password">
              <Input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
              />
            </Field>
            <Button type="submit" variant="primary" disabled={busy || password.length < 8}>
              {busy ? 'Activating...' : 'Activate account'}
            </Button>
          </form>
        )}
      </Card>
    </div>
  );
}
