'use client';

import React from 'react';
import type { Requirement, Screening } from '../lib/types';

export const scoreClass = (score: number) =>
  score >= 75
    ? 'text-green-400 bg-green-950/80 border-green-800'
    : score >= 60
    ? 'text-amber-300 bg-amber-950/60 border-amber-800'
    : 'text-red-400 bg-red-950/60 border-red-800';

const GRADE: Record<Requirement['grade'], string> = {
  strong: 'bg-green-900/60 text-green-300',
  moderate: 'bg-emerald-900/40 text-emerald-300',
  claimed: 'bg-sky-900/40 text-sky-300',
  adjacent: 'bg-amber-900/50 text-amber-300',
  invalidated: 'bg-red-900/50 text-red-300',
  absent: 'bg-red-950/70 text-red-400',
};

export default function ScreeningCard({ s }: { s: Screening }) {
  const trace = s.agentTrace || {};
  return (
    <div className="mt-3 space-y-4 rounded-xl border border-slate-800 bg-slate-950/60 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded border px-2.5 py-1 text-xl font-extrabold ${scoreClass(s.score)}`}>{s.score}/100</span>
        <span className="font-semibold">{s.band}</span>
        <span className="text-slate-400">· {s.recommendation}</span>
        <span className="ml-auto text-xs text-slate-500">
          {s.roleTitle}{s.sector ? ` · ${s.sector}` : ''}{s.totalYears != null ? ` · ${s.totalYears} yrs` : ''}
          {s.requiredYears ? ` (needs ${s.requiredYears})` : ''}
        </span>
      </div>
      {s.integrityNote && <p className="text-xs text-amber-400">Integrity note: {s.integrityNote}</p>}

      <table className="w-full text-left text-xs">
        <thead className="text-slate-500">
          <tr><th className="py-1">Requirement (from JD)</th><th>Grade</th><th>Evidence in resume</th></tr>
        </thead>
        <tbody>
          {s.requirements.map((r, i) => (
            <tr key={i} className="border-t border-slate-800/70 align-top">
              <td className="py-1.5 pr-2">
                <span className="text-slate-200">{r.name}</span>
                <span className={`ml-1 text-[10px] ${r.importance === 'must' ? 'text-red-300' : 'text-slate-500'}`}>
                  {r.importance === 'must' ? 'must' : 'nice'}
                </span>
                <div className="text-[10px] text-slate-500" title="Quoted from the JD">“{r.jdQuote}”</div>
              </td>
              <td className="py-1.5 pr-2"><span className={`rounded px-1.5 py-0.5 ${GRADE[r.grade]}`}>{r.grade}</span></td>
              <td className="py-1.5 text-slate-400">
                {r.matchedSkills.length ? <span className="text-slate-300">{r.matchedSkills.join(', ')}</span> : '-'}
                {r.evidence && <div className="text-[11px]">{r.evidence}</div>}
                {r.years != null && <div className="text-[11px]">{r.years} yrs</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {s.questions.length > 0 && (
        <details>
          <summary className="cursor-pointer font-semibold text-slate-200">Interview questions ({s.questions.length})</summary>
          <ol className="mt-2 list-decimal space-y-2 pl-5 text-xs text-slate-300">
            {s.questions.map((q, i) => (
              <li key={i}>
                <span className="text-blue-300">[{q.skill}]</span> {q.question}
                <div className="text-slate-400">{q.expectedAnswer}</div>
                {q.redFlag && <div className="text-red-300/80">Red flag: {q.redFlag}</div>}
              </li>
            ))}
          </ol>
        </details>
      )}

      <details className="text-xs text-slate-400">
        <summary className="cursor-pointer">How this result was produced</summary>
        <div className="mt-2 space-y-1">
          <p>Skill: <code>{trace.skill || s.skill}</code> · model: <code>{s.model}</code></p>
          <p>Steps the agent chose to run: {(trace.stepsByAgent || []).join(' → ') || 'none'}</p>
          {!!trace.stepsFallback?.length && <p className="text-amber-400">Steps run as fallback: {trace.stepsFallback.join(', ')}</p>}
          <p>Score breakdown: {Object.entries(s.breakdown).filter(([, v]) => v !== null && v !== '').map(([k, v]) => `${k} ${v}`).join(' · ')}</p>
          {!!trace.corrections?.length && (
            <div>
              <p>Corrections by the grounding checks (claims not supported by the JD/resume text):</p>
              <ul className="list-disc pl-5">{trace.corrections.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </div>
          )}
        </div>
      </details>
    </div>
  );
}
