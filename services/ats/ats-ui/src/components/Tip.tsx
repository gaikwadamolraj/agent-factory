'use client';

// Tooltip shown on hover and keyboard focus. Rendered with fixed positioning so it is never clipped by
// scrolling containers (sidebar list, chat panel).
import React, { useRef, useState } from 'react';

export default function Tip({ label, children, side = 'top' }: { label: string; children: React.ReactNode; side?: 'top' | 'bottom' }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (r) setPos({ x: r.left + r.width / 2, y: side === 'top' ? r.top - 6 : r.bottom + 6 });
  };
  const hide = () => setPos(null);

  return (
    <span ref={ref} className="inline-flex" onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      {children}
      {pos && (
        <span
          role="tooltip"
          style={{ left: pos.x, top: pos.y, transform: `translate(-50%, ${side === 'top' ? '-100%' : '0'})` }}
          className="pointer-events-none fixed z-50 max-w-xs whitespace-nowrap rounded-md border border-slate-700 bg-slate-800 px-2 py-1 text-[11px] text-slate-100 shadow-lg"
        >
          {label}
        </span>
      )}
    </span>
  );
}
