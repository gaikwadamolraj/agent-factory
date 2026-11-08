"""Shared platform-admin tables: skills and their versions/files/approvals, notifications, groups and members,
access policies, and user invites. Used by agent-factory, ats-orchestrator, auth-service and admin-ui, which is why
they live in agent_common.db (CLAUDE.md section 8: shared tenant tables and repositories live here).

These are NEW tables, so they adopt the platform-wide conventions (docs/impl_agent_factory.md section 5):
* `id` BIGSERIAL internal primary key - for joins only, never exposed on the wire;
* `external_id` UUID - the id the API and UI use;
* an audit mixin (created/updated/deleted actor + time, soft delete via `is_deleted`);
* `client_id` is the tenant isolation key (we have clients -> projects, no separate tenant), enforced by Postgres RLS
  keyed off the `app.client_id` session GUC (see the 0001 migration and agent_common.db.set_tenant_context).

Column types match the 0001 migration exactly: `id` and foreign keys between these new tables are BigInteger
(BIGSERIAL); foreign keys to the existing tenancy tables (clients/users/projects) are Integer, because those tables
use Integer primary keys.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlmodel import Field, SQLModel

from agent_common.db.tables import created_at_field, updated_at_field


def id_field() -> Any:
    """BIGSERIAL internal primary key (joins only, never exposed)."""
    return Field(default=None, sa_type=BigInteger, primary_key=True)


def external_id_field() -> Any:
    """UUID exposed to the API/UI; the database fills it with gen_random_uuid() when not supplied."""
    return Field(default_factory=uuid.uuid4, sa_type=PgUuid(as_uuid=True),
                 sa_column_kwargs={"unique": True}, nullable=False)


def jsonb_field(default_factory) -> Any:
    """A JSONB column with a Python default (e.g. dict or list)."""
    return Field(default_factory=default_factory, sa_type=JSONB, nullable=False)


class AuditMixin(SQLModel):
    """Audit + soft-delete columns shared by every new platform table. `*_by` columns hold the actor's user id but are
    plain integers (not foreign keys), so the audit trail survives even if the actor's user row is later removed."""

    created_at: datetime = created_at_field()
    created_by: Optional[int] = Field(default=None, sa_type=BigInteger)
    updated_at: datetime = updated_at_field()
    updated_by: Optional[int] = Field(default=None, sa_type=BigInteger)
    is_deleted: bool = Field(default=False, nullable=False)
    deleted_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    deleted_by: Optional[int] = Field(default=None, sa_type=BigInteger)
    deleted_reason: Optional[str] = Field(default=None, max_length=500)


