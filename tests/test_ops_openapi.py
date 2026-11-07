"""Operational endpoints, error envelope, body limits and the published API contract."""

import logging
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import ASGITransport, AsyncByteStream, AsyncClient
from openapi_spec_validator import validate
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.body_limit import BodyLimitMiddleware
from app.config import settings
from app.db import Base
from app.main import app
from app.routers import ops


@pytest.mark.asyncio
async def test_request_logging_excludes_body_and_unmatched_path_secrets(caplog):
    secret = "unique-sensitive-value-for-log-test"
    with caplog.at_level(logging.INFO, logger="oge"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/v1/auth/login", json={"password": secret})
            await client.get(f"/api/v1/absent/{secret}")
    application_logs = " ".join(
        record.getMessage() for record in caplog.records if record.name == "oge"
    )
    assert "http_request" in application_logs
    assert secret not in application_logs


@pytest_asyncio.fixture
async def ops_database(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ops, "SessionLocal", maker)
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_health_separates_liveness_from_database_readiness(ops_database, monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        live = await client.get("/api/v1/health/live")
        ready = await client.get("/api/v1/health/ready")
        assert live.status_code == 200
        assert live.json() == {"status": "ok"}
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}

        def unavailable():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(ops, "SessionLocal", unavailable)
        live_without_db = await client.get("/api/v1/health/live")
        ready_without_db = await client.get("/api/v1/health/ready")
        assert live_without_db.status_code == 200
        assert ready_without_db.status_code == 503
        assert ready_without_db.json()["code"] == "NOT_READY"


@pytest.mark.asyncio
async def test_metrics_requires_service_token_and_returns_prometheus_text(ops_database):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/api/v1/metrics")
        wrong = await client.get("/api/v1/metrics", headers={"X-Metrics-Token": "wrong"})
        allowed = await client.get(
            "/api/v1/metrics", headers={"X-Metrics-Token": settings.metrics_token}
        )
    assert missing.status_code == wrong.status_code == 403
    assert missing.json()["code"] == wrong.json()["code"] == "METRICS_FORBIDDEN"
    assert allowed.status_code == 200
    assert allowed.headers["content-type"].startswith("text/plain")
    assert "oge_http_requests_total" in allowed.text
    assert "oge_database_up" in allowed.text


@pytest.mark.asyncio
async def test_success_validation_auth_and_missing_route_share_correlation_contract():
    supplied = str(uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        success = await client.get("/api/v1/health/live", headers={"X-Correlation-ID": supplied})
        unauthorized = await client.get("/api/v1/users/me", headers={"X-Correlation-ID": supplied})
        invalid = await client.post("/api/v1/auth/login", json={"login": "student"})
        missing = await client.get("/api/v1/does-not-exist")
        regenerated = await client.get(
            "/api/v1/health/live", headers={"X-Correlation-ID": "invalid"}
        )

    assert success.headers["X-Correlation-ID"] == supplied
    assert unauthorized.status_code == 401
    assert unauthorized.headers["X-Correlation-ID"] == supplied
    assert unauthorized.json()["correlationId"] == supplied
    for response, expected_status in ((invalid, 422), (missing, 404)):
        assert response.status_code == expected_status
        data = response.json()
        assert set(data) == {"code", "message", "details", "correlationId"}
        assert data["correlationId"] == response.headers["X-Correlation-ID"]
        UUID(data["correlationId"])
        assert "traceback" not in response.text.lower()
        assert "select " not in response.text.lower()
    assert regenerated.headers["X-Correlation-ID"] != "invalid"
    UUID(regenerated.headers["X-Correlation-ID"])


class ChunkedBody(AsyncByteStream):
    def __init__(self, *parts: bytes) -> None:
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part


@pytest.mark.asyncio
async def test_chunked_body_limit_applies_without_content_length_and_replays_safe_chunks():
    limited = FastAPI()
    limited.add_middleware(BodyLimitMiddleware, max_bytes=6)

    @limited.post("/echo")
    async def echo(request: Request) -> dict:
        return {
            "body": (await request.body()).decode(),
            "contentLength": request.headers.get("content-length"),
        }

    async with AsyncClient(transport=ASGITransport(app=limited), base_url="http://test") as client:
        safe = await client.post(
            "/echo", content=ChunkedBody(b"abc", b"def"), headers={"Transfer-Encoding": "chunked"}
        )
        oversized = await client.post(
            "/echo", content=ChunkedBody(b"abc", b"defg"), headers={"Transfer-Encoding": "chunked"}
        )
    assert safe.status_code == 200
    assert safe.json() == {"body": "abcdef", "contentLength": None}
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "REQUEST_TOO_LARGE"
    assert oversized.json()["correlationId"] == oversized.headers["X-Correlation-ID"]


def test_openapi_31_is_valid_and_covers_every_public_route():
    schema = app.openapi()
    assert schema["openapi"] == "3.1.0"
    validate(schema)
    actual_paths = {
        context.path
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and context.original_route.include_in_schema
    }
    assert set(schema["paths"]) == actual_paths
    assert "HTTPBearer" in schema["components"]["securitySchemes"]

    required = {
        ("post", "/api/v1/auth/login"),
        ("post", "/api/v1/auth/refresh"),
        ("post", "/api/v1/auth/logout"),
        ("get", "/api/v1/users/me"),
        ("post", "/api/v1/admin/users"),
        ("patch", "/api/v1/admin/users/{user_id}"),
        ("post", "/api/v1/tasks"),
        ("post", "/api/v1/tasks/{task_id}/versions"),
        ("post", "/api/v1/task-versions/{version_id}/publish"),
        ("post", "/api/v1/variants"),
        ("post", "/api/v1/variants/{variant_id}/publish"),
        ("post", "/api/v1/exams"),
        ("post", "/api/v1/exams/{exam_id}/assignments"),
        ("post", "/api/v1/exams/{exam_id}/sessions"),
        ("get", "/api/v1/sessions/{session_id}"),
        ("put", "/api/v1/sessions/{session_id}/answers/{task_id}"),
        ("post", "/api/v1/sessions/{session_id}/audio/{task_id}/play"),
        ("post", "/api/v1/sessions/{session_id}/proctor-events"),
        ("post", "/api/v1/sessions/{session_id}/submit"),
        ("get", "/api/v1/reviews/queue"),
        ("post", "/api/v1/reviews/{review_id}/claim"),
        ("put", "/api/v1/reviews/{review_id}/scores"),
        ("post", "/api/v1/reviews/{review_id}/finalize"),
        ("get", "/api/v1/results/{session_id}"),
        ("post", "/api/v1/results/{session_id}/recalculate"),
        ("get", "/api/v1/audit/events"),
        ("get", "/api/v1/exports/results"),
        ("get", "/api/v1/health/live"),
        ("get", "/api/v1/health/ready"),
        ("get", "/api/v1/metrics"),
        ("post", "/api/v1/kim-imports/validate"),
        ("post", "/api/v1/kim-imports"),
        ("get", "/api/v1/kim-imports/{job_id}"),
        ("get", "/api/v1/kim-packages"),
        ("get", "/api/v1/kim-packages/{package_id}/versions"),
    }
    assert required <= {(method, path) for path, item in schema["paths"].items() for method in item}
    for path in (
        "/api/v1/users/me",
        "/api/v1/reviews/queue",
        "/api/v1/results/{session_id}",
        "/api/v1/exports/results",
    ):
        assert schema["paths"][path]["get"].get("security")
