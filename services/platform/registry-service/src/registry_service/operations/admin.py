"""Delegated administration (docs/impl_registry_service.md section 6b): projects, clients, super-admin grants, and the
identity operations delegated to the auth-service.

registry-service owns clients, projects, groups, policies and skills. Identity writes (create user, set role,
deactivate, invite) are delegated to the auth-service over the service-token contract (identity_client). Guardrails:
client-admin work is forced to the caller's own client by RLS and the resolved tenant context; only a super admin
creates clients, lists all clients, assumes another client or grants super admin.
"""
from sqlmodel import col, select

from agent_common.authz import bump_policy_version
from agent_common.db.platform_tables import AccessPolicy
from agent_common.db.repositories import ClientRepository, MembershipRepository
from agent_common.db.tables import Client, Project, ProjectMember
from agent_common.errors import AppError
from agent_common.notifications import notify

from registry_service import audit
from registry_service.authorization import ToolContext
from registry_service.constants import SUPER_ADMIN_ACTION, SUPER_ADMIN_RESOURCE
from registry_service.errors import FactoryErrors


def _project_dict(project: Project) -> dict:
    return {"id": project.id, "slug": project.slug, "name": project.name, "description": project.description,
            "isActive": project.is_active}


def _client_dict(client: Client) -> dict:
    return {"id": client.id, "slug": client.slug, "name": client.name, "isActive": client.is_active}


# --------------------------------------------------------------------------------- projects (own client)
async def list_projects(ctx: ToolContext, req) -> dict:
    """Every project of the caller's (or the assumed) client."""
    rows = (await ctx.session.exec(select(Project).where(Project.client_id == ctx.client_id)
                                   .order_by(col(Project.name)))).all()
    return {"items": [_project_dict(p) for p in rows], "total": len(rows)}


async def create_project(ctx: ToolContext, req) -> dict:
    """Create a project in the caller's client."""
    project = Project(client_id=ctx.client_id, slug=req.slug, name=req.name, description=req.description)
    ctx.session.add(project)
    await ctx.session.flush()
    audit.record("create_project", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"project:{project.id}",
                 after={"slug": project.slug})
    return _project_dict(project)


async def update_project(ctx: ToolContext, req) -> dict:
    """Edit a project's name/description or activate/archive it (is_active=false)."""
    project = await _load_project(ctx, req.project_id)
    if req.name is not None:
        project.name = req.name
    if req.description is not None:
        project.description = req.description
    if req.is_active is not None:
        project.is_active = req.is_active
    ctx.session.add(project)
    audit.record("update_project", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"project:{project.id}",
                 after={"isActive": project.is_active})
    return _project_dict(project)


async def add_project_member(ctx: ToolContext, req) -> dict:
    """Add a user to a project of the caller's client (idempotent)."""
    await _load_project(ctx, req.project_id)
    exists = (await ctx.session.exec(select(ProjectMember).where(
        ProjectMember.project_id == req.project_id, ProjectMember.user_id == req.user_id))).first()
    if exists is None:
        await MembershipRepository(ctx.session).add(req.user_id, [req.project_id])
    audit.record("add_project_member", actor_id=ctx.actor_id, client_id=ctx.client_id,
                 target=f"project:{req.project_id}", after={"userId": req.user_id})
    return {"projectId": req.project_id, "userId": req.user_id, "member": True}


async def remove_project_member(ctx: ToolContext, req) -> dict:
    """Remove a user from a project of the caller's client."""
    await _load_project(ctx, req.project_id)
    rows = (await ctx.session.exec(select(ProjectMember).where(
        ProjectMember.project_id == req.project_id, ProjectMember.user_id == req.user_id))).all()
    for row in rows:
        await ctx.session.delete(row)
    audit.record("remove_project_member", actor_id=ctx.actor_id, client_id=ctx.client_id,
                 target=f"project:{req.project_id}", after={"userId": req.user_id})
    return {"projectId": req.project_id, "userId": req.user_id, "member": False}


# --------------------------------------------------------------------------------- clients & super admin (platform)
async def list_clients(ctx: ToolContext, req) -> dict:
    """Every client on the platform (super admin only; the cross-tenant read)."""
    rows = (await ctx.session.exec(select(Client).order_by(col(Client.name)))).all()
    return {"items": [_client_dict(c) for c in rows], "total": len(rows)}


async def create_client(ctx: ToolContext, req) -> dict:
    """Create a new client/tenant (super admin only)."""
    client = Client(slug=req.slug, name=req.name)
    ctx.session.add(client)
    await ctx.session.flush()
    audit.record("create_client", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"client:{client.id}",
                 after={"slug": client.slug})
    return _client_dict(client)


