"""Administrator management of user status and role assignments."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth import (
    KNOWN_ROLES,
    aware_utc,
    hash_password,
    normalize_login,
    require_roles,
    role_names,
)
from app.db import get_db, utcnow
from app.errors import ApiError, conflict, not_found
from app.models.auth import RefreshSession, Role, User, UserRole

router = APIRouter(prefix="/admin", tags=["admin"])
admin_required = require_roles("ADMIN")


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=1024)
    roles: list[str] = Field(min_length=1)
    is_active: bool = True


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    roles: list[str] | None = None
    password: str | None = Field(default=None, min_length=8, max_length=1024)


class AdminUserResponse(BaseModel):
    id: str
    login: str
    is_active: bool
    roles: list[str]
    created_at: datetime


async def _resolved_roles(db: AsyncSession, requested: list[str]) -> list[Role]:
    names = set(requested)
    if not names.issubset(KNOWN_ROLES):
        raise ApiError(422, "INVALID_ROLE", "Неизвестная роль")
    existing = await db.scalars(select(Role).where(Role.name.in_(names)))
    by_name = {role.name: role for role in existing}
    for name in sorted(names - by_name.keys()):
        role = Role(name=name)
        db.add(role)
        by_name[name] = role
    await db.flush()
    return [by_name[name] for name in sorted(names)]


async def _profile(db: AsyncSession, user: User) -> AdminUserResponse:
    return AdminUserResponse(
        id=user.id,
        login=user.login,
        is_active=user.is_active,
        roles=sorted(await role_names(db, user.id)),
        created_at=aware_utc(user.created_at),
    )


@router.post("/users", status_code=status.HTTP_201_CREATED, response_model=AdminUserResponse)
async def create_user(
    body: UserCreate,
    actor: Annotated[User, Depends(admin_required)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserResponse:
    login = normalize_login(body.login)
    if not login or len(login) > 255:
        raise ApiError(422, "INVALID_LOGIN", "Некорректный логин")
    if await db.scalar(select(User.id).where(User.login == login)):
        raise conflict("LOGIN_EXISTS", "Пользователь с таким логином уже существует")

    roles = await _resolved_roles(db, body.roles)
    password_hash = await run_in_threadpool(hash_password, body.password)
    user = User(login=login, password_hash=password_hash, is_active=body.is_active)
    db.add(user)
    await db.flush()
    db.add_all(UserRole(user_id=user.id, role_id=role.id) for role in roles)
    record_audit(
        db,
        "USER_CREATED",
        "User",
        user.id,
        actor_id=actor.id,
        context={"roles": ",".join(sorted(set(body.roles)))},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict("LOGIN_EXISTS", "Пользователь с таким логином уже существует") from exc
    return await _profile(db, user)


@router.patch("/users/{user_id}", response_model=AdminUserResponse)
async def update_user(
    user_id: str,
    body: UserPatch,
    actor: Annotated[User, Depends(admin_required)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUserResponse:
    if not body.model_fields_set or all(
        getattr(body, field) is None for field in body.model_fields_set
    ):
        raise ApiError(400, "EMPTY_UPDATE", "Не указаны изменения")
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise not_found("Пользователь не найден")

    context: dict[str, object] = {}
    if body.is_active is not None:
        user.is_active = body.is_active
        context["isActive"] = body.is_active
    if body.password is not None:
        user.password_hash = await run_in_threadpool(hash_password, body.password)
        context["passwordChanged"] = True
    if body.roles is not None:
        roles = await _resolved_roles(db, body.roles)
        await db.execute(delete(UserRole).where(UserRole.user_id == user.id))
        db.add_all(UserRole(user_id=user.id, role_id=role.id) for role in roles)
        context["roles"] = ",".join(sorted(set(body.roles)))
        record_audit(db, "USER_ROLES_CHANGED", "User", user.id, actor_id=actor.id, context=context)
    if body.is_active is False or body.password is not None:
        await db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
    record_audit(db, "USER_UPDATED", "User", user.id, actor_id=actor.id, context=context)
    await db.commit()
    return await _profile(db, user)
