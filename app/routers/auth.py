"""Login, token rotation, logout and the current user's profile."""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth import (
    aware_utc,
    get_access_claims,
    get_current_user,
    hash_refresh_token,
    issue_token_pair,
    normalize_login,
    role_names,
    verify_password,
)
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models.auth import LoginAttempt, RefreshSession, User
from app.rate_limit import enforce_rate_limit

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=32, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class UserProfile(BaseModel):
    id: str
    login: str
    is_active: bool
    roles: list[str]
    created_at: datetime


def _blocked_until(attempts: list[LoginAttempt], now: datetime) -> datetime | None:
    failures: list[datetime] = []
    blocked_until: datetime | None = None
    for attempt in attempts:
        at = aware_utc(attempt.at)
        if attempt.success:
            failures.clear()
            continue
        failures.append(at)
        failures = [time for time in failures if at - time <= timedelta(minutes=10)]
        if len(failures) >= 5:
            candidate = at + timedelta(minutes=15)
            if candidate > now and (blocked_until is None or candidate > blocked_until):
                blocked_until = candidate
    return blocked_until


async def _recent_attempts(db: AsyncSession, login: str, now: datetime) -> list[LoginAttempt]:
    result = await db.scalars(
        select(LoginAttempt)
        .where(LoginAttempt.login == login, LoginAttempt.at >= now - timedelta(minutes=25))
        .order_by(LoginAttempt.at, LoginAttempt.id)
    )
    return list(result.all())


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    normalized = normalize_login(body.login)
    if not normalized or len(normalized) > 255:
        raise ApiError(422, "INVALID_LOGIN", "Некорректный логин")
    # Serialize attempts for an existing account across API instances.
    user = await db.scalar(select(User).where(User.login == normalized).with_for_update())
    now = utcnow()
    attempts = await _recent_attempts(db, normalized, now)
    blocked_until = _blocked_until(attempts, now)
    if blocked_until is not None:
        record_audit(
            db,
            "LOGIN_RATE_LIMITED",
            "User",
            user.id if user else "unknown",
            actor_id=user.id if user else None,
        )
        await db.commit()
        raise ApiError(
            429,
            "LOGIN_RATE_LIMITED",
            "Слишком много попыток входа. Повторите позже",
            {"retryAfterSeconds": max(1, int((blocked_until - now).total_seconds()))},
        )

    valid = await run_in_threadpool(
        verify_password, user.password_hash if user else None, body.password
    )
    ip = request.client.host if request.client else "unknown"
    db.add(
        LoginAttempt(
            login=normalized, ip=ip, at=now, success=bool(user and user.is_active and valid)
        )
    )
    if user is None or not user.is_active or not valid:
        record_audit(
            db,
            "LOGIN_FAILED",
            "User",
            user.id if user else "unknown",
            actor_id=user.id if user else None,
        )
        await db.commit()
        if _blocked_until(
            [*attempts, LoginAttempt(login=normalized, ip=ip, at=now, success=False)], now
        ):
            raise ApiError(
                429, "LOGIN_RATE_LIMITED", "Слишком много попыток входа. Повторите позже"
            )
        raise ApiError(401, "INVALID_CREDENTIALS", "Неверный логин или пароль")

    tokens = issue_token_pair(user, db)
    record_audit(db, "LOGIN_SUCCESS", "User", user.id, actor_id=user.id)
    await db.commit()
    return tokens


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, Any]:
    await enforce_rate_limit(db, "refresh", request.client.host if request.client else "unknown")
    now = utcnow()
    consumed = (
        await db.execute(
            update(RefreshSession)
            .where(
                RefreshSession.token_hash == hash_refresh_token(body.refresh_token),
                RefreshSession.revoked_at.is_(None),
                RefreshSession.expires_at > now,
            )
            .values(revoked_at=now)
            .returning(RefreshSession.id, RefreshSession.user_id)
        )
    ).one_or_none()
    if consumed is None:
        raise ApiError(401, "INVALID_REFRESH_TOKEN", "Недействительный токен обновления")
    session_id, user_id = consumed
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        await db.commit()
        raise ApiError(401, "INVALID_REFRESH_TOKEN", "Недействительный токен обновления")

    tokens = issue_token_pair(user, db)
    record_audit(db, "REFRESH_TOKEN_ROTATED", "RefreshSession", session_id, actor_id=user.id)
    await db.commit()
    return tokens


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    claims: Annotated[dict[str, Any], Depends(get_access_claims)],
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    session = await db.scalar(
        select(RefreshSession)
        .where(RefreshSession.id == claims["sid"], RefreshSession.user_id == user.id)
        .with_for_update()
    )
    if session is not None and session.revoked_at is None:
        session.revoked_at = utcnow()
    record_audit(db, "LOGOUT", "RefreshSession", claims["sid"], actor_id=user.id)
    await db.commit()


@router.get("/users/me", response_model=UserProfile)
async def me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserProfile:
    return UserProfile(
        id=user.id,
        login=user.login,
        is_active=user.is_active,
        roles=sorted(await role_names(db, user.id)),
        created_at=aware_utc(user.created_at),
    )
