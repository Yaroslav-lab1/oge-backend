from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import (
    aware_utc,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    normalize_login,
    verify_password,
)
from app.config import settings
from app.db import Base, get_db, utcnow
from app.errors import ApiError
from app.models.auth import (
    LoginAttempt,
    Permission,
    RateLimitBucket,
    RefreshSession,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.models.review import AuditEvent
from app.routers import admin, auth
from app.routers.auth import _blocked_until


@pytest.fixture
async def api():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    auth_tables = [
        User.__table__,
        Role.__table__,
        Permission.__table__,
        UserRole.__table__,
        RolePermission.__table__,
        RefreshSession.__table__,
        LoginAttempt.__table__,
        RateLimitBucket.__table__,
        AuditEvent.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=auth_tables)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with sessions() as db:
            yield db

    app = FastAPI()
    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(admin.router, prefix="/api/v1")

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "correlationId": "test-correlation-id",
            },
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, sessions
    await engine.dispose()


async def seed_user(sessions, login: str, password: str, role_name: str) -> User:
    async with sessions() as db:
        user = User(login=login, password_hash=hash_password(password))
        role = await db.scalar(select(Role).where(Role.name == role_name))
        if role is None:
            role = Role(name=role_name)
        db.add_all([user, role])
        await db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id))
        await db.commit()
        return user


def test_argon2id_password_hash_and_mismatch():
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, "correct horse battery staple")
    assert not verify_password(encoded, "wrong password")
    assert not verify_password(None, "wrong password")


