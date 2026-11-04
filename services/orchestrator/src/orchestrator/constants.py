"""Orchestrator constants (shared ones live in ats_common.constants)."""
from enum import StrEnum

API_V1 = "/api/v1"
PUBLIC_API_PATHS = frozenset({f"{API_V1}/auth/login", f"{API_V1}/auth/refresh", f"{API_V1}/auth/logout"})
TENANT_API_PREFIXES = (f"{API_V1}/chats",)          # require X-Client-Id + X-Project-Id
AUTH_API_PREFIXES = (API_V1,)                      # require a user JWT (minus PUBLIC_API_PATHS)
SERVICE_NAME_FOR_AGENT = "ats-orchestrator"        # caller identity in service tokens


class TurnAction(StrEnum):
    CHAT = "chat"
    SCREEN = "screen"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class TurnEvent(StrEnum):
    """Server-Sent Event types of a chat turn."""
    TURN_STARTED = "turn_started"
    USER_MESSAGE = "user_message"
    PROGRESS = "progress"
    HEARTBEAT = "heartbeat"
    MESSAGE = "message"
    ABORTED = "aborted"
    ERROR = "error"


DEFAULT_CHAT_TITLE = "New chat"
SCREEN_DEFAULT_MESSAGE = "Screen this candidate against the job description."
GAP_GRADES = ("absent", "invalidated", "adjacent")
MET_GRADES = ("strong", "moderate", "claimed")
