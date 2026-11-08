"""Shared notifications: write an in-app notification row, and optionally deliver it out of app.

    from agent_common.notifications import notify, LogNotifier

    await notify(session, client_id=claims.client_id, recipient_user_id=approver_id,
                 type="skill.submitted", title="A skill version needs review",
                 body="ats/screening v3 was submitted for approval.",
                 link="/admin/skills/ats/screening", actor_user_id=claims.user_id,
                 notifier=LogNotifier())

`notify` always writes the DB row (the UI's in-app notification) inside the caller's unit of work, then best-effort
calls the notifier for out-of-app delivery. Invites reuse the same notifier, so the invite link works today
(log/in-app) and becomes email later with no caller change (docs/impl_agent_factory.md sections 2 and 6b).

The Notification table and its repository live in agent_common.db (shared tenant tables).
"""
from typing import Optional

from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.db.platform_repositories import NotificationRepository
from agent_common.db.platform_tables import Notification
from agent_common.logging import get_logger, log_event
from agent_common.notifications.notifier import EmailNotifier, LogNotifier, NullNotifier, Notifier

log = get_logger(__name__)


async def notify(session: AsyncSession, *, client_id: int, recipient_user_id: int, type: str, title: str,
                 body: str = "", link: Optional[str] = None, actor_user_id: Optional[int] = None,
                 project_id: Optional[int] = None, notifier: Optional[Notifier] = None,
                 recipient_email: Optional[str] = None) -> Notification:
    """Write a notification row for `recipient_user_id` and, if a notifier is given, deliver it out of app.

    The row is created inside the caller's unit of work (not committed here). Out-of-app delivery is best-effort: a
    notifier error is logged, never raised, so it cannot roll back the in-app notification. Returns the row.
    """
    notification = Notification(client_id=client_id, project_id=project_id, recipient_user_id=recipient_user_id,
                                type=type, title=title, body=body, link=link, actor_user_id=actor_user_id,
                                created_by=actor_user_id)
    await NotificationRepository(session).add(notification)
    if notifier is not None:
        try:
            await notifier.deliver(recipient_user_id, type, title, body, link, email=recipient_email)
        except Exception:   # noqa: BLE001 - delivery is best-effort; the in-app row already succeeded
            log_event(log, "notification.delivery_failed", recipient_user_id=recipient_user_id, type=type)
    return notification


__all__ = ["EmailNotifier", "LogNotifier", "Notification", "NotificationRepository", "Notifier", "NullNotifier",
           "notify"]
