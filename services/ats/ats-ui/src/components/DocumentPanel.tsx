'use client';

// Right-hand document viewer: resume / JD preview (PDF) or extracted text. Can be collapsed, or detached to a
// full-screen view. Files selected but not sent yet are previewed locally.
import React, { useEffect, useState } from 'react';
import { ExternalLink, FileText, Maximize2, Minimize2, PanelRightClose } from 'lucide-react';
import { api } from '../lib/api';
import type { DocumentInfo, DocumentKind } from '../lib/types';
import Tip from './Tip';

interface Props {
  chatId: string;
  resume: DocumentInfo | null;
  jd: DocumentInfo | null;
  jdText: string;
  pendingResume: File | null;
  pendingJd: File | null;
  tab: DocumentKind;
  onTab: (tab: DocumentKind) => void;
  expanded: boolean;
  onToggleExpanded: () => void;
  onClose: () => void;
}

const kb = (bytes: number) => (bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`);

export default function DocumentPanel(p: Props) {
  const { tab, onTab: setTab } = p;
  const [mode, setMode] = useState<'preview' | 'text'>('preview');
  const [url, setUrl] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [error, setError] = useState('');

  const pending = tab === 'resume' ? p.pendingResume : p.pendingJd;
  const stored = tab === 'resume' ? p.resume : p.jd;
  const isPdf = pending ? pending.type === 'application/pdf' || pending.name.toLowerCase().endsWith('.pdf')
                        : stored?.contentType === 'application/pdf';
  // Reload only when the document itself changes. The chat refetches its documents after every message, which
  // yields new but identical objects; depending on those would re-download the PDF and make the preview flicker.
  const storedKey = stored ? `${stored.filename}|${stored.createdAt}|${stored.sizeBytes}` : '';

  useEffect(() => {
    let revoke: string | null = null;
    let cancelled = false;
    // Keep showing the current document until the next one is ready (no blank frame in between).
    const showUrl = (u: string) => { if (!cancelled) { setUrl(u); setText(''); setError(''); } };
    const showText = (t: string) => { if (!cancelled) { setText(t); setUrl(null); setError(''); } };
    (async () => {
      try {
        if (pending) {
          if (isPdf) { revoke = URL.createObjectURL(pending); showUrl(revoke); }
          else showText(await pending.text());
        } else if (stored) {
          if (mode === 'preview' && isPdf) {
            revoke = await api.documentFileUrl(p.chatId, tab);
            showUrl(revoke);
          } else {
            showText((await api.documentText(p.chatId, tab)).text);
          }
        } else {
          showText(tab === 'jd' ? p.jdText : '');
        }
      } catch (e: any) {
        if (!cancelled) { setError(e.message); setUrl(null); setText(''); }
      }
    })();
    return () => {
      cancelled = true;
      // Revoke a little later: the frame may still be showing this blob until the next one replaces it.
      if (revoke) { const old = revoke; setTimeout(() => URL.revokeObjectURL(old), 30_000); }
    };
  }, [p.chatId, tab, mode, pending, storedKey, isPdf, p.jdText]);

  /** Open the current document in a new tab from the in-memory blob (authenticated fetch; no public URL). */
  const openInNewTab = async () => {
    try {
      let href = url;
      if (!href) {
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        href = URL.createObjectURL(blob);
        setTimeout(() => URL.revokeObjectURL(href as string), 60_000);
      }
      window.open(href, '_blank', 'noopener,noreferrer');
    } catch (e: any) {
      setError(e.message);
    }
  };

  const name = pending?.name || stored?.filename || (tab === 'jd' && p.jdText ? 'Pasted job description' : '');
  const meta = pending ? `${kb(pending.size)} · not sent yet` : stored ? `${stored.pages} page(s) · ${kb(stored.sizeBytes)}` : '';

  const shell = p.expanded
    ? 'fixed inset-4 z-40 rounded-2xl border border-slate-700 bg-slate-950 shadow-2xl'
    : 'w-[420px] shrink-0 border-l border-slate-900 bg-slate-900/40';

  return (
    <aside className={`flex flex-col ${shell}`}>
      <div className="flex items-center gap-1 border-b border-slate-900 px-3 py-2">
        {(['resume', 'jd'] as DocumentKind[]).map((k) => (
          <button key={k} onClick={() => setTab(k)} aria-label={k === 'resume' ? 'Resume tab' : 'Job description tab'}
                  className={`rounded-lg px-2.5 py-1 text-xs ${tab === k ? 'bg-slate-800 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}>
            {k === 'resume' ? 'Resume' : 'Job description'}
          </button>
        ))}
        <div className="ml-auto flex items-center gap-1">
          {isPdf && (
            <Tip label={mode === 'preview' ? 'Show the extracted text' : 'Show the original PDF'} side="bottom">
              <button aria-label="Toggle preview or text" onClick={() => setMode(mode === 'preview' ? 'text' : 'preview')}
                      className="rounded-lg px-2 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-slate-200">
                {mode === 'preview' ? 'Text' : 'PDF'}
              </button>
            </Tip>
          )}
          {(url || text) && (
            <Tip label="Open this document in a new browser tab (private copy, not a public link)" side="bottom">
              <button aria-label="Open in new tab" onClick={openInNewTab}
                      className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-200">
                <ExternalLink className="h-4 w-4" />
              </button>
            </Tip>
          )}
          <Tip label={p.expanded ? 'Back to the side panel' : 'Detach to a full view'} side="bottom">
            <button aria-label={p.expanded ? 'Dock panel' : 'Full view'} onClick={p.onToggleExpanded}
                    className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-200">
              {p.expanded ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </button>
          </Tip>
          <Tip label="Hide the document panel" side="bottom">
            <button aria-label="Hide documents" onClick={p.onClose}
                    className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-200">
              <PanelRightClose className="h-4 w-4" />
            </button>
          </Tip>
        </div>
      </div>
      {name && (
        <div className="flex items-center gap-2 border-b border-slate-900 px-3 py-2 text-xs text-slate-300">
          <FileText className="h-3.5 w-3.5 shrink-0" /> <span className="truncate">{name}</span>
          <span className="ml-auto shrink-0 text-slate-500">{meta}</span>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto p-3">
        {error && <p className="text-xs text-red-400">{error}</p>}
        {!error && url && <iframe title={`${tab} preview`} src={url} className="h-full min-h-[480px] w-full rounded-lg bg-white" />}
        {!error && !url && text && <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed text-slate-300">{text}</pre>}
        {!error && !url && !text && !name && (
          <p className="mt-10 text-center text-xs text-slate-500">
            {tab === 'resume' ? 'No resume yet. Attach the candidate PDF above.' : 'No job description yet. Paste it or upload a file above.'}
          </p>
        )}
      </div>
    </aside>
  );
}
