"""Skills, versions, their lifecycle, approvals and notifications (docs/impl_registry_service.md sections 1-2).

Lifecycle: draft -> (submit) -> in_review -> (approve) -> published -> (archive) -> archived; reject sends it back
to draft. Published versions are immutable (CLAUDE.md section 10): edits are refused; branch a new draft instead.
Activating a published version sets `skills.active_version_id` and bumps the skill-resolution cache version so every
replica resolves the new default at once. A draft is runnable only by its author and an explicit tester allow-list,
expressed as IAM policies so the one policy engine decides who may run it.
"""
import hashlib
from collections.abc import Sequence
from typing import Optional

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.authz import bump_policy_version, bump_skill_version
from agent_common.db import (AccessPolicyRepository, SkillApprovalRepository, SkillRepository,
                           SkillVersionFileRepository, SkillVersionRepository)
from agent_common.db.platform_tables import (AccessPolicy, Skill, SkillApproval, SkillVersion, SkillVersionFile)
from agent_common.db.repositories import UserRepository
from agent_common.errors import AppError
from agent_common.notifications import notify
from agent_common.pack import PACK_FILE, parse_pack, validate_pack
from agent_common.policy.matcher import matches_any

from registry_service import audit
from registry_service.authorization import ToolContext
from registry_service.constants import STATE_ARCHIVED, STATE_DRAFT, STATE_IN_REVIEW, STATE_PUBLISHED
from registry_service.errors import FactoryErrors
from registry_service.operations._common import get_by_external_id

APPROVE_ACTION = "skill.approve"
DRAFT_TESTER_PREFIX = "draft-tester:"       # name tag of the per-tester run-permission policies for a draft version


def skill_resource(skill: Skill) -> str:
    """The IAM resource string for a skill, e.g. 'skill:ats/screening'."""
    return f"skill:{skill.domain}/{skill.name}"


def _skill_dict(skill: Skill) -> dict:
    return {"id": str(skill.external_id), "domain": skill.domain, "name": skill.name,
            "description": skill.description, "activeVersionId": skill.active_version_id,
            "resource": skill_resource(skill)}


def _version_dict(version: SkillVersion, files: Optional[Sequence[SkillVersionFile]] = None) -> dict:
    data = {"id": str(version.external_id), "skillId": version.skill_id, "versionNumber": version.version_number,
            "state": version.state, "authorId": version.author_id, "rejectionReason": version.rejection_reason}
    if files is not None:
        data["files"] = [{"path": f.path, "contentHash": f.content_hash} for f in files]
        data["workflow"] = version.workflow
    return data


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _load_version(ctx: ToolContext, external_id) -> SkillVersion:
    version = await get_by_external_id(ctx.session, SkillVersion, external_id, ctx.client_id)
    if version is None:
        raise AppError(FactoryErrors.VERSION_NOT_FOUND)
    return version


async def _load_skill(ctx: ToolContext, external_id) -> Skill:
    skill = await get_by_external_id(ctx.session, Skill, external_id, ctx.client_id)
    if skill is None:
        raise AppError(FactoryErrors.SKILL_NOT_FOUND)
    return skill


# --------------------------------------------------------------------------------- skills
async def create_skill(ctx: ToolContext, req) -> dict:
    """Create a new skill (no versions yet). Fails with SCR-CNF-0001 when domain+name already exists."""
    repo = SkillRepository(ctx.session)
    if await repo.get_by_name(ctx.client_id, req.domain, req.name) is not None:
        raise AppError(FactoryErrors.SKILL_EXISTS)
    skill = Skill(client_id=ctx.client_id, domain=req.domain, name=req.name, description=req.description)
    audit.stamp_created(skill, ctx.actor_id)
    await repo.add(skill)
    audit.record("create_skill", actor_id=ctx.actor_id, client_id=ctx.client_id, target=skill_resource(skill),
                 after={"domain": skill.domain, "name": skill.name})
    return _skill_dict(skill)


async def list_skills(ctx: ToolContext, req) -> dict:
    """Every skill of the caller's client."""
    skills = await SkillRepository(ctx.session).list_for_client(ctx.client_id)
    return {"items": [_skill_dict(s) for s in skills], "total": len(skills)}


async def get_skill(ctx: ToolContext, req) -> dict:
    """One skill with its versions (states only; use get_skill_version for a version's files)."""
    skill = await _load_skill(ctx, req.skill_id)
    versions = await SkillVersionRepository(ctx.session).list_for_skill(ctx.client_id, skill.id)
    return {**_skill_dict(skill), "versions": [_version_dict(v) for v in versions]}


