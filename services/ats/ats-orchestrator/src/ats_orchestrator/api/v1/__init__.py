from fastapi import APIRouter

from ats_orchestrator.api.v1 import chats, runs
from ats_orchestrator.constants import API_V1

# Sign-in (/api/v1/auth/*) and user management (/api/v1/admin/*) live in the auth-service.
router = APIRouter(prefix=API_V1)
router.include_router(chats.router)
router.include_router(runs.router)
