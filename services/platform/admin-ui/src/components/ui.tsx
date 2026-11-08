'use client';
// Small, dependency-free UI primitives shared by every admin tab. Dark theme, Tailwind only.
import { ReactNode } from 'react';
import { ApiError } from '../lib/api';

export function Button({
  children,
  onClick,
  type = 'button',
  variant = 'default',
  disabled,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: 'button' | 'submit';
  variant?: 'default' | 'primary' | 'danger' | 'ghost';
  disabled?: boolean;
  title?: string;
}) {
  const styles: Record<string, string> = {
    default: 'bg-slate-700 hover:bg-slate-600 text-slate-100',
    primary: 'bg-indigo-600 hover:bg-indigo-500 text-white',
    danger: 'bg-red-700 hover:bg-red-600 text-white',
    ghost: 'bg-transparent hover:bg-slate-800 text-slate-300 border border-slate-700',
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`rounded px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${styles[variant]}`}
    >
      {children}
    </button>
  );
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-lg border border-slate-800 bg-slate-900/60 p-4 ${className}`}>{children}</div>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-slate-400">{label}</span>
      {children}
    </label>
  );
}

const inputClass =
  'w-full rounded border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-indigo-500';

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`${inputClass} ${props.className || ''}`} />;
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={`${inputClass} ${props.className || ''}`} />;
}

export function TextArea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={`${inputClass} font-mono text-xs ${props.className || ''}`} />;
}

const BADGE_COLORS: Record<string, string> = {
  draft: 'bg-slate-700 text-slate-200',
  in_review: 'bg-amber-600/80 text-amber-50',
  published: 'bg-emerald-700 text-emerald-50',
  archived: 'bg-slate-800 text-slate-400 line-through',
};

export function StateBadge({ state }: { state: string }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-semibold ${BADGE_COLORS[state] || 'bg-slate-700 text-slate-200'}`}>
      {state}
    </span>
  );
}

/** Renders an API error (code + message + hint + field issues) in a consistent banner. */
export function ErrorBanner({ error, onClose }: { error: unknown; onClose?: () => void }) {
  if (!error) return null;
  const e = error as ApiError;
  const code = e?.body?.code || 'ERROR';
  const message = e?.body?.message || (error instanceof Error ? error.message : String(error));
  const hint = e?.body?.hint;
  const fields = e?.body?.fields;
  return (
    <div className="mb-3 rounded border border-red-800 bg-red-950/50 px-3 py-2 text-sm text-red-200">
      <div className="flex items-start justify-between gap-3">
        <div>
          <span className="font-mono text-xs text-red-400">{code}</span> {message}
          {hint ? <span className="text-red-300"> {hint}</span> : null}
          {fields?.length ? (
            <ul className="mt-1 list-disc pl-5 text-xs text-red-300">
              {fields.map((f, i) => (
                <li key={i}>
                  <span className="font-mono">{f.field}</span>: {f.issue}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
        {onClose ? (
          <button onClick={onClose} className="text-red-400 hover:text-red-200">
            x
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-slate-500">{children}</p>;
}

/** A copy-to-clipboard row for links the backend returns (e.g. an invite link). */
export function CopyLink({ value }: { value: string }) {
  return (
    <div className="flex items-center gap-2">
      <code className="flex-1 overflow-x-auto rounded bg-slate-950 px-2 py-1 text-xs text-indigo-300">{value}</code>
      <Button variant="ghost" onClick={() => void navigator.clipboard?.writeText(value)}>
        Copy
      </Button>
    </div>
  );
}
