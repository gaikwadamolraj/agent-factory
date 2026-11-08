"""The caller's own notifications (self-scoped: no IAM policy needed, scoped strictly to the actor).

These tools read and update only the acting user's rows (recipient_user_id = actor), so there is no cross-user
exposure and the pipeline marks them self-scoped (skips the policy engine).
"""
from agent_common.db import NotificationRepository
from agent_common.db.platform_tables import Notification
from agent_common.errors import AppError, Errors

from registry_service.authorization import ToolContext
from registry_service.operations._common import get_by_external_id


def _notification_dict(n: Notification) -> dict:
    return {"id": str(n.external_id), "type": n.type, "title": n.title, "body": n.body, "link": n.link,
            "isRead": n.is_read, "actorUserId": n.actor_user_id, "createdAt": n.created_at.isoformat()}


async def list_notifications(ctx: ToolContext, req) -> dict:
    """The actor's notifications, unread first then newest first."""
    rows = await NotificationRepository(ctx.session).list_for_user(
        ctx.client_id, ctx.actor_id, unread_only=req.unread_only, limit=req.limit)
    unread = sum(1 for n in rows if not n.is_read)
    return {"items": [_notification_dict(n) for n in rows], "total": len(rows), "unread": unread}


async def mark_notification_read(ctx: ToolContext, req) -> dict:
    """Mark one of the actor's notifications read. 404 when it is not the actor's own notification."""
    notification = await get_by_external_id(ctx.session, Notification, req.notification_id, ctx.client_id)
    if notification is None or notification.recipient_user_id != ctx.actor_id:
        raise AppError(Errors.NOT_FOUND)
    await NotificationRepository(ctx.session).mark_read(notification)
    return {"id": str(notification.external_id), "isRead": True}
