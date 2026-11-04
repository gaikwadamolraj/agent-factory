"""Tenant-scoped repositories. All queries are filtered by the request's client and project (TenantScope)."""
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import bindparam, text
from sqlmodel import col, select

from ats_common.db.repositories import TenantRepository
from ats_common.db.tables import utcnow
from orchestrator.models import Chat, ChatMessage, Document, Screening


class ChatRepository(TenantRepository[Chat]):
    model = Chat

    async def create(self, user_id: int, title: str) -> Chat:
        return await self.add(Chat(client_id=self.scope.client_id, project_id=self.scope.project_id,
                                   user_id=user_id, title=title))

    async def get_owned(self, chat_id: str, user_id: int) -> Optional[Chat]:
        chat = await self.get_scoped(chat_id)
        return chat if chat and chat.user_id == user_id else None

    async def list_for_user(self, user_id: int, limit: int = 100) -> Sequence[Chat]:
        statement = self.scoped().where(Chat.user_id == user_id).order_by(col(Chat.updated_at).desc()).limit(limit)
        return (await self.session.exec(statement)).all()

    async def touch(self, chat: Chat) -> None:
        chat.updated_at = utcnow()
        self.session.add(chat)

    async def add_message(self, chat_id: str, role: str, content: str, data: Optional[dict] = None) -> ChatMessage:
        message = ChatMessage(chat_id=chat_id, role=role, content=content, data=data or {})
        self.session.add(message)
        await self.session.flush()
        return message

    async def list_messages(self, chat_id: str, limit: Optional[int] = None) -> list[ChatMessage]:
        """Messages of a chat (call only after the chat was loaded through this scope)."""
        statement = select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(col(ChatMessage.id).desc())
        if limit:
            statement = statement.limit(limit)
        return list(reversed((await self.session.exec(statement)).all()))


class DocumentRepository(TenantRepository[Document]):
    model = Document

    async def find_by_hash(self, kind: str, text_hash: str) -> Optional[Document]:
        statement = self.scoped().where(Document.kind == kind, Document.text_hash == text_hash)
        return (await self.session.exec(statement)).first()


class ScreeningRepository(TenantRepository[Screening]):
    model = Screening

    async def latest_for_chat(self, chat_id: str) -> Optional[Screening]:
        statement = self.scoped().where(Screening.chat_id == chat_id).order_by(col(Screening.id).desc()).limit(1)
        return (await self.session.exec(statement)).first()


@dataclass(frozen=True)
class ChunkHit:
    document_id: int
    kind: str
    chunk_index: int
    content: str
    score: float


# Reciprocal Rank Fusion of a vector ranking and a full-text ranking (k=60), restricted to the tenant + documents.
_HYBRID_SQL = text("""
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> CAST(:query_vector AS vector)) AS rank
    FROM document_chunks
    WHERE client_id = :client_id AND project_id = :project_id AND document_id IN :document_ids
      AND embedding IS NOT NULL AND embedding_model = :embedding_model AND :use_vector
    ORDER BY embedding <=> CAST(:query_vector AS vector)
    LIMIT :candidates
),
kw AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS rank
    FROM document_chunks, websearch_to_tsquery('english', :query_text) AS q
    WHERE client_id = :client_id AND project_id = :project_id AND document_id IN :document_ids AND tsv @@ q
    ORDER BY ts_rank_cd(tsv, q) DESC
    LIMIT :candidates
)
SELECT c.document_id, c.kind, c.chunk_index, c.content,
       COALESCE(1.0 / (60 + vec.rank), 0) + COALESCE(1.0 / (60 + kw.rank), 0) AS score
FROM document_chunks c
LEFT JOIN vec ON vec.id = c.id
LEFT JOIN kw ON kw.id = c.id
WHERE vec.id IS NOT NULL OR kw.id IS NOT NULL
ORDER BY score DESC
LIMIT :limit
""").bindparams(bindparam("document_ids", expanding=True))

_INSERT_CHUNK_SQL = text("""
INSERT INTO document_chunks (client_id, project_id, document_id, kind, chunk_index, content, embedding_model, embedding)
VALUES (:client_id, :project_id, :document_id, :kind, :chunk_index, :content, :embedding_model,
        CAST(:embedding AS vector))
""")


def _vector_literal(values: Optional[list[float]]) -> Optional[str]:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]" if values else None


class ChunkRepository:
    """Chunks of tenant documents for hybrid (vector + keyword) search."""

    def __init__(self, session, scope):
        self.session, self.scope = session, scope

    async def replace_chunks(self, document: Document, chunks: list[str], embeddings: Optional[list[list[float]]],
                             embedding_model: Optional[str]) -> None:
        await self.session.exec(text("DELETE FROM document_chunks WHERE document_id = :doc AND client_id = :c")
                                .bindparams(doc=document.id, c=self.scope.client_id))
        rows = [{"client_id": self.scope.client_id, "project_id": self.scope.project_id, "document_id": document.id,
                 "kind": document.kind, "chunk_index": i, "content": chunk,
                 "embedding_model": embedding_model if embeddings else None,
                 "embedding": _vector_literal(embeddings[i]) if embeddings else None}
                for i, chunk in enumerate(chunks)]
        if rows:
            await self.session.exec(_INSERT_CHUNK_SQL, params=rows)

    async def hybrid_search(self, document_ids: list[int], query_text: str, query_vector: Optional[list[float]],
                            embedding_model: Optional[str], limit: int = 5, candidates: int = 20) -> list[ChunkHit]:
        if not document_ids:
            return []
        result = await self.session.exec(_HYBRID_SQL, params={
            "client_id": self.scope.client_id, "project_id": self.scope.project_id, "document_ids": document_ids,
            "query_vector": _vector_literal(query_vector) or "[0]", "use_vector": bool(query_vector),
            "embedding_model": embedding_model or "", "query_text": query_text, "candidates": candidates,
            "limit": limit})
        return [ChunkHit(r.document_id, r.kind, r.chunk_index, r.content, float(r.score)) for r in result.all()]
