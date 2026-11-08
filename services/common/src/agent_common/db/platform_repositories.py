"""Repositories for the shared platform-admin tables. Like the tenancy repositories, these never commit; the caller's
unit of work does. Every read is scoped to a client so a query can never cross a tenant.
"""
import uuid
from typing import Optional, Sequence

from sqlalchemy import and_, func, or_, text
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.db.platform_tables import (AccessPolicy, Group, GroupMember, Notification, Skill, SkillApproval,
                                           MatchResult, Subject, SubjectRelation, SubjectVersion, WorkflowRun,
                                           SkillVersion, SkillVersionFile, UserInvite)
from agent_common.db.repositories import BaseRepository
from agent_common.db.tables import utcnow


class SkillRepository(BaseRepository[Skill]):
    model = Skill

    async def get_by_name(self, client_id: int, domain: str, name: str) -> Optional[Skill]:
        statement = select(Skill).where(Skill.client_id == client_id, Skill.domain == domain, Skill.name == name,
                                        col(Skill.is_deleted).is_(False))
        return (await self.session.exec(statement)).first()

    async def list_for_client(self, client_id: int) -> Sequence[Skill]:
        statement = (select(Skill).where(Skill.client_id == client_id, col(Skill.is_deleted).is_(False))
                     .order_by(col(Skill.domain), col(Skill.name)))
        return (await self.session.exec(statement)).all()


class SkillVersionRepository(BaseRepository[SkillVersion]):
    model = SkillVersion

    async def list_for_skill(self, client_id: int, skill_id: int) -> Sequence[SkillVersion]:
        statement = (select(SkillVersion)
                     .where(SkillVersion.client_id == client_id, SkillVersion.skill_id == skill_id,
                            col(SkillVersion.is_deleted).is_(False))
                     .order_by(col(SkillVersion.version_number)))
        return (await self.session.exec(statement)).all()

    async def latest_published(self, client_id: int, skill_id: int) -> Optional[SkillVersion]:
        """The highest-numbered published version (the default a run with no version pin resolves to)."""
        statement = (select(SkillVersion)
                     .where(SkillVersion.client_id == client_id, SkillVersion.skill_id == skill_id,
                            SkillVersion.state == "published", col(SkillVersion.is_deleted).is_(False))
                     .order_by(col(SkillVersion.version_number).desc()))
        return (await self.session.exec(statement)).first()


class SkillVersionFileRepository(BaseRepository[SkillVersionFile]):
    model = SkillVersionFile

    async def list_for_version(self, client_id: int, skill_version_id: int) -> Sequence[SkillVersionFile]:
        statement = (select(SkillVersionFile)
                     .where(SkillVersionFile.client_id == client_id,
                            SkillVersionFile.skill_version_id == skill_version_id,
                            col(SkillVersionFile.is_deleted).is_(False))
                     .order_by(col(SkillVersionFile.path)))
        return (await self.session.exec(statement)).all()


class SkillApprovalRepository(BaseRepository[SkillApproval]):
    model = SkillApproval

    async def list_for_version(self, client_id: int, skill_version_id: int) -> Sequence[SkillApproval]:
        statement = (select(SkillApproval)
                     .where(SkillApproval.client_id == client_id,
                            SkillApproval.skill_version_id == skill_version_id)
                     .order_by(col(SkillApproval.created_at)))
        return (await self.session.exec(statement)).all()


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    async def list_for_user(self, client_id: int, recipient_user_id: int, unread_only: bool = False,
                            limit: int = 50) -> Sequence[Notification]:
        """A user's notifications, unread first then newest first (the UI's notifications tab)."""
        statement = select(Notification).where(Notification.client_id == client_id,
                                               Notification.recipient_user_id == recipient_user_id,
                                               col(Notification.is_deleted).is_(False))
        if unread_only:
            statement = statement.where(col(Notification.is_read).is_(False))
        statement = statement.order_by(col(Notification.is_read), col(Notification.created_at).desc()).limit(limit)
        return (await self.session.exec(statement)).all()

    async def mark_read(self, notification: Notification) -> None:
        notification.is_read = True
        self.session.add(notification)


