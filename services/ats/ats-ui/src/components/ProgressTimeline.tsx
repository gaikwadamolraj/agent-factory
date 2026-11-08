'use client';

import React from 'react';

export interface Step {
  stage: string;
  message: string;
  at: number; // seconds since the turn started
}

const LABELS: Record<string, string> = {
  pdf: 'Read the PDF',
  index: 'Index the resume (embeddings)',
  queued: 'Waiting for a free model slot',
  thinking: 'Assistant is deciding what to do',
  tool_call: 'Assistant chose a tool',
  'agent:start': 'Sent to the screening agent (MCP)',
  'agent:agent': 'Agent activated its skill',
  'agent:analyze_job_description': 'Agent: analyse the job description',
  'agent:analyze_candidate_profile': 'Agent: analyse the resume',
  'agent:match_requirements': 'Agent: match requirements and score',
  'agent:write_verdict': 'Agent: write summary and questions',
  'agent:done': 'Agent finished',
  agent_cache: 'Reused an identical earlier screening',
  'tool:details': 'Reading the latest screening',
  'tool:memory': "Asking the agent's memory",
  responding: 'Assistant is writing the answer',
};

export default function ProgressTimeline({ steps, progress, elapsed }: { steps: Step[]; progress: number; elapsed: number }) {
  // One row per stage (latest message wins), in first-seen order.
  const rows: Step[] = [];
  steps.forEach((s) => {
    const existing = rows.find((r) => r.stage === s.stage);
    if (existing) existing.message = s.message;
    else rows.push({ ...s });
  });
  return (
    <div className="w-full space-y-3">
      <div className="flex justify-between text-xs text-slate-400">
        <span>Working...</span>
        <span>{Math.floor(elapsed)}s</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
        <div className="h-1.5 rounded-full bg-blue-500 transition-all" style={{ width: `${Math.max(3, Math.min(100, progress))}%` }} />
      </div>
      <ul className="space-y-1.5 text-xs">
        {rows.map((r, i) => {
          const active = i === rows.length - 1;
          return (
            <li key={r.stage} className="flex gap-2">
              <span className="mt-0.5">
                {active ? (
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
                ) : '✅'}
              </span>
              <div>
                <span className={active ? 'font-semibold text-blue-300' : 'text-slate-300'}>{LABELS[r.stage] || r.stage}</span>
                <div className="text-[11px] text-slate-500">{r.message} <span className="text-slate-600">· {r.at.toFixed(0)}s</span></div>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