async def assume_client(ctx: ToolContext, req) -> dict:
    """Super admin chooses a client to act in. Validates it exists and records the (audited) choice; subsequent calls
    carry X-Client-Id = that client, which the dispatcher allows only for a super admin."""
    client = await ClientRepository(ctx.session).get(req.client_id)
    if client is None:
        raise AppError(FactoryErrors.CROSS_CLIENT_FORBIDDEN)
    audit.record("assume_client", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"client:{req.client_id}")
    return {"assumedClientId": client.id, "slug": client.slug, "name": client.name}


async def grant_super_admin(ctx: ToolContext, req) -> dict:
    """Grant the super-admin bundle to a user (super admin only). Written in the target user's own client, then the
    user's authz version is bumped so it applies at once."""
    from agent_common.db.repositories import UserRepository

    user = await UserRepository(ctx.session).get(req.user_id)
    if user is None:
        raise AppError(FactoryErrors.CROSS_CLIENT_FORBIDDEN)
    document = {"statements": [{"effect": "allow", "actions": [SUPER_ADMIN_ACTION],
                                "resources": [SUPER_ADMIN_RESOURCE]}]}
    # The policy row must be in the target user's client (RLS WITH CHECK), so use a session scoped to that client.
    async with ctx.container.db.tenant_session(user.client_id) as session:
        policy = AccessPolicy(client_id=user.client_id, principal_type="user", principal_id=user.id,
                              name="super-admin", document=document, enabled=True)
        audit.stamp_created(policy, ctx.actor_id)
        session.add(policy)
        await session.flush()
    await bump_policy_version(ctx.container.redis, ctx.container.env)
    await ctx.container.authz.bump_user(user.id)
    audit.record("grant_super_admin", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"user:{user.id}")
    return {"userId": user.id, "superAdmin": True}


# --------------------------------------------------------------------------------- delegated identity (auth-service)
async def invite_user(ctx: ToolContext, req) -> dict:
    """Invite an email into the caller's client (auth-service creates the pending user + signed invite token). The
    invite link is returned and an in-app notification is written to the inviter."""
    result = await ctx.container.identity.create_invite(
        client_id=ctx.client_id, email=req.email, role=req.role, group_ids=req.group_ids,
        project_ids=req.project_ids, invited_by=ctx.actor_id)
    link = result.get("inviteLink")
    await notify(ctx.session, client_id=ctx.client_id, recipient_user_id=ctx.actor_id, type="user.invited",
                 title="Invitation created", body=f"An invite for {req.email} is ready to share.", link=link,
                 actor_user_id=ctx.actor_id, notifier=ctx.container.notifier)
    audit.record("invite_user", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"invite:{req.email}",
                 after={"role": req.role})
    return result


async def create_user(ctx: ToolContext, req) -> dict:
    """Create a user in the caller's client (delegated to the auth-service)."""
    result = await ctx.container.identity.create_user(
        client_id=ctx.client_id, username=req.username, email=req.email, role=req.role)
    audit.record("create_user", actor_id=ctx.actor_id, client_id=ctx.client_id,
                 target=f"user:{result.get('id')}", after={"role": req.role})
    return result


async def set_user_role(ctx: ToolContext, req) -> dict:
    """Change a user's role (delegated to the auth-service)."""
    result = await ctx.container.identity.set_role(client_id=ctx.client_id, user_id=req.user_id, role=req.role)
    audit.record("set_user_role", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"user:{req.user_id}",
                 after={"role": req.role})
    return result


async def deactivate_user(ctx: ToolContext, req) -> dict:
    """Deactivate a user (delegated to the auth-service)."""
    result = await ctx.container.identity.deactivate_user(client_id=ctx.client_id, user_id=req.user_id)
    audit.record("deactivate_user", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"user:{req.user_id}")
    return result


async def list_invites(ctx: ToolContext, req) -> dict:
    """Pending invites of the caller's client (delegated to the auth-service)."""
    return await ctx.container.identity.list_invites(client_id=ctx.client_id)


async def revoke_invite(ctx: ToolContext, req) -> dict:
    """Revoke a pending invite (delegated to the auth-service)."""
    result = await ctx.container.identity.revoke_invite(client_id=ctx.client_id, invite_id=str(req.invite_id))
    audit.record("revoke_invite", actor_id=ctx.actor_id, client_id=ctx.client_id, target=f"invite:{req.invite_id}")
    return result


async def _load_project(ctx: ToolContext, project_id: int) -> Project:
    project = await ctx.session.get(Project, project_id)
    if project is None or project.client_id != ctx.client_id:
        raise AppError(FactoryErrors.PROJECT_NOT_FOUND)
    return project