class GroupRepository(BaseRepository[Group]):
    model = Group

    async def list_for_client(self, client_id: int) -> Sequence[Group]:
        statement = (select(Group).where(Group.client_id == client_id, col(Group.is_deleted).is_(False))
                     .order_by(col(Group.name)))
        return (await self.session.exec(statement)).all()


class GroupMemberRepository(BaseRepository[GroupMember]):
    model = GroupMember

    async def group_ids_for_user(self, client_id: int, user_id: int) -> frozenset[int]:
        """The ids of the groups a user belongs to (used to build a policy Subject)."""
        statement = select(GroupMember.group_id).where(GroupMember.client_id == client_id,
                                                       GroupMember.user_id == user_id,
                                                       col(GroupMember.is_deleted).is_(False))
        return frozenset((await self.session.exec(statement)).all())


class UserInviteRepository(BaseRepository[UserInvite]):
    model = UserInvite

    async def get_by_token_hash(self, token_hash: str) -> Optional[UserInvite]:
        statement = select(UserInvite).where(UserInvite.token_hash == token_hash)
        return (await self.session.exec(statement)).first()

    async def list_pending(self, client_id: int) -> Sequence[UserInvite]:
        statement = (select(UserInvite).where(UserInvite.client_id == client_id, UserInvite.status == "pending",
                                              col(UserInvite.is_deleted).is_(False))
                     .order_by(col(UserInvite.created_at).desc()))
        return (await self.session.exec(statement)).all()


class AccessPolicyRepository(BaseRepository[AccessPolicy]):
    model = AccessPolicy

    async def list_for_principal(self, client_id: int, principal_type: str, principal_id: int) -> Sequence[AccessPolicy]:
        statement = (select(AccessPolicy)
                     .where(AccessPolicy.client_id == client_id, AccessPolicy.principal_type == principal_type,
                            AccessPolicy.principal_id == principal_id, col(AccessPolicy.is_deleted).is_(False))
                     .order_by(col(AccessPolicy.created_at)))
        return (await self.session.exec(statement)).all()

    async def effective_for(self, subject) -> list:
        """Every enabled policy document attached to the subject and to each group/project/client it belongs to.

        Returns a list of agent_common.policy.PolicyDocument (parsed from the stored JSONB), ready for the evaluator.
        """
        # Imported here to avoid a hard dependency of the db layer on the policy package.
        from agent_common.policy.models import PolicyDocument

        # One query over all of the subject's principals: itself, its groups, its projects and its client.
        principal_filters = [and_(AccessPolicy.principal_type == "user", AccessPolicy.principal_id == subject.user_id),
                             and_(AccessPolicy.principal_type == "client",
                                  AccessPolicy.principal_id == subject.client_id)]
        if subject.group_ids:
            principal_filters.append(and_(AccessPolicy.principal_type == "group",
                                          col(AccessPolicy.principal_id).in_(list(subject.group_ids))))
        if subject.project_ids:
            principal_filters.append(and_(AccessPolicy.principal_type == "project",
                                          col(AccessPolicy.principal_id).in_(list(subject.project_ids))))
        statement = select(AccessPolicy).where(AccessPolicy.client_id == subject.client_id,
                                               col(AccessPolicy.enabled).is_(True),
                                               col(AccessPolicy.is_deleted).is_(False),
                                               or_(*principal_filters))
        rows = (await self.session.exec(statement)).all()
        return [PolicyDocument.model_validate(row.document) for row in rows]


