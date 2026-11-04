from fastapi import APIRouter

from orchestrator.api.v1 import admin, auth, chats
from orchestrator.constants import API_V1

router = APIRouter(prefix=API_V1)
router.include_router(auth.router)
router.include_router(chats.router)
router.include_router(admin.router)
