"""Password, token and role checks shared by protected API routes."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db, new_id, utcnow
from app.errors import ApiError, forbidden
from app.models.auth import RefreshSession, Role, User, UserRole

KNOWN_ROLES = frozenset(
    {
        "STUDENT",
        "EXPERT",
        "SENIOR_EXPERT",
        "CONTENT_MANAGER",
        "ADMIN",
        "AUDITOR",
        "BI_SERVICE",
    }
)

_password_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_dummy_hash = _password_hasher.hash("no-such-user-dummy-password")
bearer_scheme = HTTPBearer(auto_error=False)


def normalize_login(login: str) -> str:
    return login.strip().casefold()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash or _dummy_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def aware_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo from DateTime; stored values are always UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _access_lifetime() -> timedelta:
    return timedelta(minutes=min(max(settings.access_minutes, 1), 15))


def _refresh_lifetime() -> timedelta:
    return timedelta(days=min(max(settings.refresh_days, 1), 7))


def issue_token_pair(user: User, db: AsyncSession) -> dict[str, Any]:
    now = utcnow()
    session_id = new_id()
    refresh_token = secrets.token_urlsafe(48)
    db.add(
        RefreshSession(
            id=session_id,
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=now + _refresh_lifetime(),
        )
    )
    expires_in = int(_access_lifetime().total_seconds())
    access_token = jwt.encode(
        {
            "sub": user.id,
            "sid": session_id,
            "jti": new_id(),
            "typ": "access",
            "iat": now,
            "exp": now + _access_lifetime(),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        raise ApiError(401, "INVALID_ACCESS_TOKEN", "Недействительный токен доступа") from exc
    if (
        claims.get("typ") != "access"
        or not isinstance(claims.get("sub"), str)
        or not isinstance(claims.get("sid"), str)
        or not isinstance(claims.get("jti"), str)
    ):
        raise ApiError(401, "INVALID_ACCESS_TOKEN", "Недействительный токен доступа")
    return claims


async def get_access_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, Any]:
    if credentials is None:
        raise ApiError(401, "AUTH_REQUIRED", "Требуется авторизация")
    return decode_access_token(credentials.credentials)


async def get_current_user(
    claims: Annotated[dict[str, Any], Depends(get_access_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user = await db.scalar(select(User).where(User.id == claims["sub"]))
    if user is None or not user.is_active:
        raise ApiError(401, "INVALID_ACCESS_TOKEN", "Недействительный токен доступа")
    return user


async def role_names(db: AsyncSession, user_id: str) -> set[str]:
    names = await db.scalars(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    return set(names.all())


def require_roles(*names: str) -> Any:
    allowed = frozenset(names)

    async def check(
        user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        if not allowed.intersection(await role_names(db, user.id)):
            raise forbidden()
        return user

    return check
