"""The ONE table of registry-service tools: name -> (request model, handler, action+resource, flags).

This is the single source of truth both front doors use (docs/impl_registry_service.md section 3). Adding a tool is one
entry here plus its handler and request model - it then appears on the MCP front door and at `POST
/api/v1/factory/{tool}` with no extra handler code. The `action`/`resource` columns are what the IAM engine evaluates
for a user call (default deny for anything not listed).
"""
from agent_common.policy import AccessRequest

from registry_service.authorization import ToolContext, ToolSpec
from registry_service.constants import STATE_DRAFT
from registry_service.operations import admin, catalogue_ops, groups, notifications, policies, skills
from registry_service.operations import models as m


# --------------------------------------------------------------------------------- access-request builders
def _simple(action: str, resource: str):
    """An access builder with a fixed action + resource, tagged with the caller's client for same_client conditions."""
    async def build(ctx: ToolContext, req) -> AccessRequest:
        return AccessRequest(action=action, resource=resource, resource_client_id=ctx.client_id)
    return build


def _skill_from_skill_id(action: str):
    """Access for a tool whose request carries a skill_id: resolve the skill's resource and author-free attributes."""
    async def build(ctx: ToolContext, req) -> AccessRequest:
        skill = await skills._load_skill(ctx, req.skill_id)
        return AccessRequest(action=action, resource=skills.skill_resource(skill), resource_client_id=ctx.client_id)
    return build


def _skill_from_version_id(action: str):
    """Access for a tool whose request carries a version_id: resolve the owning skill + the draft author (so the
    draft_author / unless_author conditions can compare a verified author id, never one parsed from a string)."""
    async def build(ctx: ToolContext, req) -> AccessRequest:
        version = await skills._load_version(ctx, req.version_id)
        skill = await skills._load_skill_by_id(ctx, version.skill_id)
        return AccessRequest(action=action, resource=skills.skill_resource(skill), resource_client_id=ctx.client_id,
                             resource_author_id=version.author_id)
    return build


def _spec(name, description, model, handler, mutating, action, resource, access, **flags) -> ToolSpec:
    return ToolSpec(name=name, description=description, request_model=model, handler=handler, mutating=mutating,
                    access=access, action=action, resource=resource, **flags)


async def _create_skill_access(ctx: ToolContext, req) -> AccessRequest:
    """create_skill's resource is built from the request (the skill does not exist yet)."""
    return AccessRequest(action="skill.create", resource=f"skill:{req.domain}/{req.name}",
                         resource_client_id=ctx.client_id)


async def _resolve_skill_access(ctx: ToolContext, req) -> AccessRequest:
    """resolve_skill is addressed by natural key, so the resource is derived from the request plus a lookup.

    A pinned *draft* resolves to the draft resource (`skill:<domain>/<name>:draft:<n>`), which is exactly what
    set_version_testers grants, so pinning someone else's draft is refused by the same policy that governs
    running it. A pinned published/archived version keeps the plain skill resource, so reproducing an old run
    needs no extra grant. The author id travels too, so draft_author/unless_author compare a verified id.
    """
    skill, version = await skills.find_by_natural_key(ctx, req.domain, req.name, req.version_number)
    resource = skills.skill_resource(skill)
    if version is not None and version.state == STATE_DRAFT:
        resource = f"{resource}:draft:{version.version_number}"
    return AccessRequest(action="skill.run", resource=resource, resource_client_id=ctx.client_id,
                         resource_author_id=version.author_id if version is not None else None)


