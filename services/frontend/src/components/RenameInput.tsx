'use client';

import React, { useEffect, useRef, useState } from 'react';

/** Inline title editor: Enter saves, Escape cancels, blur saves. */
export default function RenameInput({ value, onSave, onCancel, className = '' }: {
  value: string;
  onSave: (title: string) => void;
  onCancel: () => void;
  className?: string;
}) {
  const [text, setText] = useState(value);
  const ref = useRef<HTMLInputElement>(null);
  const done = useRef(false);

  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);

  const finish = (save: boolean) => {
    if (done.current) return;
    done.current = true;
    const title = text.trim();
    if (save && title && title !== value) onSave(title);
    else onCancel();
  };

  return (
    <input
      ref={ref}
      aria-label="Chat title"
      maxLength={200}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === 'Enter') finish(true);
        if (e.key === 'Escape') finish(false);
      }}
      onBlur={() => finish(true)}
      className={`w-full rounded-md border border-blue-500 bg-slate-950 px-2 py-0.5 text-sm focus:outline-none ${className}`}
    />
  );
}
