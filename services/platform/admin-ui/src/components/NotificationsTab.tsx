'use client';
// Notifications: the convenience GET /api/v1/notifications (unread first) + mark_notification_read. Unread rows are
// highlighted; marking read is one call, then the list reloads.
import { useCallback, useEffect, useState } from 'react';
import { factory } from '../lib/api';
import type { ListResult, Notification } from '../lib/types';
import { Button, Card, Empty, ErrorBanner } from './ui';

export default function NotificationsTab() {
  const [items, setItems] = useState<Notification[]>([]);
  const [unread, setUnread] = useState(0);
  const [error, setError] = useState<unknown>(null);
  const [unreadOnly, setUnreadOnly] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      // list_notifications sorts unread first; it is also exposed as a GET for the UI.
      const res = await factory<ListResult<Notification>>('list_notifications', { unreadOnly, limit: 100 });
      setItems(res.items || []);
      setUnread(res.unread ?? 0);
    } catch (e) {
      setError(e);
    }
  }, [unreadOnly]);

  useEffect(() => {
    void load();
  }, [load]);

  async function markRead(id: string) {
    setError(null);
    try {
      await factory('mark_notification_read', { notificationId: id });
      await load();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">
          Notifications {unread ? <span className="text-amber-400">({unread} unread)</span> : null}
        </h2>
        <label className="flex items-center gap-2 text-xs text-slate-400">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />
          Unread only
        </label>
      </div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      {items.length === 0 ? (
        <Empty>No notifications.</Empty>
      ) : (
        <ul className="space-y-2">
          {items.map((n) => (
            <li
              key={n.id}
              className={`rounded border px-3 py-2 text-sm ${
                n.isRead ? 'border-slate-800 bg-slate-950/40 text-slate-400' : 'border-indigo-800 bg-indigo-950/30 text-slate-100'
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="font-medium">{n.title}</p>
                  <p className="text-xs text-slate-400">{n.body}</p>
                  {n.link ? (
                    <a href={n.link} className="text-xs text-indigo-400 hover:underline">
                      {n.link}
                    </a>
                  ) : null}
                  <p className="mt-0.5 text-[11px] text-slate-600">{n.createdAt?.slice(0, 19)}</p>
                </div>
                {!n.isRead ? (
                  <Button variant="ghost" onClick={() => void markRead(n.id)}>
                    Mark read
                  </Button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
