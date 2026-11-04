'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { Bot } from 'lucide-react';
import AdminUsers from '../components/AdminUsers';
import ChatView from '../components/ChatView';
import LoginView from '../components/LoginView';
import Sidebar from '../components/Sidebar';
import Tip from '../components/Tip';
import { api, auth, LOGOUT_EVENT, refreshSession, scheduleRefresh } from '../lib/api';
import type { ChatDetail, ChatSummary, Session } from '../lib/types';

export default function Page() {
  const [ready, setReady] = useState(false);
  const [session, setSession] = useState<Session | null>(null);
  const [projectId, setProjectId] = useState<number | null>(null);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [active, setActive] = useState<ChatDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [adminOpen, setAdminOpen] = useState(false);
  const [error, setError] = useState('');

  const clearView = useCallback(() => {
    setSession(null); setChats([]); setActive(null); setAdminOpen(false);
  }, []);
  const logout = useCallback(() => { void api.logout(); }, []);

  const signedIn = (s: Session) => {
    setSession(s);
    setProjectId(auth.projectId());
  };

  // Restore the session from the httpOnly cookies: /auth/me, or a silent refresh when the access token expired.
  useEffect(() => {
    const restore = async () => {
      try {
        const fresh = await api.me().catch(async () => ((await refreshSession()) ? api.me() : null));
        if (fresh) {
          auth.remember(fresh);
          if (!fresh.projects.some((p) => p.id === auth.projectId()) && fresh.projects[0]) auth.setProject(fresh.projects[0].id);
          signedIn(fresh);
          if (!fresh.expiresIn) await refreshSession();   // start the silent-refresh cycle
        }
      } finally {
        setReady(true);
      }
    };
    void restore();
    window.addEventListener(LOGOUT_EVENT, clearView);
    return () => { window.removeEventListener(LOGOUT_EVENT, clearView); scheduleRefresh(null); };
  }, [clearView]);

  const refresh = useCallback(async () => {
    try { setChats(await api.listChats()); setError(''); } catch (e: any) { setError(e.message); }
  }, []);

  useEffect(() => {
    if (session && projectId) { setActive(null); refresh(); }
  }, [session, projectId, refresh]);

  const switchProject = (id: number) => {
    auth.setProject(id);
    setProjectId(id);
  };

  const open = async (id: string) => {
    setError(''); setAdminOpen(false);
    try { setActive(await api.getChat(id)); } catch (e: any) { setError(e.message); }
  };

  const newChat = async () => {
    try {
      const chat = await api.createChat();   // a chat id is mandatory before sending any message
      await refresh();
      await open(chat.id);
    } catch (e: any) { setError(e.message); }
  };

  const rename = async (id: string, title: string) => {
    try {
      await api.updateChat(id, { title });
      setActive((a) => (a && a.id === id ? { ...a, title } : a));
      await refresh();
    } catch (e: any) { setError(e.message); }
  };

  const remove = async (id: string) => {
    try {
      await api.deleteChat(id);
      if (active?.id === id) setActive(null);
      await refresh();
    } catch (e: any) { setError(e.message); }
  };

  if (!ready) return null;
  if (!session) return <LoginView onLogin={signedIn} />;
  if (!session.projects.length) {
    return <div className="flex h-screen items-center justify-center text-slate-400">
      You are not a member of any project yet. Ask an administrator of {session.client.name} for access.
    </div>;
  }

  return (
    <div className="flex h-screen bg-slate-950 text-slate-100">
      <Sidebar session={session} projectId={projectId} chats={chats} activeId={active?.id || null} busy={busy}
               onProject={switchProject} onNew={newChat} onSelect={open} onRename={rename} onDelete={remove}
               onLogout={logout} collapsed={sidebarCollapsed} onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
               onAdmin={session.user.role === 'admin' ? () => setAdminOpen(true) : undefined} />
      {adminOpen ? (
        <AdminUsers selfId={session.user.id} clientName={session.client.name} onClose={() => setAdminOpen(false)} />
      ) : active ? (
        <ChatView key={active.id} chat={active} onChanged={refresh} onBusy={setBusy}
                  title={chats.find((c) => c.id === active.id)?.title || active.title}
                  onRename={(t) => rename(active.id, t)} />
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-slate-500">
          <Bot className="h-10 w-10 text-blue-400" />
          <p>Start a new chat, or pick one from the list.</p>
          {error && <p className="text-xs text-red-400">{error}</p>}
          <Tip label="Start a new screening conversation in this project">
            <button onClick={newChat} aria-label="New chat" className="rounded-xl bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700">New chat</button>
          </Tip>
        </div>
      )}
    </div>
  );
}
