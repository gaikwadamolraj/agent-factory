'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, FileText, PanelRightOpen, Pencil, ScanSearch, Send, Square, Upload, X } from 'lucide-react';
import { api, MAX_FILE_MB } from '../lib/api';
import type { ChatDetail, DocumentInfo, DocumentKind, Message, TurnEvent } from '../lib/types';
import DocumentPanel from './DocumentPanel';
import Markdown from './Markdown';
import OnboardingStatus from './OnboardingStatus';
import ProgressTimeline, { Step } from './ProgressTimeline';
import RenameInput from './RenameInput';
import ScreeningCard from './ScreeningCard';
import Tip from './Tip';

interface Props {
  chat: ChatDetail;
  title: string;                    // live title from the chat list (renames, auto-titles)
  onChanged: () => void;            // refresh the sidebar (title/score/updated time)
  onBusy: (busy: boolean) => void;
  onRename: (title: string) => void;
}

const isPdf = (f: File) => f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf');
const isText = (f: File) => /\.(txt|md)$/i.test(f.name);

function Bubble({ m, onOpenDocument }: { m: Message; onOpenDocument: (kind: DocumentKind) => void }) {
  const user = m.role === 'user';
  return (
    <div className={`flex w-full ${user ? 'justify-end' : 'justify-start'}`}>
      <div className={`max-w-[85%] rounded-2xl px-4 py-3 ${user ? 'bg-blue-600 text-white' : 'border border-slate-800 bg-slate-900 text-slate-100'}`}>
        {m.data.attachments?.map((a) => (
          <React.Fragment key={a.kind}>
            <Tip label="Show this document in the side panel">
              <button onClick={() => onOpenDocument(a.kind)} aria-label={`Open ${a.kind}`}
                      className="mb-1 flex items-center gap-1 text-left text-xs opacity-80 underline-offset-2 hover:underline hover:opacity-100">
                <FileText className="h-3.5 w-3.5" /> {a.kind === 'resume' ? 'Resume' : 'Job description'}: {a.filename} · {a.pages} page(s)
              </button>
            </Tip>
            {a.onboardingRunId && <OnboardingStatus runId={a.onboardingRunId} />}
          </React.Fragment>
        ))}
        {m.data.jdUpdated && !m.data.attachments?.some((a) => a.kind === 'jd') && (
          <div className="mb-1 text-xs opacity-80">Job description updated</div>
        )}
        {user ? <p className="whitespace-pre-wrap text-sm">{m.content}</p> : <Markdown text={m.content} />}
        {m.data.screening && <ScreeningCard s={m.data.screening} />}
        {m.data.aborted && <p className="mt-1 text-[11px] text-amber-400">Stopped by you; no tokens were spent after the stop.</p>}
        {!user && !!m.data.toolsUsed?.length && (
          <p className="mt-2 text-[11px] text-slate-500">Tools used: {m.data.toolsUsed.join(', ')}</p>
        )}
        {!user && m.data.errorCode && !m.data.aborted && <p className="mt-1 text-[11px] text-slate-500">Code: {m.data.errorCode}</p>}
      </div>
    </div>
  );
}

function FilePick({ label, tip, accept, file, stored, onPick, onClear, disabled }: {
  label: string; tip: string; accept: string; file: File | null; stored: DocumentInfo | null;
  onPick: (f: File) => void; onClear: () => void; disabled: boolean;
}) {
  const ref = useRef<HTMLInputElement>(null);
  return (
    <div className="flex items-center gap-2">
      <input ref={ref} type="file" accept={accept} className="hidden"
             onChange={(e) => { const f = e.target.files?.[0]; if (f) onPick(f); e.target.value = ''; }} />
      <Tip label={tip}>
        <button aria-label={label} disabled={disabled} onClick={() => ref.current?.click()}
                className="flex items-center gap-1 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-200 hover:bg-slate-800 disabled:opacity-50">
          <Upload className="h-3.5 w-3.5" /> {label}
        </button>
      </Tip>
      {file ? (
        <span className="flex min-w-0 items-center gap-1 rounded-md bg-blue-900/40 px-2 py-1 text-xs text-blue-200">
          <FileText className="h-3.5 w-3.5 shrink-0" /> <span className="truncate">{file.name}</span>
          <Tip label="Remove this file (not sent yet)">
            <button aria-label="Remove file" onClick={onClear}><X className="h-3.5 w-3.5" /></button>
          </Tip>
        </span>
      ) : stored ? (
        <span className="flex min-w-0 items-center gap-1 text-xs text-slate-400">
          <FileText className="h-3.5 w-3.5 shrink-0" /> <span className="truncate">{stored.filename}</span> · {stored.pages} p.
        </span>
      ) : (
        <span className="text-xs text-slate-500">none yet</span>
      )}
    </div>
  );
}

