"""Out-of-app delivery of notifications, behind one `Notifier` interface.

In-app delivery is always the DB row (the UI reads it); the notifier is the *extra* channel - email today's default being
"none". The seam exists now so email (or any provider) is added later with no caller change: `notify(...)` always
writes the row and optionally calls the configured notifier.

Backends:
* NullNotifier  - does nothing (no out-of-app delivery); the safe default when no channel is configured.
* LogNotifier   - logs the delivery (so an invite link is visible in the logs before a mail server exists).
* EmailNotifier - documented stub for SMTP/provider email, added later without touching callers.
"""
from typing import Optional, Protocol, runtime_checkable

from agent_common.logging import get_logger, log_event

log = get_logger(__name__)


@runtime_checkable
class Notifier(Protocol):
    """Delivers a notification out of app. Implementations must never raise into the caller's unit of work: a delivery
    failure must not roll back the DB row that already represents the in-app notification."""

    async def deliver(self, recipient_user_id: int, type: str, title: str, body: str,
                      link: Optional[str], email: Optional[str] = None) -> None:
        """Best-effort out-of-app delivery (email, push, ...). Returns on success, swallows/logs its own errors."""
        ...


class NullNotifier:
    """No out-of-app delivery. The default: in-app (the DB row) is enough, nothing else is sent."""

    async def deliver(self, recipient_user_id: int, type: str, title: str, body: str,
                      link: Optional[str], email: Optional[str] = None) -> None:
        return None


class LogNotifier:
    """Records the delivery in the logs. Useful before a mail server exists: an invite link is visible to an operator.
    Never logs the body (it may carry resume/JD text); only type, recipient and link."""

    async def deliver(self, recipient_user_id: int, type: str, title: str, body: str,
                      link: Optional[str], email: Optional[str] = None) -> None:
        log_event(log, "notification.delivered", channel="log", recipient_user_id=recipient_user_id,
                  type=type, has_link=link is not None)


class EmailNotifier:
    """Future seam: deliver by email over SMTP or a provider API.

    Not implemented yet (docs/impl_agent_factory.md section 2: no email this phase). When added it takes the SMTP /
    provider settings and the secret (through agent_common.secrets), renders the title/body/link into an email and sends
    it through agent_common.resilience.resilient_call. Because invites and approvals already call a Notifier, switching
    from LogNotifier to EmailNotifier is a configuration change, not a code change in the callers."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "EmailNotifier is a future backend; use LogNotifier/NullNotifier until SMTP is configured.")

    async def deliver(self, recipient_user_id: int, type: str, title: str, body: str,
                      link: Optional[str], email: Optional[str] = None) -> None:  # pragma: no cover
        raise NotImplementedError