# --------------------------------------------------------------------------------- versions
async def create_draft_version(ctx: ToolContext, req) -> dict:
    """Branch a new draft version of a skill. Workflow + files come from the request, or are copied from
    `base_version_id` when given (so a new draft starts from a published version)."""
    skill = await _load_skill(ctx, req.skill_id)
    versions = SkillVersionRepository(ctx.session)
    existing = await versions.list_for_skill(ctx.client_id, skill.id)
    next_number = max((v.version_number for v in existing), default=0) + 1

    workflow, files = dict(req.workflow), [(f.path, f.content) for f in req.files]
    if req.base_version_id is not None:
        base = await _load_version(ctx, req.base_version_id)
        if not req.workflow:
            workflow = dict(base.workflow)
        if not req.files:
            base_files = await SkillVersionFileRepository(ctx.session).list_for_version(ctx.client_id, base.id)
            files = [(f.path, f.content) for f in base_files]

    version = SkillVersion(client_id=ctx.client_id, skill_id=skill.id, version_number=next_number,
                           state=STATE_DRAFT, author_id=ctx.actor_id, workflow=workflow)
    audit.stamp_created(version, ctx.actor_id)
    await versions.add(version)
    await _replace_files(ctx.session, ctx.client_id, version.id, files, ctx.actor_id)
    audit.record("create_draft_version", actor_id=ctx.actor_id, client_id=ctx.client_id,
                 target=f"{skill_resource(skill)}:v{next_number}", after={"state": STATE_DRAFT})
    stored = await SkillVersionFileRepository(ctx.session).list_for_version(ctx.client_id, version.id)
    return _version_dict(version, stored)


async def update_draft_version(ctx: ToolContext, req) -> dict:
    """Edit a draft version's workflow and/or files. Only a draft is editable (SCR-CNF-0002)."""
    version = await _load_version(ctx, req.version_id)
    if version.state != STATE_DRAFT:
        raise AppError(FactoryErrors.VERSION_NOT_EDITABLE)
    if req.workflow is not None:
        version.workflow = dict(req.workflow)
    audit.stamp_updated(version, ctx.actor_id)
    ctx.session.add(version)
    if req.files is not None:
        await _replace_files(ctx.session, ctx.client_id, version.id, [(f.path, f.content) for f in req.files],
                             ctx.actor_id)
    audit.record("update_draft_version", actor_id=ctx.actor_id, client_id=ctx.client_id,
                 target=str(version.external_id), after={"state": version.state})
    stored = await SkillVersionFileRepository(ctx.session).list_for_version(ctx.client_id, version.id)
    return _version_dict(version, stored)


async def find_by_natural_key(ctx: ToolContext, domain: str, name: str,
                              version_number: Optional[int]) -> tuple[Skill, Optional[SkillVersion]]:
    """Resolve `domain/name` (+ optional pinned version) to the skill and the version that would run.

    Shared by resolve_skill and its access builder so authorization and execution can never disagree about
    which version is in play. Returns `(skill, None)` when nothing is runnable; the handler turns that into
    SCR-NFD-0004 while the access builder still has a resource to authorize against.
    """
    skill = await SkillRepository(ctx.session).get_by_name(ctx.client_id, domain, name)
    if skill is None:
        raise AppError(FactoryErrors.SKILL_NOT_FOUND)
    versions = await SkillVersionRepository(ctx.session).list_for_skill(ctx.client_id, skill.id)
    if version_number is not None:
        return skill, next((v for v in versions if v.version_number == version_number), None)
    if skill.active_version_id is not None:
        return skill, next((v for v in versions if v.id == skill.active_version_id), None)
    return skill, None


async def resolve_skill(ctx: ToolContext, req) -> dict:
    """Runtime resolution: the content an agent must run for `domain/name`, by natural key.

    This is the bridge between the authoring plane (versions as database rows) and the runtime (which used to
    import Python modules): activating a version in the admin UI is what changes an agent's behaviour.

    Returns the version's workflow and every file with its content, plus a `cacheId`
    (`domain/name@vN:<hash of the content>`) the caller puts in its own cache keys, so a re-activation or an
    edited draft can never serve a stale result. `versionNumber` pins an exact version for reproducing an old
    run; omitted resolves the skill's active version. Only published or archived versions resolve by default -
    a draft is returned only when pinned, and the caller still has to hold run permission for it.

    Raises SCR-NFD-0001 (unknown skill) or SCR-NFD-0003 (no runnable version).
    """
    skill, version = await find_by_natural_key(ctx, req.domain, req.name, req.version_number)
    if version is None:
        raise AppError(FactoryErrors.NO_RUNNABLE_VERSION)

    files = await SkillVersionFileRepository(ctx.session).list_for_version(ctx.client_id, version.id)
    return {
        "skillId": str(skill.external_id),
        "domain": skill.domain,
        "name": skill.name,
        "versionId": str(version.external_id),
        "versionNumber": version.version_number,
        "state": version.state,
        "cacheId": _cache_id(skill, version, files),
        "workflow": version.workflow,
        "files": [{"path": f.path, "content": f.content, "contentHash": f.content_hash} for f in files],
    }


