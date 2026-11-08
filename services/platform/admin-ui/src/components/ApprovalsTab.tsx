'use client';
// Approvals queue: versions awaiting review. registry-service has no dedicated "list in_review" tool, so we list the
// skills and read each one's versions, keeping those in the in_review state. Approve/reject map to the same tools the
// skills tab uses; the server enforces that the caller actually holds skill.approve.
import { useCallback, useEffect, useState } from 'react';
import { factory } from '../lib/api';
import type { ListResult, Skill, SkillDetail } from '../lib/types';
import { Button, Card, Empty, ErrorBanner } from './ui';

interface Pending {
  skill: string;
  versionId: string;
  versionNumber: number;
}

export default function ApprovalsTab() {
  const [pending, setPending] = useState<Pending[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await factory<ListResult<Skill>>('list_skills');
      const details = await Promise.all((list.items || []).map((s) => factory<SkillDetail>('get_skill', { skillId: s.id })));
      const rows: Pending[] = [];
      for (const d of details) {
        for (const v of d.versions || []) {
          if (v.state === 'in_review') {
            rows.push({ skill: `${d.domain}/${d.name}`, versionId: v.id, versionNumber: v.versionNumber });
          }
        }
      }
      setPending(rows);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(versionId: string, approve: boolean) {
    setError(null);
    try {
      if (approve) {
        const comment = window.prompt('Approval comment (optional)?') || undefined;
        await factory('approve_version', comment ? { versionId, comment } : { versionId });
      } else {
        const reason = window.prompt('Reason for rejection?') || '';
        if (!reason) return;
        await factory('reject_version', { versionId, reason });
      }
      await load();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">Approvals queue</h2>
        <Button variant="ghost" onClick={() => void load()}>
          Refresh
        </Button>
      </div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      {loading ? (
        <Empty>Loading...</Empty>
      ) : pending.length === 0 ? (
        <Empty>Nothing awaiting your review.</Empty>
      ) : (
        <ul className="space-y-2">
          {pending.map((p) => (
            <li
              key={p.versionId}
              className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm"
            >
              <span>
                <span className="font-mono">{p.skill}</span> v{p.versionNumber}
              </span>
              <div className="flex gap-2">
                <Button variant="primary" onClick={() => void decide(p.versionId, true)}>
                  Approve
                </Button>
                <Button variant="danger" onClick={() => void decide(p.versionId, false)}>
                  Reject
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
