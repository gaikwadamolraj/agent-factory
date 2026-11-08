'use client';
// Skills & versions: list skills, branch/edit drafts, view a version's workflow JSON + files + history, diff two
// versions, and drive the lifecycle (submit -> approve/reject -> activate -> archive). Each button maps to one
// registry-service tool; the server enforces permission, so a denied action surfaces as an error banner.
import { useCallback, useEffect, useState } from 'react';
import { factory } from '../lib/api';
import type { ListResult, Skill, SkillDetail, SkillVersion } from '../lib/types';
import { Button, Card, Empty, ErrorBanner, Field, Input, StateBadge, TextArea } from './ui';

export default function SkillsTab() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selected, setSelected] = useState<SkillDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [newSkill, setNewSkill] = useState({ domain: '', name: '', description: '' });

  const loadSkills = useCallback(async () => {
    try {
      const res = await factory<ListResult<Skill>>('list_skills');
      setSkills(res.items || []);
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    void loadSkills();
  }, [loadSkills]);

  async function openSkill(id: string) {
    setError(null);
    try {
      setSelected(await factory<SkillDetail>('get_skill', { skillId: id }));
    } catch (e) {
      setError(e);
    }
  }

  async function createSkill(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await factory('create_skill', newSkill);
      setNewSkill({ domain: '', name: '', description: '' });
      await loadSkills();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[320px_1fr]">
      <div className="space-y-4">
        <Card>
          <h2 className="mb-2 text-sm font-semibold text-slate-200">Skills</h2>
          <ErrorBanner error={error} onClose={() => setError(null)} />
          {skills.length === 0 ? (
            <Empty>No skills yet.</Empty>
          ) : (
            <ul className="space-y-1">
              {skills.map((s) => (
                <li key={s.id}>
                  <button
                    onClick={() => void openSkill(s.id)}
                    className={`w-full rounded px-2 py-1.5 text-left text-sm hover:bg-slate-800 ${
                      selected?.id === s.id ? 'bg-slate-800 text-indigo-300' : 'text-slate-300'
                    }`}
                  >
                    <span className="font-mono">{s.domain}/{s.name}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card>
          <h2 className="mb-2 text-sm font-semibold text-slate-200">New skill</h2>
          <form onSubmit={createSkill} className="space-y-2">
            <Field label="Domain">
              <Input
                value={newSkill.domain}
                onChange={(e) => setNewSkill({ ...newSkill, domain: e.target.value })}
                placeholder="ats"
              />
            </Field>
            <Field label="Name">
              <Input
                value={newSkill.name}
                onChange={(e) => setNewSkill({ ...newSkill, name: e.target.value })}
                placeholder="screening"
              />
            </Field>
            <Field label="Description">
              <Input
                value={newSkill.description}
                onChange={(e) => setNewSkill({ ...newSkill, description: e.target.value })}
              />
            </Field>
            <Button type="submit" variant="primary" disabled={!newSkill.domain || !newSkill.name}>
              Create skill
            </Button>
          </form>
        </Card>
      </div>

      <div>
        {selected ? (
          <SkillDetailPanel skill={selected} onChanged={() => void openSkill(selected.id)} setError={setError} />
        ) : (
          <Card>
            <Empty>Select a skill to see its versions.</Empty>
          </Card>
        )}
      </div>
    </div>
  );
}

function SkillDetailPanel({
  skill,
  onChanged,
  setError,
}: {
  skill: SkillDetail;
  onChanged: () => void;
  setError: (e: unknown) => void;
}) {
  const [openVersion, setOpenVersion] = useState<SkillVersion | null>(null);
  const [diffPair, setDiffPair] = useState<[string, string]>(['', '']);

  async function act(tool: string, body: Record<string, unknown>) {
    setError(null);
    try {
      await factory(tool, body);
      onChanged();
    } catch (e) {
      setError(e);
    }
  }

  async function newDraft() {
    // Branch a fresh draft (empty workflow; the editor fills it in). The backend assigns the version number.
    await act('create_draft_version', { skillId: skill.id, workflow: {}, files: [] });
  }

  async function viewVersion(id: string) {
    setError(null);
    try {
      setOpenVersion(await factory<SkillVersion>('get_skill_version', { versionId: id }));
    } catch (e) {
      setError(e);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="mb-3 flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-100">
              {skill.domain}/{skill.name}
            </h2>
            {skill.description ? <p className="text-sm text-slate-400">{skill.description}</p> : null}
          </div>
          <Button variant="primary" onClick={() => void newDraft()}>
            New draft version
          </Button>
        </div>

        {skill.versions.length === 0 ? (
          <Empty>No versions yet. Branch a draft to start.</Empty>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase text-slate-500">
                <th className="py-1">Version</th>
                <th>State</th>
                <th>Active</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {skill.versions.map((v) => (
                <tr key={v.id} className="border-t border-slate-800">
                  <td className="py-2 font-mono">v{v.versionNumber}</td>
                  <td>
                    <StateBadge state={v.state} />
                  </td>
                  <td>{skill.activeVersionId === v.skillId ? 'default' : ''}</td>
                  <td className="py-1 text-right">
                    <div className="flex flex-wrap justify-end gap-1">
                      <Button variant="ghost" onClick={() => void viewVersion(v.id)}>
                        View
                      </Button>
                      {v.state === 'draft' && (
                        <Button onClick={() => void act('submit_version', { versionId: v.id })}>Submit</Button>
                      )}
                      {v.state === 'in_review' && (
                        <>
                          <Button
                            variant="primary"
                            onClick={() => void act('approve_version', { versionId: v.id })}
                          >
                            Approve
                          </Button>
                          <Button
                            variant="danger"
                            onClick={() => {
                              const reason = window.prompt('Reason for rejection?') || '';
                              if (reason) void act('reject_version', { versionId: v.id, reason });
                            }}
                          >
                            Reject
                          </Button>
                        </>
                      )}
                      {v.state === 'published' && (
                        <Button onClick={() => void act('activate_version', { versionId: v.id })}>Activate</Button>
                      )}
                      {v.state !== 'archived' && (
                        <Button variant="ghost" onClick={() => void act('archive_version', { versionId: v.id })}>
                          Archive
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <DiffPanel skill={skill} pair={diffPair} setPair={setDiffPair} setError={setError} />

      {openVersion ? (
        <VersionEditor
          version={openVersion}
          onClose={() => setOpenVersion(null)}
          onSaved={() => {
            setOpenVersion(null);
            onChanged();
          }}
          setError={setError}
        />
      ) : null}
    </div>
  );
}

/** View a version and, for a draft, edit its workflow JSON + tester allow-list. */
function VersionEditor({
  version,
  onClose,
  onSaved,
  setError,
}: {
  version: SkillVersion;
  onClose: () => void;
  onSaved: () => void;
  setError: (e: unknown) => void;
}) {
  const editable = version.state === 'draft';
  const [workflow, setWorkflow] = useState(JSON.stringify(version.workflow ?? {}, null, 2));
  const [testers, setTesters] = useState('');

  async function saveWorkflow() {
    setError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(workflow);
    } catch {
      setError(new Error('Workflow is not valid JSON.'));
      return;
    }
    try {
      await factory('update_draft_version', { versionId: version.id, workflow: parsed });
      onSaved();
    } catch (e) {
      setError(e);
    }
  }

  async function saveTesters() {
    setError(null);
    const ids = testers
      .split(',')
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isInteger(n) && n > 0);
    try {
      await factory('set_version_testers', { versionId: version.id, testerUserIds: ids });
      onSaved();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-200">
          Version v{version.versionNumber} <StateBadge state={version.state} />
        </h3>
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      {version.rejectionReason ? (
        <p className="mb-2 text-sm text-amber-300">Rejected: {version.rejectionReason}</p>
      ) : null}
      <Field label={editable ? 'Workflow JSON (editable draft)' : 'Workflow JSON (read-only)'}>
        <TextArea rows={12} value={workflow} readOnly={!editable} onChange={(e) => setWorkflow(e.target.value)} />
      </Field>
      {version.files?.length ? (
        <div className="mt-2 text-xs text-slate-400">
          <p className="mb-1 font-semibold">Files</p>
          <ul className="space-y-0.5">
            {version.files.map((f) => (
              <li key={f.path} className="font-mono">
                {f.path} <span className="text-slate-600">{f.contentHash.slice(0, 12)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {version.history?.length ? (
        <div className="mt-2 text-xs text-slate-400">
          <p className="mb-1 font-semibold">History</p>
          <ul className="space-y-0.5">
            {version.history.map((h, i) => (
              <li key={i}>
                {h.fromState} → {h.toState} {h.comment ? `(${h.comment})` : ''} {h.createdAt?.slice(0, 19)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {editable ? (
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <Button variant="primary" onClick={() => void saveWorkflow()}>
            Save workflow
          </Button>
          <div className="flex items-end gap-2">
            <Field label="Testers (user ids, comma-separated)">
              <Input value={testers} onChange={(e) => setTesters(e.target.value)} placeholder="12, 34" />
            </Field>
            <Button onClick={() => void saveTesters()}>Set testers</Button>
          </div>
        </div>
      ) : null}
    </Card>
  );
}

/** Diff two versions' workflow JSON, fetched on demand, with a naive line-level added/removed marker. */
function DiffPanel({
  skill,
  pair,
  setPair,
  setError,
}: {
  skill: SkillDetail;
  pair: [string, string];
  setPair: (p: [string, string]) => void;
  setError: (e: unknown) => void;
}) {
  const [left, setLeft] = useState<string[]>([]);
  const [right, setRight] = useState<string[]>([]);

  async function run() {
    setError(null);
    if (!pair[0] || !pair[1]) return;
    try {
      const [a, b] = await Promise.all([
        factory<SkillVersion>('get_skill_version', { versionId: pair[0] }),
        factory<SkillVersion>('get_skill_version', { versionId: pair[1] }),
      ]);
      setLeft(JSON.stringify(a.workflow ?? {}, null, 2).split('\n'));
      setRight(JSON.stringify(b.workflow ?? {}, null, 2).split('\n'));
    } catch (e) {
      setError(e);
    }
  }

  const rightSet = new Set(right);
  const leftSet = new Set(left);

  return (
    <Card>
      <h3 className="mb-2 text-sm font-semibold text-slate-200">Diff versions</h3>
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <Field label="From">
          <select
            className="rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm"
            value={pair[0]}
            onChange={(e) => setPair([e.target.value, pair[1]])}
          >
            <option value="">—</option>
            {skill.versions.map((v) => (
              <option key={v.id} value={v.id}>
                v{v.versionNumber} ({v.state})
              </option>
            ))}
          </select>
        </Field>
        <Field label="To">
          <select
            className="rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm"
            value={pair[1]}
            onChange={(e) => setPair([pair[0], e.target.value])}
          >
            <option value="">—</option>
            {skill.versions.map((v) => (
              <option key={v.id} value={v.id}>
                v{v.versionNumber} ({v.state})
              </option>
            ))}
          </select>
        </Field>
        <Button onClick={() => void run()} disabled={!pair[0] || !pair[1]}>
          Compare
        </Button>
      </div>
      {left.length || right.length ? (
        <div className="grid grid-cols-2 gap-2 font-mono text-xs">
          <pre className="overflow-x-auto rounded bg-slate-950 p-2">
            {left.map((l, i) => (
              <div key={i} className={rightSet.has(l) ? 'text-slate-400' : 'bg-red-950/60 text-red-300'}>
                {l || ' '}
              </div>
            ))}
          </pre>
          <pre className="overflow-x-auto rounded bg-slate-950 p-2">
            {right.map((l, i) => (
              <div key={i} className={leftSet.has(l) ? 'text-slate-400' : 'bg-emerald-950/60 text-emerald-300'}>
                {l || ' '}
              </div>
            ))}
          </pre>
        </div>
      ) : null}
    </Card>
  );
}
