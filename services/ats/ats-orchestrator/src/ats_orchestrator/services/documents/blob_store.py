"""Original-file storage behind an interface (Postgres today; S3/Azure Blob/GDrive later via BLOB_BACKEND)."""
from abc import ABC, abstractmethod
from typing import Optional

from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.db.repositories import TenantScope
from ats_orchestrator.models import DocumentBlob


class BlobStore(ABC):
    """Tenant-scoped byte storage. Keys are opaque; callers never build backend-specific paths."""

    @abstractmethod
    async def put(self, scope: TenantScope, key: str, data: bytes) -> None:
        """Store bytes under `key` (idempotent)."""

    @abstractmethod
    async def get(self, scope: TenantScope, key: str) -> Optional[bytes]:
        """Bytes for `key` if it belongs to the tenant scope, else None."""


class PostgresBlobStore(BlobStore):
    """Keeps blobs in the document_blobs table, inside the caller's unit of work."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def put(self, scope: TenantScope, key: str, data: bytes) -> None:
        if await self.session.get(DocumentBlob, key) is None:
            self.session.add(DocumentBlob(key=key, client_id=scope.client_id, project_id=scope.project_id, data=data))
            await self.session.flush()

    async def get(self, scope: TenantScope, key: str) -> Optional[bytes]:
        blob = await self.session.get(DocumentBlob, key)
        if blob is None or blob.client_id != scope.client_id or blob.project_id != scope.project_id:
            return None
        return blob.data


_BACKENDS = {"postgres": PostgresBlobStore}


def blob_store(backend: str, session: AsyncSession) -> BlobStore:
    try:
        return _BACKENDS[backend](session)
    except KeyError:
        raise RuntimeError(f"Unknown BLOB_BACKEND '{backend}' (supported: {sorted(_BACKENDS)})")