def test_access_token_rejects_tampering_and_refresh_token():
    invalid = jwt.encode(
        {
            "sub": "u",
            "sid": "s",
            "jti": "j",
            "typ": "refresh",
            "exp": utcnow() + timedelta(hours=1),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(ApiError) as exc:
        decode_access_token(invalid)
    assert exc.value.status_code == 401
    with pytest.raises(ApiError):
        decode_access_token(invalid + "tampered")


def test_five_failures_in_ten_minutes_block_for_fifteen_minutes():
    start = utcnow()
    attempts = [SimpleNamespace(at=start + timedelta(seconds=i), success=False) for i in range(5)]
    assert _blocked_until(attempts, start + timedelta(minutes=14)) is not None
    assert _blocked_until(attempts, start + timedelta(minutes=16)) is None
    attempts.insert(
        3, SimpleNamespace(at=start + timedelta(seconds=2, microseconds=1), success=True)
    )
    assert _blocked_until(attempts, start + timedelta(minutes=1)) is None


@given(st.text(max_size=100))
def test_login_normalization_is_idempotent(value: str):
    assert normalize_login(normalize_login(value)) == normalize_login(value)


def test_refresh_hash_never_contains_opaque_token():
    raw = "opaque-token-with-random-bytes"
    digest = hash_refresh_token(raw)
    assert len(digest) == 64
    assert raw not in digest
    assert digest == hash_refresh_token(raw)


def test_naive_database_times_are_read_as_utc():
    naive = datetime(2026, 11, 5, 12, 0)
    assert aware_utc(naive) == datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    local = naive.replace(tzinfo=timezone(timedelta(hours=3)))
    assert aware_utc(local) == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_login_refresh_rotation_logout_and_profile(api):
    client, sessions = api
    user = await seed_user(sessions, "student", "password123", "STUDENT")
    unauthenticated = await client.get("/api/v1/users/me")
    assert unauthenticated.status_code == 401

    login = await client.post(
        "/api/v1/auth/login", json={"login": " STUDENT ", "password": "password123"}
    )
    assert login.status_code == 200, login.text
    pair = login.json()
    claims = decode_access_token(pair["access_token"])
    assert claims["sub"] == user.id
    assert claims["exp"] - claims["iat"] <= 15 * 60
    assert pair["expires_in"] <= 15 * 60

    async with sessions() as db:
        stored = await db.scalar(select(RefreshSession).where(RefreshSession.user_id == user.id))
        assert stored is not None
        assert stored.token_hash != pair["refresh_token"]
        assert stored.token_hash == hash_refresh_token(pair["refresh_token"])
        assert (stored.expires_at - stored.created_at).days <= 7

    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    profile = await client.get("/api/v1/users/me", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["roles"] == ["STUDENT"]
    assert "password_hash" not in profile.json()

    rotated = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()
    assert second["refresh_token"] != pair["refresh_token"]
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert replay.status_code == 401

    logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {second['access_token']}"},
    )
    assert logout.status_code == 204
    revoked = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
    )
    assert revoked.status_code == 401


@pytest.mark.asyncio
async def test_brute_force_and_admin_role_checks(api):
    client, sessions = api
    await seed_user(sessions, "admin", "adminpass123", "ADMIN")
    await seed_user(sessions, "student", "studentpass123", "STUDENT")

    student_login = await client.post(
        "/api/v1/auth/login", json={"login": "student", "password": "studentpass123"}
    )
    student_headers = {"Authorization": f"Bearer {student_login.json()['access_token']}"}
    denied = await client.post(
        "/api/v1/admin/users",
        json={"login": "new", "password": "password123", "roles": ["STUDENT"]},
        headers=student_headers,
    )
    assert denied.status_code == 403

    admin_login = await client.post(
        "/api/v1/auth/login", json={"login": "admin", "password": "adminpass123"}
    )
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    created = await client.post(
        "/api/v1/admin/users",
        json={"login": "NEW", "password": "password123", "roles": ["STUDENT"]},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["login"] == "new"
    target_id = created.json()["id"]
    patch = await client.patch(
        f"/api/v1/admin/users/{target_id}",
        json={"roles": ["EXPERT"], "is_active": False},
        headers=admin_headers,
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["roles"] == ["EXPERT"]
    assert patch.json()["is_active"] is False
    disabled_login = await client.post(
        "/api/v1/auth/login", json={"login": "new", "password": "password123"}
    )
    assert disabled_login.status_code == 401
    async with sessions() as db:
        events = (await db.scalars(select(AuditEvent.action))).all()
        assert "USER_CREATED" in events
        assert "USER_ROLES_CHANGED" in events


@pytest.mark.asyncio
async def test_five_bad_logins_trigger_rate_limit(api):
    client, sessions = api
    await seed_user(sessions, "student", "studentpass123", "STUDENT")
    for number in range(5):
        response = await client.post(
            "/api/v1/auth/login", json={"login": "student", "password": "incorrect"}
        )
        assert response.status_code == (429 if number == 4 else 401)
    blocked = await client.post(
        "/api/v1/auth/login", json={"login": "student", "password": "studentpass123"}
    )
    assert blocked.status_code == 429


@pytest.mark.asyncio
async def test_admin_validation_password_reset_and_live_role_revocation(api):
    client, sessions = api
    await seed_user(sessions, "admin", "adminpass123", "ADMIN")
    admin_login = await client.post(
        "/api/v1/auth/login", json={"login": "admin", "password": "adminpass123"}
    )
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    created = await client.post(
        "/api/v1/admin/users",
        json={"login": "another", "password": "password123", "roles": ["ADMIN"]},
        headers=admin_headers,
    )
    assert created.status_code == 201
    target_id = created.json()["id"]

    duplicate = await client.post(
        "/api/v1/admin/users",
        json={"login": "ANOTHER", "password": "password123", "roles": ["STUDENT"]},
        headers=admin_headers,
    )
    assert duplicate.status_code == 409
    invalid_role = await client.post(
        "/api/v1/admin/users",
        json={"login": "third", "password": "password123", "roles": ["SUPERUSER"]},
        headers=admin_headers,
    )
    assert invalid_role.status_code == 422
    assert (
        await client.patch(f"/api/v1/admin/users/{target_id}", json={}, headers=admin_headers)
    ).status_code == 400
    assert (
        await client.patch(
            "/api/v1/admin/users/not-found", json={"is_active": False}, headers=admin_headers
        )
    ).status_code == 404

    target_login = await client.post(
        "/api/v1/auth/login", json={"login": "another", "password": "password123"}
    )
    target_pair = target_login.json()
    target_headers = {"Authorization": f"Bearer {target_pair['access_token']}"}
    assert (
        await client.post(
            "/api/v1/admin/users",
            json={"login": "fourth", "password": "password123", "roles": ["STUDENT"]},
            headers=target_headers,
        )
    ).status_code == 201

    changed = await client.patch(
        f"/api/v1/admin/users/{target_id}",
        json={"roles": ["STUDENT"], "password": "newpassword123"},
        headers=admin_headers,
    )
    assert changed.status_code == 200
    assert changed.json()["roles"] == ["STUDENT"]
    assert (
        await client.post(
            "/api/v1/admin/users",
            json={"login": "fifth", "password": "password123", "roles": ["STUDENT"]},
            headers=target_headers,
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": target_pair["refresh_token"]}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login", json={"login": "another", "password": "password123"}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login", json={"login": "another", "password": "newpassword123"}
        )
    ).status_code == 200