# --------------------------------------------------------------------------------- the registry
_SPECS: list[ToolSpec] = [
    # ---- skills & versions ------------------------------------------------------------------------------------
    _spec("create_skill", "Create a new skill (domain + name).", m.CreateSkillRequest, skills.create_skill, True,
          "skill.create", "skill:{domain}/{name}", _create_skill_access),
    _spec("list_skills", "List the skills of your organisation.", m.Empty, skills.list_skills, False,
          "skill.read", "skill:*", _simple("skill.read", "skill:*")),
    _spec("get_skill", "Get one skill with its versions.", m.SkillIdRequest, skills.get_skill, False,
          "skill.read", "skill:{domain}/{name}", _skill_from_skill_id("skill.read")),
    _spec("create_draft_version", "Branch a new draft version of a skill.", m.CreateDraftVersionRequest,
          skills.create_draft_version, True, "skill.edit", "skill:{domain}/{name}",
          _skill_from_skill_id("skill.edit")),
    _spec("update_draft_version", "Edit a draft version's workflow/files.", m.UpdateDraftVersionRequest,
          skills.update_draft_version, True, "skill.edit", "skill:{domain}/{name}",
          _skill_from_version_id("skill.edit")),
    _spec("get_skill_version", "Get a version with its files and history.", m.VersionIdRequest,
          skills.get_skill_version, False, "skill.read", "skill:{domain}/{name}",
          _skill_from_version_id("skill.read")),
    # Runtime resolution: agents call this to load the version they must run (see skills.resolve_skill).
    # Service callers are gated by TOOL_POLICY; a human caller needs skill.run on the skill.
    _spec("resolve_skill", "Resolve the version an agent should run for a skill, with its files.",
          m.ResolveSkillRequest, skills.resolve_skill, False, "skill.run", "skill:{domain}/{name}",
          _resolve_skill_access),
    _spec("submit_version", "Submit a draft for review.", m.VersionIdRequest, skills.submit_version, True,
          "skill.submit", "skill:{domain}/{name}", _skill_from_version_id("skill.submit")),
    _spec("approve_version", "Approve a version under review (publish it).", m.ApproveVersionRequest,
          skills.approve_version, True, "skill.approve", "skill:{domain}/{name}",
          _skill_from_version_id("skill.approve")),
    _spec("reject_version", "Reject a version under review (back to draft).", m.RejectVersionRequest,
          skills.reject_version, True, "skill.approve", "skill:{domain}/{name}",
          _skill_from_version_id("skill.approve")),
    _spec("activate_version", "Make a published version the active default.", m.VersionIdRequest,
          skills.activate_version, True, "skill.activate", "skill:{domain}/{name}",
          _skill_from_version_id("skill.activate")),
    _spec("archive_version", "Archive a version.", m.VersionIdRequest, skills.archive_version, True,
          "skill.archive", "skill:{domain}/{name}", _skill_from_version_id("skill.archive")),
    _spec("set_version_testers", "Set the tester allow-list for a draft version.", m.SetTestersRequest,
          skills.set_version_testers, True, "skill.edit", "skill:{domain}/{name}",
          _skill_from_version_id("skill.edit")),

    # ---- notifications (self-scoped: the caller's own rows, no policy) -----------------------------------------
    _spec("list_notifications", "List your notifications (unread first).", m.ListNotificationsRequest,
          notifications.list_notifications, False, "notification.read", "notification:self", None, self_scoped=True),
    _spec("mark_notification_read", "Mark one of your notifications read.", m.NotificationIdRequest,
          notifications.mark_notification_read, True, "notification.read", "notification:self", None,
          self_scoped=True),

    # ---- policies & groups ------------------------------------------------------------------------------------
    _spec("list_policies", "List access policies.", m.ListPoliciesRequest, policies.list_policies, False,
          "policy.read", "policy:*", _simple("policy.read", "policy:*")),
    _spec("create_policy", "Create an access policy.", m.CreatePolicyRequest, policies.create_policy, True,
          "policy.manage", "policy:*", _simple("policy.manage", "policy:*")),
    _spec("update_policy", "Edit an access policy.", m.UpdatePolicyRequest, policies.update_policy, True,
          "policy.manage", "policy:*", _simple("policy.manage", "policy:*")),
    _spec("delete_policy", "Delete an access policy.", m.PolicyIdRequest, policies.delete_policy, True,
          "policy.manage", "policy:*", _simple("policy.manage", "policy:*")),
    _spec("simulate_policy", "Simulate: can user X do action Y on resource Z?", m.SimulateRequest,
          policies.simulate_policy, False, "policy.read", "policy:*", _simple("policy.read", "policy:*")),
    _spec("list_groups", "List groups and members.", m.Empty, groups.list_groups, False, "group.read", "group:*",
          _simple("group.read", "group:*")),
    _spec("create_group", "Create a group.", m.CreateGroupRequest, groups.create_group, True, "group.manage",
          "group:*", _simple("group.manage", "group:*")),
    _spec("update_group", "Rename/describe a group.", m.UpdateGroupRequest, groups.update_group, True,
          "group.manage", "group:*", _simple("group.manage", "group:*")),
    _spec("delete_group", "Delete a group.", m.GroupIdRequest, groups.delete_group, True, "group.manage",
          "group:*", _simple("group.manage", "group:*")),
    _spec("add_group_member", "Add a user to a group.", m.GroupMemberRequest, groups.add_group_member, True,
          "group.manage", "group:*", _simple("group.manage", "group:*")),
    _spec("remove_group_member", "Remove a user from a group.", m.GroupMemberRequest, groups.remove_group_member,
          True, "group.manage", "group:*", _simple("group.manage", "group:*")),

    # ---- projects (own client) --------------------------------------------------------------------------------
    _spec("list_projects", "List your organisation's projects.", m.Empty, admin.list_projects, False,
          "project.read", "project:*", _simple("project.read", "project:*")),
    _spec("create_project", "Create a project.", m.CreateProjectRequest, admin.create_project, True,
          "project.manage", "project:*", _simple("project.manage", "project:*")),
    _spec("update_project", "Edit/archive a project.", m.UpdateProjectRequest, admin.update_project, True,
          "project.manage", "project:*", _simple("project.manage", "project:*")),
    _spec("add_project_member", "Add a user to a project.", m.ProjectMemberRequest, admin.add_project_member, True,
          "project.manage", "project:*", _simple("project.manage", "project:*")),
    _spec("remove_project_member", "Remove a user from a project.", m.ProjectMemberRequest,
          admin.remove_project_member, True, "project.manage", "project:*", _simple("project.manage", "project:*")),

    # ---- delegated identity (auth-service) --------------------------------------------------------------------
    _spec("invite_user", "Invite an email into your organisation.", m.InviteUserRequest, admin.invite_user, True,
          "admin.users.manage", "user:*", _simple("admin.users.manage", "user:*")),
    _spec("create_user", "Create a user in your organisation.", m.CreateUserRequest, admin.create_user, True,
          "admin.users.manage", "user:*", _simple("admin.users.manage", "user:*")),
    _spec("set_user_role", "Change a user's role.", m.SetUserRoleRequest, admin.set_user_role, True,
          "admin.users.manage", "user:*", _simple("admin.users.manage", "user:*")),
    _spec("deactivate_user", "Deactivate a user.", m.UserIdRequest, admin.deactivate_user, True,
          "admin.users.manage", "user:*", _simple("admin.users.manage", "user:*")),
    _spec("list_invites", "List pending invites.", m.Empty, admin.list_invites, False, "admin.users.manage",
          "user:*", _simple("admin.users.manage", "user:*")),
    _spec("revoke_invite", "Revoke a pending invite.", m.InviteIdRequest, admin.revoke_invite, True,
          "admin.users.manage", "user:*", _simple("admin.users.manage", "user:*")),

    # ---- clients & super admin (platform owner; cross-tenant) -------------------------------------------------
    _spec("list_clients", "List every client on the platform.", m.Empty, admin.list_clients, False,
          "client.manage", "client:*", _simple("client.manage", "client:*"), require_super_admin=True),
    _spec("create_client", "Create a new client/tenant.", m.CreateClientRequest, admin.create_client, True,
          "client.manage", "client:*", _simple("client.manage", "client:*"), require_super_admin=True),
    _spec("assume_client", "Choose a client to act in (super admin).", m.AssumeClientRequest, admin.assume_client,
          True, "client.assume", "client:*", _simple("client.assume", "client:*"), require_super_admin=True),
    _spec("grant_super_admin", "Grant super admin to a user.", m.GrantSuperAdminRequest, admin.grant_super_admin,
          True, "admin.super.grant", "user:*", _simple("admin.super.grant", "user:*"), require_super_admin=True),

    # ---- tool catalogue ---------------------------------------------------------------------------------------
    _spec("list_catalogue", "List registered MCP tool servers.", m.Empty, catalogue_ops.list_catalogue, False,
          "catalogue.read", "catalogue:*", _simple("catalogue.read", "catalogue:*")),
]


REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}


def get_spec(name: str) -> ToolSpec | None:
    """The ToolSpec for a tool name, or None (unknown -> default deny)."""
    return REGISTRY.get(name)


def all_specs() -> list[ToolSpec]:
    return list(_SPECS)
