"""Group and group-membership administration (docs/impl_registry_service.md section 4).

Groups let a policy be attached once to many users. Every change bumps the policy version, because a subject's
effective policies depend on its group memberships (agent_common.policy.loader).
"""
from sqlmodel import col, select

from agent_common.authz import bump_policy_version
from agent_common.db import GroupRepository
from agent_common.db.platform_tables import Group, GroupMember
from agent_common.errors import AppError

from registry_service import audit
from registry_service.authorization import ToolContext
from registry_service.errors import FactoryErrors
from registry_service.operations._common import get_by_external_id


def _group_dict(group: Group) -> dict:
    return {"id": str(group.external_id), "name": group.name, "description": group.description}


async def list_groups(ctx: ToolContext, req) -> dict:
    """Every group of the caller's client, each with its member user ids."""
    groups = await GroupRepository(ctx.session).list_for_client(ctx.client_id)
    items = []
    for group in groups:
        members = await _member_ids(ctx, group.id)
        items.append({**_group_dict(group), "memberIds": members})
    return {"items": items, "total": len(groups)}


async def create_group(ctx: ToolContext, req) -> dict:
    """Create a group in the caller's client."""
    group = Group(client_id=ctx.client_id, name=req.name, description=req.description)
    audit.stamp_created(group, ctx.actor_id)
    await GroupRepository(ctx.session).add(group)
    await bump_policy_version(ctx.container.redis, ctx.container.env)
    audit.record("create_group", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(group.external_id),
                 after={"name": group.name})
    return _group_dict(group)


async def update_group(ctx: ToolContext, req) -> dict:
    """Rename a group or change its description."""
    group = await get_by_external_id(ctx.session, Group, req.group_id, ctx.client_id)
    if group is None:
        raise AppError(FactoryErrors.GROUP_NOT_FOUND)
    if req.name is not None:
        group.name = req.name
    if req.description is not None:
        group.description = req.description
    audit.stamp_updated(group, ctx.actor_id)
    ctx.session.add(group)
    audit.record("update_group", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(group.external_id))
    return _group_dict(group)


async def delete_group(ctx: ToolContext, req) -> dict:
    """Soft-delete a group and its memberships; bumps the policy version."""
    group = await get_by_external_id(ctx.session, Group, req.group_id, ctx.client_id)
    if group is None:
        raise AppError(FactoryErrors.GROUP_NOT_FOUND)
    audit.stamp_deleted(group, ctx.actor_id, reason="deleted via admin")
    ctx.session.add(group)
    for member in await _members(ctx, group.id):
        audit.stamp_deleted(member, ctx.actor_id, reason="group deleted")
        ctx.session.add(member)
    await bump_policy_version(ctx.container.redis, ctx.container.env)
    audit.record("delete_group", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(group.external_id))
    return {"id": str(group.external_id), "deleted": True}


async def add_group_member(ctx: ToolContext, req) -> dict:
    """Add a user to a group (idempotent); bumps the policy version so the user's effective policies include it."""
    group = await get_by_external_id(ctx.session, Group, req.group_id, ctx.client_id)
    if group is None:
        raise AppError(FactoryErrors.GROUP_NOT_FOUND)
    if not await _is_member(ctx, group.id, req.user_id):
        member = GroupMember(client_id=ctx.client_id, group_id=group.id, user_id=req.user_id)
        audit.stamp_created(member, ctx.actor_id)
        ctx.session.add(member)
        await ctx.session.flush()
        await bump_policy_version(ctx.container.redis, ctx.container.env)
    audit.record("add_group_member", actor_id=ctx.actor_id, client_id=ctx.client_id,
                 target=str(group.external_id), after={"userId": req.user_id})
    return {"groupId": str(group.external_id), "userId": req.user_id, "member": True}


async def remove_group_member(ctx: ToolContext, req) -> dict:
    """Remove a user from a group; bumps the policy version."""
    group = await get_by_external_id(ctx.session, Group, req.group_id, ctx.client_id)
    if group is None:
        raise AppError(FactoryErrors.GROUP_NOT_FOUND)
    rows = (await ctx.session.exec(select(GroupMember).where(
        GroupMember.client_id == ctx.client_id, GroupMember.group_id == group.id,
        GroupMember.user_id == req.user_id, col(GroupMember.is_deleted).is_(False)))).all()
    for member in rows:
        audit.stamp_deleted(member, ctx.actor_id, reason="removed from group")
        ctx.session.add(member)
    if rows:
        await bump_policy_version(ctx.container.redis, ctx.container.env)
    audit.record("remove_group_member", actor_id=ctx.actor_id, client_id=ctx.client_id,
                 target=str(group.external_id), after={"userId": req.user_id})
    return {"groupId": str(group.external_id), "userId": req.user_id, "member": False}


async def _members(ctx: ToolContext, group_id: int):
    rows = await ctx.session.exec(select(GroupMember).where(
        GroupMember.client_id == ctx.client_id, GroupMember.group_id == group_id,
        col(GroupMember.is_deleted).is_(False)))
    return list(rows.all())


async def _member_ids(ctx: ToolContext, group_id: int) -> list[int]:
    return [m.user_id for m in await _members(ctx, group_id)]


async def _is_member(ctx: ToolContext, group_id: int, user_id: int) -> bool:
    return user_id in await _member_ids(ctx, group_id)
