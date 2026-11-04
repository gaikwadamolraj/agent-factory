// Tiny, safe markdown renderer (bold + bullet lists + paragraphs). Builds React elements; never injects HTML,
// because assistant text is derived from untrusted resumes and JDs.
import React from 'react';

const inline = (text: string) =>
  text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith('**') && part.endsWith('**') ? <strong key={i} className="text-slate-50">{part.slice(2, -2)}</strong> : part
  );

export default function Markdown({ text }: { text: string }) {
  const blocks: React.ReactNode[] = [];
  let bullets: string[] = [];
  const flush = () => {
    if (bullets.length) {
      blocks.push(
        <ul key={`ul${blocks.length}`} className="list-disc pl-5 space-y-0.5">
          {bullets.map((b, i) => <li key={i}>{inline(b)}</li>)}
        </ul>
      );
      bullets = [];
    }
  };
  text.split('\n').forEach((line) => {
    const m = line.match(/^\s*[-*•]\s+(.*)$/);
    if (m) return void bullets.push(m[1]);
    flush();
    if (line.trim()) blocks.push(<p key={`p${blocks.length}`}>{inline(line)}</p>);
  });
  flush();
  return <div className="space-y-2 text-sm leading-relaxed">{blocks}</div>;
}
