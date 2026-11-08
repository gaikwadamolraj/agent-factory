"""Routers of the auth-service: /api/v1/auth + /api/v1/admin (browser, via the frontend proxy), /oauth/token and
/.well-known/* (cluster-internal)."""
from fastapi import APIRouter

from auth_service.api import admin, auth, identity, oauth, well_known
from auth_service.constants import API_V1

api_v1 = APIRouter(prefix=API_V1)
api_v1.include_router(auth.router)
api_v1.include_router(admin.router)
api_v1.include_router(identity.router)

routers = [api_v1, oauth.router, well_known.router]
