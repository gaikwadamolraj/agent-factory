"""Small shared helpers for the operations: load a row by its exposed external_id, scoped to the tenant.

Reads are redundantly filtered by `client_id` on top of RLS, so a wrong-tenant external_id reads as "not found".
"""
from typing import Optional, TypeVar
from uuid import UUID

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from agent_common.db.platform_tables import AuditMixin

M = TypeVar("M", bound=AuditMixin)


async def get_by_external_id(session: AsyncSession, model: type[M], external_id: UUID, client_id: int,
                             include_deleted: bool = False) -> Optional[M]:
    """Load one row of `model` by its external UUID inside `client_id`, or None (indistinguishable from not found)."""
    statement = select(model).where(model.external_id == external_id, model.client_id == client_id)
    if not include_deleted:
        statement = statement.where(col(model.is_deleted).is_(False))
    return (await session.exec(statement)).first()
