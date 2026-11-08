"""Audit trail for every mutating tool (docs/impl_registry_service.md section 3: actor, before/after).

There is no separate audit table: the record lives in two durable, already-present places, plus a structured log
line an operator can search. This keeps the trail without a new migration.

1. The affected row's audit-mixin columns (`created_by` / `updated_by` / `deleted_by`) carry the actor's user id.
   Helpers here stamp those columns so a caller cannot forget.
2. A `factory.audit` log event with the actor, tool, target and a compact before/after diff (never resume/JD text
   or secrets - CLAUDE.md section 9).
"""
from datetime import datetime, timezone
from typing import Any, Optional

from agent_common.db.platform_tables import AuditMixin
from agent_common.logging import get_logger, log_event

log = get_logger(__name__)


def stamp_created(row: AuditMixin, actor_id: Optional[int]) -> None:
    """Record who created a row (audit mixin)."""
    row.created_by = actor_id
    row.updated_by = actor_id


def stamp_updated(row: AuditMixin, actor_id: Optional[int]) -> None:
    """Record who last changed a row (audit mixin)."""
    row.updated_by = actor_id


def stamp_deleted(row: AuditMixin, actor_id: Optional[int], reason: Optional[str] = None) -> None:
    """Mark a row soft-deleted by an actor, with a reason (audit mixin)."""
    row.is_deleted = True
    row.deleted_by = actor_id
    row.deleted_at = datetime.now(timezone.utc)
    row.deleted_reason = reason
    row.updated_by = actor_id


def record(tool: str, *, actor_id: Optional[int], client_id: int, target: str,
           before: Optional[dict[str, Any]] = None, after: Optional[dict[str, Any]] = None) -> None:
    """Write the `factory.audit` event for a mutating tool: who did what to which target, and the before/after.

    `before`/`after` must be small, non-sensitive dicts (ids, states, names, enabled flags) - never free text that
    could contain resume/JD content or secrets.
    """
    log_event(log, "factory.audit", tool=tool, actor_id=actor_id, client_id=client_id, target=target,
              before=before or None, after=after or None)
