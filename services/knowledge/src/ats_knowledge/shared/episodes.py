"""Episode ledger (`knowledge_episodes`): what was ingested, its state, and the durable ingestion queue.

Why a table and not only a Redis stream: one write gives idempotency (unique content hash per graph), retries,
status for callers, and an erasure trail (forget a source -> delete its graph episodes). Workers claim rows with
`FOR UPDATE SKIP LOCKED`, so any number of worker replicas share the queue without double work.

The raw `content` is kept only until the episode is done (needed for retries) and then cleared; the graph keeps
what it extracted.
"""
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Optional

from ats_common.db.tables import created_at_field, updated_at_field, utcnow
from ats_common.knowledge.contracts import EpisodeStatus
from sqlalchemy import Column, DateTime, Text, UniqueConstraint, cast, delete, or_, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession


class KnowledgeEpisode(SQLModel, table=True):
    __tablename__ = "knowledge_episodes"
    __table_args__ = (UniqueConstraint("graph_id", "content_hash", name="uq_knowledge_episodes_graph_hash"),)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True, max_length=32)
    client_id: int = Field(foreign_key="clients.id", ondelete="CASCADE")
    project_id: int = Field(foreign_key="projects.id", ondelete="CASCADE")
    namespace: str = Field(max_length=32)
    graph_id: str = Field(max_length=64)
    source_type: str = Field(max_length=32)
    source_ref: str = Field(max_length=128)
    content_hash: str = Field(max_length=64)
    content: Optional[str] = Field(default=None, sa_column=Column(Text))
    guidance: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    ontology: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSONB))
    reference_time: datetime = Field(sa_type=DateTime(timezone=True))
    status: str = Field(default=EpisodeStatus.QUEUED, max_length=16)
    attempts: int = 0
    available_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    error_code: Optional[str] = Field(default=None, max_length=32)
    subject_ref: Optional[str] = Field(default=None, max_length=128)
    # one graph episode per document section, appended as each section is extracted (a retry resumes after them)
    graph_episode_uuids: list[str] = Field(default_factory=list,
                                           sa_column=Column(JSONB, nullable=False, server_default="[]"))
    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()


