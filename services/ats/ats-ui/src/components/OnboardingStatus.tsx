'use client';

// One line under an uploaded resume: what the durable onboarding run is doing (parse -> scan -> subject ->
// index -> knowledge graph). It reads the orchestrator's `workflow_runs` projection, which answers without
// the workflow engine being reachable, so this keeps showing the last known state during an engine outage
// instead of going blank.
//
// Polling, not streaming, on purpose: a run outlives the chat turn and the page, so there is no open stream
// to attach to. It stops as soon as the run reaches a final state, and gives up after a bounded time rather
// than polling a stuck run forever.
import React, { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, Loader2, ShieldAlert, XCircle } from 'lucide-react';
import { api } from '../lib/api';
import { RUN_FINISHED, type Run } from '../lib/types';

const POLL_MS = 4000;
const MAX_POLLS = 150; // ~10 minutes; a longer onboarding is still running, we just stop asking

function label(run: Run): { icon: React.ReactNode; text: string; tone: string } {
  switch (run.status) {
    case 'completed': {
      const chunks = run.resultSummary.chunks ?? 0;
      const graph = run.resultSummary.episodeId ? ', added to the knowledge graph' : '';
      return { icon: <CheckCircle2 className="h-3 w-3" />, tone: 'text-emerald-200',
               text: `Candidate onboarded (${chunks} passage(s)${graph})` };
    }
    case 'failed':
      return { icon: <XCircle className="h-3 w-3" />, tone: 'text-rose-200',
               text: `Onboarding failed${run.errorCode ? ` · ${run.errorCode}` : ''}` };
    case 'cancelled':
      return { icon: <XCircle className="h-3 w-3" />, tone: 'text-white/70', text: 'Onboarding cancelled' };
    case 'queued':
      return { icon: <Loader2 className="h-3 w-3 animate-spin" />, tone: 'text-white/70',
               text: 'Onboarding queued' };
    default:
      return { icon: <Loader2 className="h-3 w-3 animate-spin" />, tone: 'text-white/70',
               text: 'Onboarding the candidate...' };
  }
}

export default function OnboardingStatus({ runId }: { runId: string }) {
  const [run, setRun] = useState<Run | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let polls = 0;

    const poll = async () => {
      try {
        const next = await api.getRun(runId);
        if (cancelled) return;
        setRun(next);
        // Finished, or asked long enough: stop. Either way the last state stays on screen.
        if (RUN_FINISHED.includes(next.status) || ++polls >= MAX_POLLS) return;
      } catch {
        // The run is gone, or the API is unreachable. Say nothing more rather than retrying in a loop the
        // user did not ask for; the upload itself already succeeded.
        if (!cancelled) setUnavailable(true);
        return;
      }
      timer = setTimeout(poll, POLL_MS);
    };
    void poll();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [runId]);

  if (unavailable || !run) return null;
  const { icon, text, tone } = label(run);
  const flagged = run.status === 'completed' && run.resultSummary.flagged;
  return (
    <div className="mb-1 text-[11px] opacity-90">
      <span className={`flex items-center gap-1 ${tone}`}>
        {icon} {text}
      </span>
      {flagged && (
        <span className="mt-0.5 flex items-center gap-1 text-amber-200">
          <ShieldAlert className="h-3 w-3" /> This document contains text that tries to instruct the AI; it is
          recorded with the candidate.
        </span>
      )}
      {run.status === 'failed' && run.errorSummary && (
        <span className="mt-0.5 flex items-center gap-1 text-white/70">
          <AlertTriangle className="h-3 w-3" /> {run.errorSummary}
        </span>
      )}
    </div>
  );
}