class SubjectRepository(BaseRepository[Subject]):
    """Subjects are project-scoped domain records, so every read takes client + project (not client alone)."""
    model = Subject

    async def get_by_ref(self, client_id: int, project_id: int, domain: str, subject_type: str,
                         external_ref: str) -> Optional[Subject]:
        """One subject by its natural key - the same key the knowledge graph's `subject_ref` is built from."""
        statement = select(Subject).where(Subject.client_id == client_id, Subject.project_id == project_id,
                                          Subject.domain == domain, Subject.subject_type == subject_type,
                                          Subject.external_ref == external_ref,
                                          col(Subject.is_deleted).is_(False))
        return (await self.session.exec(statement)).first()

    async def list_for_type(self, client_id: int, project_id: int, domain: str, subject_type: str,
                            status: Optional[str] = None, limit: int = 100,
                            offset: int = 0) -> Sequence[Subject]:
        """A page of one subject type, newest first. Bounded: bulk journeys page, they never load a whole project."""
        conditions = [Subject.client_id == client_id, Subject.project_id == project_id, Subject.domain == domain,
                      Subject.subject_type == subject_type, col(Subject.is_deleted).is_(False)]
        if status is not None:
            conditions.append(Subject.status == status)
        statement = (select(Subject).where(*conditions)
                     .order_by(col(Subject.created_at).desc(), col(Subject.id).desc())
                     .offset(max(offset, 0)).limit(max(1, min(limit, 500))))
        return (await self.session.exec(statement)).all()

    async def by_ids(self, client_id: int, project_id: int, subject_ids: Sequence[int]) -> dict[int, Subject]:
        """The named subjects of this project, as {id: subject}. Ids that do not belong to it are absent.

        One query, because the caller is usually building a page of results and fetching each subject in a
        loop is the N+1 that makes a shortlist of five hundred take five hundred round trips.
        """
        wanted = [int(value) for value in subject_ids]
        if not wanted:
            return {}
        statement = select(Subject).where(Subject.client_id == client_id,
                                          Subject.project_id == project_id,
                                          col(Subject.id).in_(wanted),
                                          col(Subject.is_deleted).is_(False))
        return {int(subject.id): subject for subject in (await self.session.exec(statement)).all()}

    async def page_for_type(self, client_id: int, project_id: int, domain: str, subject_type: str,
                            status: Optional[str] = None, limit: int = 100,
                            after_id: Optional[int] = None) -> Sequence[Subject]:
        """A page of one subject type by ascending id - the pagination a bulk journey must use.

        Keyset, not OFFSET. A bulk run can take an hour, and OFFSET over a table someone is still inserting
        into silently skips rows and repeats others: candidates would go ungraded with nothing to show it.
        Ordering by id means a page is defined by where the last one ended, whatever else has changed.
        """
        conditions = [Subject.client_id == client_id, Subject.project_id == project_id,
                      Subject.domain == domain, Subject.subject_type == subject_type,
                      col(Subject.is_deleted).is_(False)]
        if status is not None:
            conditions.append(Subject.status == status)
        if after_id is not None:
            conditions.append(col(Subject.id) > after_id)
        statement = (select(Subject).where(*conditions)
                     .order_by(col(Subject.id))
                     .limit(max(1, min(limit, 500))))
        return (await self.session.exec(statement)).all()

    async def count_for_type(self, client_id: int, project_id: int, domain: str, subject_type: str,
                             status: Optional[str] = None) -> int:
        """How many subjects of a type exist - the total a paged bulk run reports progress against."""
        conditions = [Subject.client_id == client_id, Subject.project_id == project_id, Subject.domain == domain,
                      Subject.subject_type == subject_type, col(Subject.is_deleted).is_(False)]
        if status is not None:
            conditions.append(Subject.status == status)
        statement = select(func.count()).select_from(Subject).where(*conditions)
        return int((await self.session.exec(statement)).one())


class SubjectVersionRepository(BaseRepository[SubjectVersion]):
    model = SubjectVersion

    async def next_version_number(self, client_id: int, subject_id: int) -> int:
        """The next dense version number for a subject (1 for the first snapshot)."""
        statement = (select(func.max(SubjectVersion.version_number))
                     .where(SubjectVersion.client_id == client_id, SubjectVersion.subject_id == subject_id))
        return int((await self.session.exec(statement)).one() or 0) + 1

    async def list_for_subject(self, client_id: int, subject_id: int, limit: int = 50) -> Sequence[SubjectVersion]:
        """Snapshots newest first; soft-deleted rows are kept, because history is evidence."""
        statement = (select(SubjectVersion)
                     .where(SubjectVersion.client_id == client_id, SubjectVersion.subject_id == subject_id)
                     .order_by(col(SubjectVersion.version_number).desc())
                     .limit(max(1, min(limit, 500))))
        return (await self.session.exec(statement)).all()