def _cache_id(skill: Skill, version: SkillVersion, files: Sequence[SkillVersionFile]) -> str:
    """`domain/name@vN:<hash>`. The hash covers the workflow and every file, so any content change is a new id.

    Files are sorted by path so the id does not depend on row order.
    """
    digest = hashlib.sha256()
    digest.update(repr(sorted(version.workflow.items())).encode("utf-8"))
    for file in sorted(files, key=lambda f: f.path):
        digest.update(b"\x1f")
        digest.update(file.path.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(file.content_hash.encode("utf-8"))
    return f"{skill.domain}/{skill.name}@v{version.version_number}:{digest.hexdigest()[:10]}"


async def get_skill_version(ctx: ToolContext, req) -> dict:
    """One version with its workflow, files and approval history."""
    version = await _load_version(ctx, req.version_id)
    files = await SkillVersionFileRepository(ctx.session).list_for_version(ctx.client_id, version.id)
    approvals = await SkillApprovalRepository(ctx.session).list_for_version(ctx.client_id, version.id)
    history = [{"actorId": a.actor_id, "fromState": a.from_state, "toState": a.to_state, "comment": a.comment}
               for a in approvals]
    return {**_version_dict(version, files), "history": history}


async def submit_version(ctx: ToolContext, req) -> dict:
    """Submit a draft for review (draft -> in_review) and notify the resolved approvers."""
    version = await _load_version(ctx, req.version_id)
    if version.state != STATE_DRAFT:
        raise AppError(FactoryErrors.INVALID_TRANSITION)
    skill = await _load_skill_by_id(ctx, version.skill_id)
    # Re-validate the pack at the gate to review, not only when files were written: it catches a version
    # authored before pack validation existed, and a pack whose files were edited around the write path.
    stored = await SkillVersionFileRepository(ctx.session).list_for_version(ctx.client_id, version.id)
    validate_pack_files([(file.path, file.content) for file in stored])
    await _transition(ctx, version, STATE_IN_REVIEW, comment=None)
    approvers = await _resolve_approvers(ctx.session, ctx.client_id, skill_resource(skill))
    link = f"/admin/skills/{skill.domain}/{skill.name}/versions/{version.external_id}"
    for approver_id in approvers:
        await notify(ctx.session, client_id=ctx.client_id, recipient_user_id=approver_id, type="skill.submitted",
                     title="A skill version needs review",
                     body=f"{skill.domain}/{skill.name} v{version.version_number} was submitted for approval.",
                     link=link, actor_user_id=ctx.actor_id, notifier=ctx.container.notifier)
    audit.record("submit_version", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(version.external_id),
                 before={"state": STATE_DRAFT}, after={"state": STATE_IN_REVIEW, "approvers": len(approvers)})
    return _version_dict(version)


async def approve_version(ctx: ToolContext, req) -> dict:
    """Approve a version under review (in_review -> published) and notify the author."""
    version = await _load_version(ctx, req.version_id)
    if version.state != STATE_IN_REVIEW:
        raise AppError(FactoryErrors.INVALID_TRANSITION)
    await _transition(ctx, version, STATE_PUBLISHED, comment=req.comment)
    await _notify_author(ctx, version, "skill.approved", "Your skill version was approved",
                         "It is now published and can be activated as the default.")
    audit.record("approve_version", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(version.external_id),
                 before={"state": STATE_IN_REVIEW}, after={"state": STATE_PUBLISHED})
    return _version_dict(version)


async def reject_version(ctx: ToolContext, req) -> dict:
    """Reject a version under review (in_review -> draft) with a reason and notify the author."""
    version = await _load_version(ctx, req.version_id)
    if version.state != STATE_IN_REVIEW:
        raise AppError(FactoryErrors.INVALID_TRANSITION)
    version.rejection_reason = req.reason
    await _transition(ctx, version, STATE_DRAFT, comment=req.reason, from_override=STATE_IN_REVIEW)
    await _notify_author(ctx, version, "skill.rejected", "Your skill version was sent back",
                         f"Reason: {req.reason}")
    audit.record("reject_version", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(version.external_id),
                 before={"state": STATE_IN_REVIEW}, after={"state": STATE_DRAFT})
    return _version_dict(version)


async def activate_version(ctx: ToolContext, req) -> dict:
    """Make a published version the skill's active default (skills.active_version_id) and bump the skill cache."""
    version = await _load_version(ctx, req.version_id)
    if version.state != STATE_PUBLISHED:
        raise AppError(FactoryErrors.NO_PUBLISHED_VERSION)
    skill = await _load_skill_by_id(ctx, version.skill_id)
    previous = skill.active_version_id
    skill.active_version_id = version.id
    audit.stamp_updated(skill, ctx.actor_id)
    ctx.session.add(skill)
    await ctx.session.flush()
    # Commit the DB change, then bump the cache version so no replica serves the old default under the new version.
    await ctx.session.commit()
    await bump_skill_version(ctx.container.redis, ctx.container.env)
    audit.record("activate_version", actor_id=ctx.actor_id, client_id=ctx.client_id, target=skill_resource(skill),
                 before={"activeVersionId": previous}, after={"activeVersionId": version.id})
    return {**_skill_dict(skill), "activatedVersionId": str(version.external_id)}


async def archive_version(ctx: ToolContext, req) -> dict:
    """Archive a version (published/draft -> archived). If it was the active default, clear it and bump the cache."""
    version = await _load_version(ctx, req.version_id)
    if version.state == STATE_ARCHIVED:
        raise AppError(FactoryErrors.INVALID_TRANSITION)
    skill = await _load_skill_by_id(ctx, version.skill_id)
    was_active = skill.active_version_id == version.id
    await _transition(ctx, version, STATE_ARCHIVED, comment=None, from_override=version.state)
    if was_active:
        skill.active_version_id = None
        audit.stamp_updated(skill, ctx.actor_id)
        ctx.session.add(skill)
        await ctx.session.commit()
        await bump_skill_version(ctx.container.redis, ctx.container.env)
    audit.record("archive_version", actor_id=ctx.actor_id, client_id=ctx.client_id, target=str(version.external_id),
                 after={"state": STATE_ARCHIVED, "wasActive": was_active})
    return _version_dict(version)


async def set_version_testers(ctx: ToolContext, req) -> dict:
    """Set who may run a draft version besides its author, as per-user IAM run-permission policies.

    Each tester gets a user policy allowing `skill.run` on this exact draft resource; the previous tester policies for
    the version are removed and the policy cache is bumped. The one policy engine then decides who may run the draft.
    """
    version = await _load_version(ctx, req.version_id)
    skill = await _load_skill_by_id(ctx, version.skill_id)
    resource = f"{skill_resource(skill)}:draft:{version.version_number}"
    tag = f"{DRAFT_TESTER_PREFIX}{version.external_id}"
    policies = AccessPolicyRepository(ctx.session)

    # Remove the version's existing tester policies (replace semantics).
    existing = (await ctx.session.exec(
        select(AccessPolicy).where(AccessPolicy.client_id == ctx.client_id, AccessPolicy.name == tag,
                                   col(AccessPolicy.is_deleted).is_(False)))).all()
    for policy in existing:
        audit.stamp_deleted(policy, ctx.actor_id, reason="testers replaced")
        ctx.session.add(policy)

    document = {"statements": [{"effect": "allow", "actions": ["skill.run"], "resources": [resource]}]}
    for tester_id in dict.fromkeys(req.tester_user_ids):     # de-duplicate, keep order
        policy = AccessPolicy(client_id=ctx.client_id, principal_type="user", principal_id=tester_id,
                              name=tag, document=document, enabled=True)
        audit.stamp_created(policy, ctx.actor_id)
        await policies.add(policy)
    await bump_policy_version(ctx.container.redis, ctx.container.env)
    audit.record("set_version_testers", actor_id=ctx.actor_id, client_id=ctx.client_id,
                 target=str(version.external_id), after={"testers": len(req.tester_user_ids)})
    return {"versionId": str(version.external_id), "resource": resource, "testers": req.tester_user_ids}


# --------------------------------------------------------------------------------- internals
async def _load_skill_by_id(ctx: ToolContext, skill_id: int) -> Skill:
    skill = await ctx.session.get(Skill, skill_id)
    if skill is None or skill.client_id != ctx.client_id:
        raise AppError(FactoryErrors.SKILL_NOT_FOUND)
    return skill


def validate_pack_files(files) -> None:
    """If this version carries a pack.json, it must be a valid pack whose every reference resolves.

    Opt-in by design: a version without a pack.json is an ordinary skill version and is unaffected. A version
    *with* one is a domain pack, and an invalid pack is refused at authoring time rather than failing later on
    a user's request. `files` is a list of (path, content).

    Raises AppError(PCK-INV-0001 / PCK-INV-0002); the caller lets it propagate to the error envelope, where the
    offending paths appear in `fields`.
    """
    contents = {path: content for path, content in files}
    if PACK_FILE not in contents:
        return
    document = parse_pack(contents[PACK_FILE])
    validate_pack(document, available=set(contents))


async def _replace_files(session: AsyncSession, client_id: int, version_id: int, files, actor_id) -> None:
    """Soft-replace a version's files with `files` (list of (path, content)). Simple and bounded: delete + re-add."""
    validate_pack_files(files)
    repo = SkillVersionFileRepository(session)
    for existing in await repo.list_for_version(client_id, version_id):
        await session.delete(existing)
    await session.flush()
    for path, content in files:
        row = SkillVersionFile(client_id=client_id, skill_version_id=version_id, path=path, content=content,
                               content_hash=_content_hash(content))
        audit.stamp_created(row, actor_id)
        await repo.add(row)


async def _transition(ctx: ToolContext, version: SkillVersion, to_state: str, *, comment: Optional[str],
                      from_override: Optional[str] = None) -> None:
    """Record a lifecycle transition on the version and in skill_approvals (who, from/to, comment)."""
    from_state = from_override or version.state
    version.state = to_state
    audit.stamp_updated(version, ctx.actor_id)
    ctx.session.add(version)
    approval = SkillApproval(client_id=ctx.client_id, skill_version_id=version.id, actor_id=ctx.actor_id,
                             from_state=from_state, to_state=to_state, comment=comment)
    audit.stamp_created(approval, ctx.actor_id)
    await SkillApprovalRepository(ctx.session).add(approval)


async def _notify_author(ctx: ToolContext, version: SkillVersion, type_: str, title: str, body: str) -> None:
    await notify(ctx.session, client_id=ctx.client_id, recipient_user_id=version.author_id, type=type_, title=title,
                 body=body, link=f"/admin/skills/versions/{version.external_id}", actor_user_id=ctx.actor_id,
                 notifier=ctx.container.notifier)


async def _resolve_approvers(session: AsyncSession, client_id: int, resource: str, limit: int = 50) -> list[int]:
    """User ids that may approve this skill: every principal of an enabled policy that allows `skill.approve` on the
    resource, expanded (group -> members, project -> members, client -> users), de-duplicated and capped."""
    policies = (await session.exec(
        select(AccessPolicy).where(AccessPolicy.client_id == client_id, col(AccessPolicy.enabled).is_(True),
                                   col(AccessPolicy.is_deleted).is_(False)))).all()
    approver_ids: list[int] = []
    for policy in policies:
        if not _grants_approve(policy.document, resource):
            continue
        approver_ids.extend(await _principal_user_ids(session, client_id, policy.principal_type, policy.principal_id))
        if len(approver_ids) >= limit * 4:       # stop scanning once we clearly have enough to cap
            break
    return list(dict.fromkeys(approver_ids))[:limit]


def _grants_approve(document: dict, resource: str) -> bool:
    """True when a policy document has an allow statement for `skill.approve` on `resource` (conditions ignored - we
    only need a candidate list to notify; the real check happens when they act)."""
    for statement in document.get("statements", []):
        if (statement.get("effect") == "allow" and matches_any(statement.get("actions", []), APPROVE_ACTION)
                and matches_any(statement.get("resources", []), resource)):
            return True
    return False


async def _principal_user_ids(session: AsyncSession, client_id: int, principal_type: str,
                              principal_id: int) -> list[int]:
    """Expand a policy principal to concrete user ids (bounded)."""
    from agent_common.db.platform_tables import GroupMember
    from agent_common.db.tables import ProjectMember

    if principal_type == "user":
        return [principal_id]
    if principal_type == "group":
        rows = await session.exec(select(GroupMember.user_id).where(GroupMember.client_id == client_id,
                                                                    GroupMember.group_id == principal_id,
                                                                    col(GroupMember.is_deleted).is_(False)))
        return list(rows.all())
    if principal_type == "project":
        rows = await session.exec(select(ProjectMember.user_id).where(ProjectMember.project_id == principal_id))
        return list(rows.all())
    if principal_type == "client":
        return [u.id for u in await UserRepository(session).list_for_client(client_id)][:200]
    return []
