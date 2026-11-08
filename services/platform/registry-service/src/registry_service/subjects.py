"""Build a policy `Subject` for a user: the memberships and the super-admin flag the IAM evaluator needs.

A subject's effective policies are every enabled policy attached to the subject itself and to each group, project
and client it belongs to (agent_common.db.AccessPolicyRepository.effective_for). We resolve the group and project
memberships here, then recognise a super admin as a subject holding the full-wildcard grant (the seeded super-admin
bundle), so cross-tenant tools can gate on one flag without a second lookup.
"""
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.db import GroupMemberRepository
from agent_common.db.repositories import ProjectRepository
from agent_common.policy import Subject
from agent_common.policy.models import Effect, PolicyDocument

from registry_service.constants import SUPER_ADMIN_ACTION, SUPER_ADMIN_RESOURCE


def _is_super_admin(policies: list[PolicyDocument]) -> bool:
    """True when any effective policy allows every action on every resource (the super-admin bundle), unconditionally."""
    for policy in policies:
        for statement in policy.statements:
            if (statement.effect == Effect.ALLOW and SUPER_ADMIN_ACTION in statement.actions
                    and SUPER_ADMIN_RESOURCE in statement.resources and not statement.conditions):
                return True
    return False


async def build_subject(session: AsyncSession, user_id: int, client_id: int) -> Subject:
    """Assemble the policy Subject for `user_id` in `client_id`: its group and project memberships plus the
    super-admin flag (derived from its effective policies). The session must already be tenant-scoped to the user's
    own client so membership reads cannot cross a tenant."""
    group_ids = await GroupMemberRepository(session).group_ids_for_user(client_id, user_id)
    projects = await ProjectRepository(session).list_for_user(user_id, client_id)
    subject = Subject(user_id=user_id, client_id=client_id, group_ids=group_ids,
                      project_ids=frozenset(p.id for p in projects))
    # Recognise a super admin from the effective policy set (a one-off read; the engine re-reads from its cache).
    from agent_common.db import AccessPolicyRepository

    policies = await AccessPolicyRepository(session).effective_for(subject)
    return Subject(user_id=user_id, client_id=client_id, group_ids=group_ids,
                   project_ids=subject.project_ids, is_super_admin=_is_super_admin(policies))