class SubjectRelationRepository(BaseRepository[SubjectRelation]):
    model = SubjectRelation

    async def list_from(self, client_id: int, source_subject_id: int,
                        relation_type: Optional[str] = None) -> Sequence[SubjectRelation]:
        conditions = [SubjectRelation.client_id == client_id,
                      SubjectRelation.source_subject_id == source_subject_id,
                      col(SubjectRelation.is_deleted).is_(False)]
        if relation_type is not None:
            conditions.append(SubjectRelation.relation_type == relation_type)
        return (await self.session.exec(select(SubjectRelation).where(*conditions))).all()

    async def list_to(self, client_id: int, target_subject_id: int,
                      relation_type: Optional[str] = None) -> Sequence[SubjectRelation]:
        conditions = [SubjectRelation.client_id == client_id,
                      SubjectRelation.target_subject_id == target_subject_id,
                      col(SubjectRelation.is_deleted).is_(False)]
        if relation_type is not None:
            conditions.append(SubjectRelation.relation_type == relation_type)
        return (await self.session.exec(select(SubjectRelation).where(*conditions))).all()


class WorkflowRunRepository(BaseRepository[WorkflowRun]):
    """Runs are project-scoped, like the subjects they are about."""
    model = WorkflowRun

    async def get_by_idempotency_key(self, client_id: int, idempotency_key: str) -> Optional[WorkflowRun]:
        """The existing run for this business key, if any - the check that makes a re-submit idempotent."""
        statement = select(WorkflowRun).where(WorkflowRun.client_id == client_id,
                                              WorkflowRun.idempotency_key == idempotency_key,
                                              col(WorkflowRun.is_deleted).is_(False))
        return (await self.session.exec(statement)).first()

    async def get_by_external_id(self, client_id: int, project_id: int,
                                 external_id: str) -> Optional[WorkflowRun]:
        """One run by the id an API exposes, scoped to the caller's project.

        The scope is part of the lookup rather than a check afterwards: a run of another client or project is
        simply not found, which is what stops a guessed id telling anyone it exists (CLAUDE.md section 7).
        """
        try:
            run_uuid = uuid.UUID(str(external_id))
        except (ValueError, AttributeError, TypeError):
            return None     # not an id this table can hold: not found, not a server error
        statement = select(WorkflowRun).where(WorkflowRun.client_id == client_id,
                                              WorkflowRun.project_id == project_id,
                                              WorkflowRun.external_id == run_uuid,
                                              col(WorkflowRun.is_deleted).is_(False))
        return (await self.session.exec(statement)).first()

    async def get_by_workflow_id(self, client_id: int, temporal_workflow_id: str) -> Optional[WorkflowRun]:
        statement = (select(WorkflowRun)
                     .where(WorkflowRun.client_id == client_id,
                            WorkflowRun.temporal_workflow_id == temporal_workflow_id,
                            col(WorkflowRun.is_deleted).is_(False))
                     .order_by(col(WorkflowRun.id).desc()))
        return (await self.session.exec(statement)).first()

    async def list_for_project(self, client_id: int, project_id: int, domain: Optional[str] = None,
                               journey: Optional[str] = None, status: Optional[str] = None,
                               limit: int = 50, offset: int = 0) -> Sequence[WorkflowRun]:
        """A page of runs, newest first - the dashboard query. Bounded so a project cannot be loaded whole."""
        conditions = [WorkflowRun.client_id == client_id, WorkflowRun.project_id == project_id,
                      col(WorkflowRun.is_deleted).is_(False)]
        if domain is not None:
            conditions.append(WorkflowRun.domain == domain)
        if journey is not None:
            conditions.append(WorkflowRun.journey == journey)
        if status is not None:
            conditions.append(WorkflowRun.status == status)
        statement = (select(WorkflowRun).where(*conditions)
                     .order_by(col(WorkflowRun.created_at).desc(), col(WorkflowRun.id).desc())
                     .offset(max(offset, 0)).limit(max(1, min(limit, 200))))
        return (await self.session.exec(statement)).all()

    async def list_for_subject(self, client_id: int, subject_id: int, limit: int = 50) -> Sequence[WorkflowRun]:
        """Every run about one subject - what the J4 "ask about this subject" journey reads for provenance."""
        statement = (select(WorkflowRun)
                     .where(WorkflowRun.client_id == client_id, WorkflowRun.subject_id == subject_id,
                            col(WorkflowRun.is_deleted).is_(False))
                     .order_by(col(WorkflowRun.created_at).desc())
                     .limit(max(1, min(limit, 200))))
        return (await self.session.exec(statement)).all()


