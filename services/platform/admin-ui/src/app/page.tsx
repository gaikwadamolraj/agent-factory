'use client';
// The admin console shell: sign-in gate, a cosmetic admin-role gate (the REAL enforcement is server-side in
// registry-service - this UI is not a security boundary), the tab layout, and a sign-out / assume-client header.
import { useEffect, useState } from 'react';
import { api, auth, LOGOUT_EVENT, scheduleRefresh } from '../lib/api';
import type { Session } from '../lib/types';
import { Button } from '../components/ui';
import LoginView from '../components/LoginView';
import SkillsTab from '../components/SkillsTab';
import ApprovalsTab from '../components/ApprovalsTab';
import NotificationsTab from '../components/NotificationsTab';
import PoliciesTab from '../components/PoliciesTab';
import UsersTab from '../components/UsersTab';
import ClientsTab from '../components/ClientsTab';
import CatalogueTab from '../components/CatalogueTab';

const TABS = ['Skills', 'Approvals', 'Notifications', 'Policies & groups', 'Users', 'Clients & projects', 'Catalogue'] as const;
type Tab = (typeof TABS)[number];

// Roles that may load the console. This gate is for UX only; every action is authorized again server-side.
const ADMIN_ROLES = new Set(['admin', 'client_admin', 'super_admin']);

export default function Page() {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [tab, setTab] = useState<Tab>('Skills');
  const [assumeTick, setAssumeTick] = useState(0);   // re-render after the assume-client switch

  useEffect(() => {
    // Restore the session on load: trust the cached profile, then confirm with /me (which also refreshes cookies).
    const cached = auth.session();
    if (cached) setSession(cached);
    api
      .me()
      .then((s) => {
        auth.remember(s);
        scheduleRefresh(s.expiresIn);
        setSession(s);
      })
      .catch(() => setSession(null))
      .finally(() => setReady(true));

    const onLogout = () => setSession(null);
    window.addEventListener(LOGOUT_EVENT, onLogout);
    return () => window.removeEventListener(LOGOUT_EVENT, onLogout);
  }, []);

  if (!ready && !session) {
    return <div className="flex min-h-screen items-center justify-center text-slate-500">Loading...</div>;
  }
  if (!session) {
    return <LoginView onSignedIn={setSession} />;
  }
  if (!ADMIN_ROLES.has(session.user.role)) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-center">
        <p className="text-lg text-slate-200">Not authorized</p>
        <p className="max-w-sm text-sm text-slate-500">
          Your account ({session.user.username}, role {session.user.role}) cannot use the admin console.
        </p>
        <Button variant="ghost" onClick={() => void api.logout()}>
          Sign out
        </Button>
      </div>
    );
  }

  const assumed = auth.assumedClientId();

  return (
    <div className="mx-auto min-h-screen max-w-6xl px-4 py-5">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-3">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">ATS Platform Admin</h1>
          <p className="text-xs text-slate-500">
            {session.user.username} · {session.user.role} · {session.client.name}
            {assumed ? <span className="text-amber-400"> · acting in client {assumed}</span> : null}
          </p>
        </div>
        <Button variant="ghost" onClick={() => void api.logout()}>
          Sign out
        </Button>
      </header>

      <nav className="mb-5 flex flex-wrap gap-1">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`rounded px-3 py-1.5 text-sm ${
              tab === t ? 'bg-indigo-600 text-white' : 'text-slate-300 hover:bg-slate-800'
            }`}
          >
            {t}
          </button>
        ))}
      </nav>

      <main key={`${tab}-${assumeTick}`}>
        {tab === 'Skills' && <SkillsTab />}
        {tab === 'Approvals' && <ApprovalsTab />}
        {tab === 'Notifications' && <NotificationsTab />}
        {tab === 'Policies & groups' && <PoliciesTab />}
        {tab === 'Users' && <UsersTab />}
        {tab === 'Clients & projects' && (
          <ClientsTab session={session} onAssume={() => setAssumeTick((n) => n + 1)} />
        )}
        {tab === 'Catalogue' && <CatalogueTab />}
      </main>
    </div>
  );
}