export default function ChatView({ chat, title, onChanged, onBusy, onRename }: Props) {
  const [renaming, setRenaming] = useState(false);
  const [messages, setMessages] = useState<Message[]>(chat.messages);
  const [resumeDoc, setResumeDoc] = useState<DocumentInfo | null>(chat.resumeDocument);
  const [jdDoc, setJdDoc] = useState<DocumentInfo | null>(chat.jdDocument);
  const [jd, setJd] = useState(chat.jobDescription || '');
  const [savedJd, setSavedJd] = useState(chat.jobDescription || '');
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [jdFile, setJdFile] = useState<File | null>(null);
  const [inputsOpen, setInputsOpen] = useState(!chat.jobDescription || !chat.resumeDocument);
  const [panelOpen, setPanelOpen] = useState(true);
  const [panelExpanded, setPanelExpanded] = useState(false);
  const [docTab, setDocTab] = useState<DocumentKind>('resume');
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const [steps, setSteps] = useState<Step[]>([]);
  const [progress, setProgress] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [turnId, setTurnId] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // Block body on purpose: newer Chrome/Edge return a Promise from scrollIntoView, and React would treat a
  // returned value as the effect's cleanup function and call it ("n is not a function").
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, steps]);

  const checkFile = (f: File, allowText: boolean): boolean => {
    setError('');
    if (!(isPdf(f) || (allowText && isText(f)))) {
      setError(allowText ? 'Job description: use a PDF, TXT or MD file.' : 'Resume: only PDF files are allowed.');
      return false;
    }
    if (f.size > MAX_FILE_MB * 1024 * 1024) {
      setError(`${f.name} is larger than ${MAX_FILE_MB} MB.`);
      return false;
    }
    return true;
  };

  const reloadDocuments = useCallback(async () => {
    try {
      const fresh = await api.getChat(chat.id);
      setResumeDoc(fresh.resumeDocument); setJdDoc(fresh.jdDocument);
      setJd(fresh.jobDescription || ''); setSavedJd(fresh.jobDescription || '');
    } catch { /* keep the current view */ }
  }, [chat.id]);

  const hasJd = Boolean(jdFile || jd.trim());
  const hasResume = Boolean(resumeFile || resumeDoc);

  const send = async (action: 'chat' | 'screen') => {
    if (running) return;
    if (action === 'chat' && !text.trim()) return;
    if (action === 'screen' && !hasJd) return setError('Add the job description first (paste it or upload a file).');
    if (action === 'screen' && !hasResume) return setError('Attach the resume PDF first.');

    const form = new FormData();
    form.append('content', text.trim());
    form.append('action', action);
    if (jdFile) form.append('jdFile', jdFile);
    else if (jd !== savedJd) form.append('jobDescription', jd);
    if (resumeFile) form.append('resumeFile', resumeFile);

    setRunning(true); onBusy(true); setError(''); setSteps([]); setProgress(1); setElapsed(0); setTurnId(null);
    controller.current = new AbortController();
    const started = Date.now();
    const ticker = setInterval(() => setElapsed((Date.now() - started) / 1000), 1000);
    try {
      await api.sendMessage(chat.id, form, (e: TurnEvent) => {
        if (e.type === 'turn_started' && e.turnId) setTurnId(e.turnId);
        if (typeof e.progress === 'number') setProgress((p) => Math.max(p, e.progress as number));
        if (e.type === 'progress' && e.stage) setSteps((s) => [...s, { stage: e.stage!, message: e.message, at: e.elapsed || 0 }]);
        if (e.type === 'user_message') {
          // The files are stored by now: load the stored copies first, then drop the local ones, so the panel never
          // goes blank while the turn is running.
          setMessages((m) => [...m, e.message]);
          setText(''); setInputsOpen(false);
          void reloadDocuments().finally(() => { setResumeFile(null); setJdFile(null); });
        }
        if (e.type === 'message' || e.type === 'aborted') setMessages((m) => [...m, e.message]);
        if (e.type === 'error' && e.error) setError(`${e.error.message} ${e.error.hint} (${e.error.code})`);
      }, controller.current.signal);
    } catch (err: any) {
      if (err.name !== 'AbortError') setError(err.message);
    } finally {
      clearInterval(ticker); setRunning(false); onBusy(false); setTurnId(null); controller.current = null;
      onChanged(); reloadDocuments();
    }
  };

  /** Stop the running turn: the server cancels the assistant and the agent's LLM calls (no more token spend). */
  const stop = async () => {
    try {
      if (turnId) await api.abortTurn(chat.id, turnId);   // server emits 'aborted' and closes the stream
      else controller.current?.abort();                  // turn not registered yet: closing the stream cancels it
    } catch (err: any) {
      controller.current?.abort();
      setError(err.message);
    }
  };

  return (
    <div className="flex min-w-0 flex-1">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-slate-900 px-6 py-3">
          <div className="flex min-w-0 flex-1 items-center gap-2">
            {renaming ? (
              <RenameInput value={title} className="max-w-md font-semibold"
                           onCancel={() => setRenaming(false)}
                           onSave={(t) => { setRenaming(false); onRename(t); }} />
            ) : (
              <>
                <h2 className="truncate font-semibold" onDoubleClick={() => setRenaming(true)}>{title}</h2>
                <Tip label="Rename this chat" side="bottom">
                  <button aria-label="Rename chat" onClick={() => setRenaming(true)} className="text-slate-500 hover:text-slate-200">
                    <Pencil className="h-4 w-4" />
                  </button>
                </Tip>
              </>
            )}
          </div>
          <Tip label={inputsOpen ? 'Hide the job description and resume inputs' : 'Show the job description, resume and the Screen candidate button'} side="bottom">
            <button aria-label="Toggle inputs" onClick={() => setInputsOpen(!inputsOpen)}
                    className={`flex items-center gap-1 rounded-lg px-2 py-1 text-xs ${hasJd && hasResume ? 'bg-slate-800 text-slate-200' : 'bg-amber-900/40 text-amber-300'}`}>
              {inputsOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />} JD &amp; resume
            </button>
          </Tip>
          {!panelOpen && (
            <Tip label="Show the document panel" side="bottom">
              <button aria-label="Show documents" onClick={() => setPanelOpen(true)}
                      className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-800 hover:text-slate-200">
                <PanelRightOpen className="h-4 w-4" />
              </button>
            </Tip>
          )}
        </header>

        {inputsOpen && (
          <div className="grid gap-3 border-b border-slate-900 bg-slate-900/40 px-6 py-3 md:grid-cols-2">
            <div className="space-y-2 rounded-xl border border-slate-800 bg-slate-950/60 p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">Job description</span>
                <FilePick label="Upload JD" tip={`Upload the job description (PDF, TXT or MD, max ${MAX_FILE_MB} MB)`}
                          accept="application/pdf,.pdf,.txt,.md,text/plain,text/markdown" file={jdFile} stored={jdDoc}
                          disabled={running} onClear={() => setJdFile(null)}
                          onPick={(f) => { if (checkFile(f, true)) setJdFile(f); }} />
              </div>
              <textarea
                className="h-28 w-full resize-y rounded-lg border border-slate-800 bg-slate-950 p-2 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
                placeholder={jdFile ? 'Using the uploaded file' : 'Or paste the job description here'}
                value={jdFile ? '' : jd} onChange={(e) => setJd(e.target.value)} disabled={running || Boolean(jdFile)}
              />
            </div>
            <div className="space-y-2 rounded-xl border border-slate-800 bg-slate-950/60 p-3">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">Candidate resume</span>
              <FilePick label="Upload resume" tip={`Attach the candidate's resume (PDF only, max ${MAX_FILE_MB} MB)`}
                        accept="application/pdf,.pdf" file={resumeFile} stored={resumeDoc} disabled={running}
                        onClear={() => setResumeFile(null)} onPick={(f) => { if (checkFile(f, false)) setResumeFile(f); }} />
            </div>
            <div className="flex items-center justify-between gap-3 md:col-span-2">
              <p className={`text-xs ${hasJd && hasResume ? 'text-slate-400' : 'text-amber-300'}`}>
                {hasJd && hasResume
                  ? 'Ready: compares the resume with the job description and gives a score and verdict.'
                  : `Still needed: ${[!hasJd && 'job description', !hasResume && 'resume PDF'].filter(Boolean).join(' and ')}.`}
              </p>
              <button aria-label="Screen candidate" onClick={() => send('screen')} disabled={running || !hasJd || !hasResume}
                      className="flex shrink-0 items-center gap-1.5 rounded-xl bg-emerald-700 px-4 py-2 text-sm font-semibold hover:bg-emerald-600 disabled:bg-slate-700 disabled:text-slate-400">
                <ScanSearch className="h-4 w-4" /> Screen candidate
              </button>
            </div>
          </div>
        )}

        <div className="flex-1 space-y-4 overflow-y-auto px-6 py-4">
          {messages.length === 0 && !running && (
            <div className="mt-10 text-center text-sm text-slate-500">
              <p>Add the job description and resume above, then press <b>Screen candidate</b>.</p>
              <p className="mt-1">Use the message box below for questions, e.g. &quot;Which certifications does the candidate have?&quot;</p>
            </div>
          )}
          {messages.map((m) => <Bubble key={m.id} m={m} onOpenDocument={(k) => { setDocTab(k); setPanelOpen(true); }} />)}
          {running && (
            <div className="flex justify-start">
              <div className="w-full max-w-[85%] rounded-2xl border border-slate-800 bg-slate-900 px-4 py-3">
                <ProgressTimeline steps={steps} progress={progress} elapsed={elapsed} />
              </div>
            </div>
          )}
          <div ref={endRef} />
        </div>

        <footer className="border-t border-slate-900 px-6 py-3">
          {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
          <div className="flex items-end gap-2">
            <textarea
              rows={1}
              className="max-h-40 min-h-[42px] flex-1 resize-y rounded-xl border border-slate-800 bg-slate-950 px-3 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
              placeholder="Ask a question about the candidate or the job description (Enter to send)"
              value={text} disabled={running}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send('chat'); } }}
            />
            {running ? (
              <Tip label="Stop this request now; the model stops generating and no more tokens are spent">
                <button aria-label="Stop" onClick={stop}
                        className="flex items-center gap-1 rounded-xl bg-red-700 px-3 py-2.5 text-sm font-semibold hover:bg-red-600">
                  <Square className="h-4 w-4" /> Stop
                </button>
              </Tip>
            ) : (
              <button aria-label="Send message" onClick={() => send('chat')} disabled={!text.trim()}
                      className="flex items-center gap-1 rounded-xl bg-blue-600 px-3 py-2.5 text-sm font-semibold hover:bg-blue-700 disabled:bg-slate-700 disabled:text-slate-400">
                <Send className="h-4 w-4" /> Send
              </button>
            )}
          </div>
        </footer>
      </div>

      {panelOpen && (
        <DocumentPanel chatId={chat.id} resume={resumeDoc} jd={jdDoc} jdText={savedJd} pendingResume={resumeFile}
                       pendingJd={jdFile} expanded={panelExpanded} tab={docTab} onTab={setDocTab}
                       onToggleExpanded={() => setPanelExpanded(!panelExpanded)}
                       onClose={() => { setPanelOpen(false); setPanelExpanded(false); }} />
      )}
    </div>
  );
}