class MatchResultRepository(BaseRepository[MatchResult]):
    """Per-subject grades of a match run. Every read is scoped to a client, like the rest of this module."""
    model = MatchResult

    async def upsert(self, result: MatchResult) -> MatchResult:
        """Write one subject's grade, replacing the previous attempt for the same run and subject.

        The uniqueness of `(client_id, run_id, subject_id)` is what makes a retried activity idempotent: a
        worker that crashed after grading but before acknowledging updates this row instead of paying an LLM
        to grade the same candidate twice. Returns the stored row.
        """
        existing = await self.for_subject(result.client_id, result.run_id, result.subject_id)
        if existing is None:
            self.session.add(result)
            await self.session.flush()
            return result
        for field in ("domain", "criteria_key", "criteria_ref", "rubric_key", "rubric_version", "score",
                      "band", "recommendation", "grades", "breakdown", "error_code", "error_summary"):
            setattr(existing, field, getattr(result, field))
        existing.updated_at = utcnow()
        await self.session.flush()
        return existing

    async def for_subject(self, client_id: int, run_id: int, subject_id: int) -> Optional[MatchResult]:
        statement = select(MatchResult).where(MatchResult.client_id == client_id,
                                              MatchResult.run_id == run_id,
                                              MatchResult.subject_id == subject_id,
                                              col(MatchResult.is_deleted).is_(False))
        return (await self.session.exec(statement)).first()

    async def ranked(self, client_id: int, run_id: int, limit: int = 50,
                     offset: int = 0) -> Sequence[MatchResult]:
        """A page of a run's results, best score first - the shortlist query.

        Rows that failed to grade carry a NULL score and sort last, so a failure never displaces a real
        result from the top of a shortlist.
        """
        statement = (select(MatchResult)
                     .where(MatchResult.client_id == client_id, MatchResult.run_id == run_id,
                            col(MatchResult.is_deleted).is_(False))
                     .order_by(col(MatchResult.score).desc().nullslast(), col(MatchResult.id))
                     .offset(max(offset, 0)).limit(max(1, min(limit, 500))))
        return (await self.session.exec(statement)).all()

    async def counts(self, client_id: int, run_id: int) -> dict[str, int]:
        """How many subjects this run graded, and how many it could not. Both matter to the caller."""
        statement = (select(func.count(), func.count(MatchResult.error_code))
                     .where(MatchResult.client_id == client_id, MatchResult.run_id == run_id,
                            col(MatchResult.is_deleted).is_(False)))
        total, failed = (await self.session.exec(statement)).one()
        return {"graded": int(total) - int(failed), "failed": int(failed), "total": int(total)}

    async def assign_ranks(self, client_id: int, run_id: int) -> int:
        """Number the run's results by score, best first. Returns how many rows were numbered.

        Done in one statement rather than row by row: a bulk run can hold thousands of results, and ranking
        them with a round trip each would take longer than the grading did.
        """
        statement = text("""
            UPDATE match_results AS target
               SET rank = ranked.position, updated_at = now()
              FROM (SELECT id, row_number() OVER (ORDER BY score DESC NULLS LAST, id) AS position
                      FROM match_results
                     WHERE client_id = :client_id AND run_id = :run_id AND is_deleted = false) AS ranked
             WHERE target.id = ranked.id
        """).bindparams(client_id=client_id, run_id=run_id)
        result = await self.session.exec(statement)
        return int(result.rowcount or 0)
