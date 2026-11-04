'use client';

import React, { useState } from 'react';
import { Building2, LogOut, MessageSquarePlus, PanelLeftClose, PanelLeftOpen, Pencil, Trash2, Users } from 'lucide-react';
import type { ChatSummary, Session } from '../lib/types';
import RenameInput from './RenameInput';
import { scoreClass } from './ScreeningCard';
import Tip from './Tip';

interface Props {
  session: Session;
  projectId: number | null;
  onProject: (id: number) => void;
  chats: ChatSummary[];
  activeId: string | null;
  busy: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onLogout: () => void;
  collapsed: boolean;
  onToggle: () => void;
  onAdmin?: () => void;   // only passed for admins
}

const ago = (iso: string) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
};

export default function Sidebar({ session, projectId, onProject, chats, activeId, busy, onNew, onSelect, onRename, onDelete,
                                 onLogout, collapsed, onToggle, onAdmin }: Props) {
  const [editing, setEditing] = useState<string | null>(null);

  if (collapsed) {
    // Slim rail: expand, new chat, sign out (tooltips explain each icon).
    return (
      <aside className="flex w-14 shrink-0 flex-col items-center gap-3 border-r border-slate-900 bg-slate-900/50 py-4">
        <Tip label="Show chats and projects" side="bottom">
          <button aria-label="Expand sidebar" onClick={onToggle} className="rounded-lg p-2 text-slate-300 hover:bg-slate-800">
            <PanelLeftOpen className="h-5 w-5" />
          </button>
        </Tip>
        <Tip label="Start a new screening conversation in this project" side="bottom">
          <button aria-label="New chat" onClick={onNew} disabled={busy} className="rounded-lg bg-blue-600 p-2 hover:bg-blue-700 disabled:bg-slate-700">
            <MessageSquarePlus className="h-5 w-5" />
          </button>
        </Tip>
        <div className="flex-1" />
        {onAdmin && (
          <Tip label="Manage users (admin)">
            <button aria-label="Manage users" onClick={onAdmin} className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-slate-100">
              <Users className="h-5 w-5" />
            </button>
          </Tip>
        )}
        <Tip label="Sign out of this browser">
          <button aria-label="Sign out" onClick={onLogout} className="rounded-lg p-2 text-slate-400 hover:bg-slate-800 hover:text-slate-100">
            <LogOut className="h-5 w-5" />
          </button>
        </Tip>
      </aside>
    );
  }

  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-slate-900 bg-slate-900/50">
      <div className="space-y-3 p-4">
        <div className="flex items-center gap-2 text-xs text-slate-400">
          <Building2 className="h-4 w-4" /> <span className="min-w-0 flex-1 truncate">{session.client.name}</span>
          <Tip label="Collapse the sidebar" side="bottom">
            <button aria-label="Collapse sidebar" onClick={onToggle} className="rounded-lg p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200">
              <PanelLeftClose className="h-4 w-4" />
            </button>
          </Tip>
        </div>
        <Tip label="Project you are working in: chats, resumes and screenings belong to it" side="bottom">
          <select
            aria-label="Project" value={projectId ?? ''} disabled={busy}
            onChange={(e) => onProject(Number(e.target.value))}
            className="w-60 rounded-lg border border-slate-800 bg-slate-950 px-2 py-1.5 text-sm text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {session.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Tip>
        <Tip label="Start a new screening conversation in this project" side="bottom">
          <button onClick={onNew} disabled={busy} aria-label="New chat"
                  className="flex w-60 items-center justify-center gap-2 rounded-xl bg-blue-600 py-2.5 text-sm font-semibold hover:bg-blue-700 disabled:bg-slate-700">
            <MessageSquarePlus className="h-4 w-4" /> New chat
          </button>
        </Tip>
      </div>
      <nav className="flex-1 space-y-1 overflow-y-auto px-2">
        {chats.length === 0 && <p className="px-3 text-xs text-slate-500">No chats yet.</p>}
        {chats.map((c) => (
          <div
            key={c.id}
            onClick={() => !busy && editing !== c.id && onSelect(c.id)}
            onDoubleClick={() => !busy && setEditing(c.id)}
            className={`group flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm ${c.id === activeId ? 'bg-slate-800' : 'hover:bg-slate-800/60'} ${busy ? 'cursor-wait' : ''}`}
          >
            <div className="min-w-0 flex-1">
              {editing === c.id ? (
                <RenameInput value={c.title} onCancel={() => setEditing(null)}
                             onSave={(t) => { setEditing(null); onRename(c.id, t); }} />
              ) : (
                <p className="truncate text-slate-200">{c.title}</p>
              )}
              <p className="text-[11px] text-slate-500">{ago(c.updatedAt)}</p>
            </div>
            {c.lastScore !== null && editing !== c.id && (
              <Tip label="Score of the latest screening in this chat">
                <span className={`rounded border px-1.5 text-[11px] font-bold ${scoreClass(c.lastScore)}`}>{c.lastScore}</span>
              </Tip>
            )}
            {editing !== c.id && (
              <div className="hidden items-center gap-1 group-hover:flex group-focus-within:flex">
                <Tip label="Rename chat (or double-click the title)">
                  <button aria-label="Rename chat" disabled={busy}
                          onClick={(e) => { e.stopPropagation(); setEditing(c.id); }}
                          className="text-slate-500 hover:text-slate-200">
                    <Pencil className="h-4 w-4" />
                  </button>
                </Tip>
                <Tip label="Delete chat and its history">
                  <button aria-label="Delete chat" disabled={busy}
                          onClick={(e) => { e.stopPropagation(); if (confirm(`Delete "${c.title}"?`)) onDelete(c.id); }}
                          className="text-slate-500 hover:text-red-400">
                    <Trash2 className="h-4 w-4" />
                  </button>
                </Tip>
              </div>
            )}
          </div>
        ))}
      </nav>
      {onAdmin && (
        <button onClick={onAdmin} aria-label="Manage users"
                className="mx-4 mb-2 flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-slate-400 hover:bg-slate-800 hover:text-slate-100">
          <Users className="h-4 w-4" /> Manage users
        </button>
      )}
      <div className="flex items-center justify-between border-t border-slate-900 p-4 text-sm">
        <span className="truncate text-slate-400">{session.user.username} · {session.user.role}</span>
        <Tip label="Sign out of this browser">
          <button onClick={onLogout} aria-label="Sign out" className="flex items-center gap-1 text-slate-400 hover:text-slate-100">
            <LogOut className="h-4 w-4" /> Sign out
          </button>
        </Tip>
      </div>
    </aside>
  );
}