class EpisodeRepository:
    """All SQL for the ledger. Never commits; the caller's unit of work does. Every read is tenant-scoped by
    `graph_id`, which already encodes namespace + client + project."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def find_by_hash(self, graph_id: str, content_hash: str) -> Optional[KnowledgeEpisode]:
        statement = select(KnowledgeEpisode).where(KnowledgeEpisode.graph_id == graph_id,
                                                   KnowledgeEpisode.content_hash == content_hash)
        return (await self.session.exec(statement)).first()

    async def add(self, episode: KnowledgeEpisode) -> KnowledgeEpisode:
        self.session.add(episode)
        await self.session.flush()
        return episode

    async def get(self, graph_id: str, episode_id: str) -> Optional[KnowledgeEpisode]:
        statement = select(KnowledgeEpisode).where(KnowledgeEpisode.graph_id == graph_id,
                                                   KnowledgeEpisode.id == episode_id)
        return (await self.session.exec(statement)).first()

    async def by_source(self, graph_id: str, source_ref: str) -> Sequence[KnowledgeEpisode]:
        statement = select(KnowledgeEpisode).where(KnowledgeEpisode.graph_id == graph_id,
                                                   KnowledgeEpisode.source_ref == source_ref)
        return (await self.session.exec(statement)).all()

    async def done_episode_uuids(self, graph_id: str, subject_refs: list[str]) -> set[str]:
        """Graph episode ids of every finished row about these subjects (to limit a search to them)."""
        statement = select(KnowledgeEpisode.graph_episode_uuids).where(
            KnowledgeEpisode.graph_id == graph_id, KnowledgeEpisode.subject_ref.in_(subject_refs),
            KnowledgeEpisode.status == EpisodeStatus.DONE)
        return {uuid for uuids in (await self.session.exec(statement)).all() for uuid in uuids}

    async def recent(self, graph_id: str, subject_ref: Optional[str], source_ref: Optional[str],
                   limit: int) -> Sequence[KnowledgeEpisode]:
        """Rows of one graph, newest first, optionally one subject and/or source."""
        statement = select(KnowledgeEpisode).where(KnowledgeEpisode.graph_id == graph_id)
        if subject_ref:
            statement = statement.where(KnowledgeEpisode.subject_ref == subject_ref)
        if source_ref:
            statement = statement.where(KnowledgeEpisode.source_ref == source_ref)
        return (await self.session.exec(statement.order_by(KnowledgeEpisode.created_at.desc()).limit(limit))).all()

    async def delete_ids(self, episode_ids: list[str]) -> None:
        if episode_ids:
            await self.session.exec(delete(KnowledgeEpisode).where(KnowledgeEpisode.id.in_(episode_ids)))

    async def claim(self, limit: int, lease_s: int) -> Sequence[KnowledgeEpisode]:
        """Take up to `limit` due episodes (queued, or processing with an expired lease) and lease them."""
        now = utcnow()
        due = or_(KnowledgeEpisode.status == EpisodeStatus.QUEUED,
                  KnowledgeEpisode.status == EpisodeStatus.PROCESSING)
        statement = (select(KnowledgeEpisode)
                     .where(due, KnowledgeEpisode.available_at <= now)
                     .order_by(KnowledgeEpisode.available_at)
                     .limit(limit)
                     .with_for_update(skip_locked=True))
        episodes = (await self.session.exec(statement)).all()
        for episode in episodes:
            episode.status, episode.attempts = EpisodeStatus.PROCESSING, episode.attempts + 1
            episode.available_at = now + timedelta(seconds=lease_s)     # lease: retried if the worker dies
            self.session.add(episode)
        await self.session.flush()
        return episodes

    async def add_section(self, episode_id: str, graph_episode_uuid: str) -> None:
        """Record one extracted section at once, so a retry or another worker never extracts it twice."""
        await self.session.exec(update(KnowledgeEpisode).where(KnowledgeEpisode.id == episode_id).values(
            graph_episode_uuids=KnowledgeEpisode.graph_episode_uuids.op("||")(
                cast([graph_episode_uuid], JSONB)), updated_at=utcnow()))

    async def mark_done(self, episode_id: str) -> None:
        await self.session.exec(update(KnowledgeEpisode).where(KnowledgeEpisode.id == episode_id).values(
            status=EpisodeStatus.DONE, content=None, error_code=None, updated_at=utcnow()))

    async def mark_failed(self, episode_id: str, error_code: str, retry_in_s: Optional[float]) -> None:
        """Back to the queue after `retry_in_s`, or FAILED for good when it is None (content is then cleared)."""
        values: dict[str, Any] = {"error_code": error_code, "updated_at": utcnow()}
        if retry_in_s is None:
            values.update(status=EpisodeStatus.FAILED, content=None)
        else:
            values.update(status=EpisodeStatus.QUEUED, available_at=utcnow() + timedelta(seconds=retry_in_s))
        await self.session.exec(update(KnowledgeEpisode).where(KnowledgeEpisode.id == episode_id).values(**values))

    async def release(self, episode_id: str) -> None:
        """Hand an unfinished episode back to the queue at once (worker shutting down). The interrupted attempt
        is not counted, so deploys and scale-downs never use up an episode's attempts."""
        await self.session.exec(
            update(KnowledgeEpisode)
            .where(KnowledgeEpisode.id == episode_id, KnowledgeEpisode.status == EpisodeStatus.PROCESSING)
            .values(status=EpisodeStatus.QUEUED, available_at=utcnow(), updated_at=utcnow(),
                    attempts=KnowledgeEpisode.attempts - 1))