class Skill(AuditMixin, table=True):
    """A named, domain-scoped capability (e.g. domain 'ats', name 'screening'). Has many versions; `active_version_id`
    points at the published version that runs by default."""
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("client_id", "domain", "name", name="uq_skills_client_domain_name"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    domain: str = Field(max_length=64)
    name: str = Field(max_length=128)
    description: Optional[str] = Field(default=None, max_length=1000)
    # The published version that a run with no version pin resolves to (set on activate); no FK to avoid a cycle with
    # skill_versions.skill_id (the migration adds the FK after both tables exist).
    active_version_id: Optional[int] = Field(default=None, sa_type=BigInteger, index=True)


class SkillVersion(AuditMixin, table=True):
    """One version of a skill: its workflow definition and lifecycle state. Published versions are immutable
    (CLAUDE.md section 10): a new draft is branched instead of editing a published one."""
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version_number", name="uq_skill_versions_skill_number"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    skill_id: int = Field(sa_type=BigInteger, foreign_key="skills.id", nullable=False, index=True)
    version_number: int = Field(default=1)
    state: str = Field(default="draft", max_length=16)        # draft|in_review|published|archived|rejected
    author_id: int = Field(sa_type=Integer, foreign_key="users.id", nullable=False, index=True)
    workflow: dict = jsonb_field(dict)                        # the skill.yaml workflow definition
    rejection_reason: Optional[str] = Field(default=None, max_length=1000)


class SkillVersionFile(AuditMixin, table=True):
    """One file of a skill version (skill.yaml, a prompt, a schema, a rule file, code/*.py). Content is stored so a
    version can be loaded at runtime without rebuilding an image (docs/impl_agent_platform.md section 2)."""
    __tablename__ = "skill_version_files"
    __table_args__ = (UniqueConstraint("skill_version_id", "path", name="uq_skill_version_files_version_path"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    skill_version_id: int = Field(sa_type=BigInteger, foreign_key="skill_versions.id", nullable=False, index=True)
    path: str = Field(max_length=255)                         # e.g. "prompts/jd_analysis.md"
    content: str = Field(default="")
    content_hash: Optional[str] = Field(default=None, max_length=64)


class SkillApproval(AuditMixin, table=True):
    """One recorded lifecycle transition of a skill version (submit/approve/reject/activate/archive): who, when,
    from/to state and a comment."""
    __tablename__ = "skill_approvals"

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    skill_version_id: int = Field(sa_type=BigInteger, foreign_key="skill_versions.id", nullable=False, index=True)
    actor_id: int = Field(sa_type=Integer, foreign_key="users.id", nullable=False, index=True)
    from_state: str = Field(max_length=16)
    to_state: str = Field(max_length=16)
    comment: Optional[str] = Field(default=None, max_length=2000)


class Notification(AuditMixin, table=True):
    """An in-app notification for one user (the UI reads these). Generic, so every feature reuses it: approvals,
    invites, and anything else. Delivery = this DB row plus an optional out-of-app notifier (agent_common.notifications).
    """
    __tablename__ = "notifications"

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    project_id: Optional[int] = Field(default=None, sa_type=Integer, foreign_key="projects.id")
    recipient_user_id: int = Field(sa_type=Integer, foreign_key="users.id", nullable=False, index=True)
    type: str = Field(max_length=64)                          # e.g. "skill.submitted", "user.invited"
    title: str = Field(max_length=200)
    body: str = Field(default="")
    link: Optional[str] = Field(default=None, max_length=500)
    actor_user_id: Optional[int] = Field(default=None, sa_type=Integer, foreign_key="users.id")
    is_read: bool = Field(default=False, nullable=False)


class Group(AuditMixin, table=True):
    """A named group of users inside a client; access policies can be attached to a group instead of each user."""
    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("client_id", "name", name="uq_groups_client_name"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    name: str = Field(max_length=128)
    description: Optional[str] = Field(default=None, max_length=1000)


class GroupMember(AuditMixin, table=True):
    """Membership of a user in a group (one row per membership)."""
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_members_group_user"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    group_id: int = Field(sa_type=BigInteger, foreign_key="groups.id", nullable=False, index=True)
    user_id: int = Field(sa_type=Integer, foreign_key="users.id", nullable=False, index=True)


class AccessPolicy(AuditMixin, table=True):
    """An IAM-style policy document attached to a principal (a user, group, project or the client). `document` is the
    JSON policy evaluated by agent_common.policy; `enabled` lets an admin turn a policy off without deleting it."""
    __tablename__ = "access_policies"

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    principal_type: str = Field(max_length=16)               # user|group|project|client
    principal_id: int = Field(sa_type=BigInteger)            # id of the principal within the client
    name: Optional[str] = Field(default=None, max_length=128)
    document: dict = jsonb_field(dict)                       # {"statements": [...]} (agent_common.policy.PolicyDocument)
    enabled: bool = Field(default=True, nullable=False)


class UserInvite(AuditMixin, table=True):
    """A pending, single-use, expiring invitation of an email into a client with a role and optional groups/projects.
    The signed token is stored only as a hash; the notifier (agent_common.notifications) delivers the link."""
    __tablename__ = "user_invites"

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    email: str = Field(max_length=255)
    role: str = Field(max_length=32)
    groups: list = jsonb_field(list)                         # group ids the invitee joins on acceptance
    projects: list = jsonb_field(list)                      # project ids the invitee is added to on acceptance
    token_hash: str = Field(max_length=128, unique=True)
    status: str = Field(default="pending", max_length=16)    # pending|accepted|expired|revoked
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    invited_by: int = Field(sa_type=Integer, foreign_key="users.id", nullable=False, index=True)
    accepted_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class Subject(AuditMixin, table=True):
    """The thing a domain journey is about: a candidate, a claim, a customer, a brief, a campaign.

    One table for every domain on purpose (docs/impl_multi_domain.md section 3). A `candidates` table, then a
    `claims` table, then a `customers` table is how one platform becomes four; the variable part of a record
    lives in `payload` JSONB and only the columns we filter, join or sort on are relational.

    `domain` + `subject_type` are pack-defined (ats/candidate, insurance/claim, banking/customer, media/brief).
    `external_ref` is the caller's own id for the record and, with `subject_type`, forms the `graph_ref` the
    knowledge graph stores as its `subject_ref` - so the record and its facts stay joined without the graph
    needing to know anything about this table.
    """
    __tablename__ = "subjects"
    __table_args__ = (UniqueConstraint("client_id", "project_id", "domain", "subject_type", "external_ref",
                                       name="uq_subjects_tenant_ref"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    project_id: int = Field(sa_type=Integer, foreign_key="projects.id", nullable=False, index=True)
    domain: str = Field(max_length=64)                       # pack id: ats | insurance | banking | media
    subject_type: str = Field(max_length=64)                 # candidate | claim | customer | brief | ...
    external_ref: str = Field(max_length=128)                # the caller's id, unique per tenant+domain+type
    display_name: str = Field(default="", max_length=255)
    status: str = Field(default="active", max_length=32)     # active | archived | superseded (pack-defined)
    schema_version: str = Field(default="v1", max_length=16)  # the pack schema `payload` was written against
    classification: str = Field(default="confidential", max_length=32)
    payload: dict = jsonb_field(dict)                        # the record itself, shaped by the pack's schema
    search_metadata: dict = jsonb_field(dict)                # small denormalised values used for filtering

    @property
    def graph_ref(self) -> str:
        """The `subject_ref` the knowledge graph stores for this record, e.g. 'candidate:42'."""
        return f"{self.subject_type}:{self.external_ref}"


class SubjectVersion(AuditMixin, table=True):
    """An immutable snapshot of a subject's payload, so a decision can be reproduced against what was known then.

    Written on every payload change. `version_number` is per subject and dense; the newest snapshot matches the
    subject row.
    """
    __tablename__ = "subject_versions"
    __table_args__ = (UniqueConstraint("subject_id", "version_number", name="uq_subject_versions_number"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    subject_id: int = Field(sa_type=BigInteger, foreign_key="subjects.id", nullable=False, index=True)
    version_number: int = Field(nullable=False)
    schema_version: str = Field(default="v1", max_length=16)
    payload_snapshot: dict = jsonb_field(dict)
    change_reason: Optional[str] = Field(default=None, max_length=500)


class SubjectRelation(AuditMixin, table=True):
    """A typed edge between two subjects: application->candidate, claim->policy, customer->account.

    Relational (not graph) because these are business facts the application joins on, not extracted knowledge.
    Self-relations are rejected by a database check so a bad write cannot create one.
    """
    __tablename__ = "subject_relations"
    __table_args__ = (UniqueConstraint("client_id", "source_subject_id", "target_subject_id", "relation_type",
                                       name="uq_subject_relations_edge"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    source_subject_id: int = Field(sa_type=BigInteger, foreign_key="subjects.id", nullable=False, index=True)
    target_subject_id: int = Field(sa_type=BigInteger, foreign_key="subjects.id", nullable=False, index=True)
    relation_type: str = Field(max_length=64)
    attributes: dict = jsonb_field(dict)


class WorkflowRun(AuditMixin, table=True):
    """The application's record of one durable run: a projection, not the source of truth.

    Temporal owns the event history and replay state; this row is what the product needs without talking to
    the engine - list a project's runs, show progress, correlate a chat message with the work it started, bill
    for it, and audit it (docs/impl_temporal.md section 3, v3.0 section 15). It is deliberately small: step
    detail lives in Temporal's history, and step *outputs* live in artifacts.

    `idempotency_key` is what makes a re-submitted request reattach instead of duplicating work: it is unique
    per client, and the caller derives it from the business key (for onboarding, the document).
    """
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("temporal_workflow_id", "temporal_run_id", name="uq_workflow_runs_temporal"),
        UniqueConstraint("client_id", "idempotency_key", name="uq_workflow_runs_idempotency"),
    )

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    project_id: int = Field(sa_type=Integer, foreign_key="projects.id", nullable=False, index=True)
    domain: str = Field(max_length=64)                       # pack id: ats | insurance | banking | media
    journey: str = Field(max_length=64)                      # onboard_subject | match_subject | bulk_match ...
    subject_id: Optional[int] = Field(default=None, sa_type=BigInteger, foreign_key="subjects.id", index=True)
    workflow_type: str = Field(max_length=128)               # the registered workflow name
    task_queue: str = Field(max_length=128)
    temporal_workflow_id: str = Field(max_length=255)
    temporal_run_id: str = Field(default="", max_length=64)
    idempotency_key: str = Field(max_length=128)
    priority_tier: str = Field(default="standard", max_length=16)   # derived server-side, never from a client
    status: str = Field(default="queued", max_length=24)     # queued|running|waiting|completed|failed|...
    input_ref: dict = jsonb_field(dict)                      # references only: ids and hashes, never content
    result_summary: dict = jsonb_field(dict)                 # small summary; large outputs become artifacts
    error_code: Optional[str] = Field(default=None, max_length=32)      # the platform error code, not a trace
    error_summary: Optional[str] = Field(default=None, max_length=500)
    started_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    completed_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class MatchResult(AuditMixin, table=True):
    """One subject's grade in one match run - the row a bulk match writes per candidate, claim or customer.

    A bulk run grades thousands of subjects against one criteria set. Those results do not belong in the
    run's `result_summary` (a small JSON column) or in workflow history (not a database), and they must
    outlive the engine: "why was this candidate ranked 400th" is asked months later, sometimes by someone
    with a legal interest in the answer. So each graded subject gets a row, with the score the rules engine
    computed and enough of its working to explain it without re-running anything.

    Unique on `(client_id, run_id, subject_id)`: an activity that retries after a worker crash updates the
    same row instead of grading the subject twice, which is what makes a long bulk run safe to resume.

    `score` is `Decimal`, not `float`: it is compared, ordered and shown to people, and a ranking that
    depends on binary floating point is a ranking nobody can reproduce.
    """
    __tablename__ = "match_results"
    __table_args__ = (UniqueConstraint("client_id", "run_id", "subject_id",
                                       name="uq_match_results_run_subject"),)

    id: Optional[int] = id_field()
    external_id: uuid.UUID = external_id_field()
    client_id: int = Field(sa_type=Integer, foreign_key="clients.id", nullable=False, index=True)
    project_id: int = Field(sa_type=Integer, foreign_key="projects.id", nullable=False, index=True)
    run_id: int = Field(sa_type=BigInteger, foreign_key="workflow_runs.id", nullable=False)
    subject_id: int = Field(sa_type=BigInteger, foreign_key="subjects.id", nullable=False, index=True)
    domain: str = Field(max_length=64)
    # The criteria set and rubric this grade came from, pinned so a later re-score is comparable rather than
    # quietly different.
    criteria_key: str = Field(default="", max_length=64)
    criteria_ref: str = Field(default="", max_length=128)
    rubric_key: str = Field(default="", max_length=64)
    rubric_version: str = Field(default="", max_length=32)
    score: Optional[Decimal] = Field(default=None, sa_type=Numeric(6, 2))
    band: str = Field(default="", max_length=64)
    recommendation: str = Field(default="", max_length=128)
    rank: Optional[int] = Field(default=None, sa_type=Integer)   # position in this run, written when it ranks
    grades: dict = jsonb_field(dict)                         # per-criterion grades: name -> grade
    breakdown: dict = jsonb_field(dict)                      # the rubric's component points, for explaining
    # A subject that could not be graded is recorded, never dropped: a bulk run that silently skipped 200
    # candidates would look exactly like one that found them unsuitable.
    error_code: Optional[str] = Field(default=None, max_length=32)
    error_summary: Optional[str] = Field(default=None, max_length=500)
