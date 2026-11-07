import uuid
from functools import lru_cache

import httpx
import jwt
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backitup.config import get_settings
from backitup.db import session_scope
from backitup.errors import BackItUpError
from backitup.models import Workspace

bearer = HTTPBearer(auto_error=False)


@lru_cache
def jwks_client():
    settings = get_settings()
    return jwt.PyJWKClient(settings.jwks_url(), cache_keys=True, lifespan=300)


def current_workspace(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Workspace:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise BackItUpError("unauthenticated", "Sign in to continue", status=401)
    settings = get_settings()
    try:
        key = jwks_client().get_signing_key_from_jwt(credentials.credentials)
        claims = jwt.decode(credentials.credentials, key.key, algorithms=["ES256", "RS256"],
                            issuer=settings.supabase_public_url + "/auth/v1", audience=settings.jwt_audience,
                            options={"require": ["exp", "sub", "iss", "aud"]})
        user_id = uuid.UUID(claims["sub"])
        if claims.get("role") != "authenticated":
            raise ValueError("Not an authenticated user")
    except Exception as exc:
        raise BackItUpError("unauthenticated", "Your session is invalid or expired. Sign in again.", status=401) from exc
    with session_scope() as session:
        existing = session.scalar(select(Workspace).where(Workspace.owner_id == user_id))
        providers = claims.get("app_metadata", {}).get("providers", [])
        if not existing and "github" not in providers:
            raise BackItUpError("github_signup_required", "Create your account through GitHub before using emergency email sign-in", status=403)
        session.execute(insert(Workspace).values(owner_id=user_id).on_conflict_do_nothing(index_elements=[Workspace.owner_id]))
        workspace = session.scalar(select(Workspace).where(Workspace.owner_id == user_id))
    return workspace


def auth_user(token: str) -> dict:
    settings = get_settings()
    if not settings.supabase_publishable_key:
        raise BackItUpError("auth_setup", "Supabase authentication is not configured", status=503)
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(settings.supabase_url + "/auth/v1/user", headers={"apikey": settings.supabase_publishable_key, "Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            raise BackItUpError("unauthenticated", "Your session could not be verified", status=401)
        return response.json()
    except httpx.HTTPError as exc:
        raise BackItUpError("auth_unavailable", "Authentication service is temporarily unavailable", status=503) from exc


def internal_auth(authorization: str | None = Header(default=None)) -> None:
    import secrets
    path = get_settings().internal_token_file
    if not path.is_file() or not authorization or not secrets.compare_digest(authorization, "Bearer " + path.read_text().strip()):
        raise BackItUpError("unauthenticated", "Internal worker authentication required", status=401)
