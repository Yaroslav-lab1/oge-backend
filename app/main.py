import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from app.audit import current_correlation_id, reset_correlation_id, set_correlation_id
from app.body_limit import BodyLimitMiddleware
from app.config import settings
from app.errors import ApiError
from app.metrics import LATENCY, REQUESTS
from app.routers import admin, auth, content, kim, ops, results, review, session
from app.session import run_expiration_worker


class JsonFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
                "level": record.levelname,
                "event": record.getMessage(),
                "correlationId": current_correlation_id(),
            },
            ensure_ascii=False,
        )


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
logger = logging.getLogger("oge")

EXPIRATION_POLL_SECONDS = 5.0
EXPIRATION_SHUTDOWN_SECONDS = 10.0


async def _supervise_expiration_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await run_expiration_worker(stop, interval_seconds=EXPIRATION_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Exception messages may contain database URLs or request data.
            logger.error("expiration_worker_retry error_type=%s", type(exc).__name__)
        if not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=EXPIRATION_POLL_SECONDS)
            except TimeoutError:
                pass


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    stop = asyncio.Event()
    worker = asyncio.create_task(_supervise_expiration_worker(stop), name="session-expiration")
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(worker, timeout=EXPIRATION_SHUTDOWN_SECONDS)
        except TimeoutError:
            logger.error("expiration_worker_shutdown_timeout")
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


app = FastAPI(
    title="Учебная платформа ОГЭ — backend API",
    version="1.0.0",
    description="Версионируемый API для учебных экзаменов, проверки и импорта КИМ.",
    openapi_version="3.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["GET", "POST", "PUT", "PATCH"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Correlation-ID",
        "X-Metrics-Token",
    ],
)
app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes)


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)
    correlationId: str


COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse, "description": description}
    for code, description in {
        400: "Некорректный запрос",
        401: "Требуется авторизация",
        403: "Доступ запрещён",
        404: "Объект не найден",
        409: "Конфликт состояния или ревизии",
        413: "Превышен лимит размера запроса",
        422: "Ошибка валидации",
        429: "Превышен лимит попыток",
        500: "Внутренняя ошибка сервера",
        503: "Сервис не готов",
    }.items()
}


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    supplied = request.headers.get("X-Correlation-ID")
    try:
        correlation_id = str(UUID(supplied)) if supplied else str(uuid4())
    except ValueError:
        correlation_id = str(uuid4())
    token = set_correlation_id(correlation_id)
    start = time.perf_counter()
    try:
        size = request.headers.get("content-length")
        if size and size.isdecimal() and int(size) > settings.max_request_bytes:
            response = JSONResponse(
                status_code=413,
                content={
                    "code": "REQUEST_TOO_LARGE",
                    "message": "Запрос слишком велик",
                    "details": {},
                    "correlationId": correlation_id,
                },
            )
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                response = await unexpected_error(request, exc)
        response.headers["X-Correlation-ID"] = correlation_id
        route = getattr(request.scope.get("route"), "path", "<unmatched>")
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        LATENCY.labels(request.method, route).observe(time.perf_counter() - start)
        logger.info(
            "http_request method=%s route=%s status=%s", request.method, route, response.status_code
        )
        return response
    finally:
        reset_correlation_id(token)


@app.exception_handler(ApiError)
async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "correlationId": current_correlation_id(),
        },
    )


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": "HTTP_ERROR",
            "message": str(exc.detail),
            "details": {},
            "correlationId": current_correlation_id(),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [
        {"field": ".".join(map(str, error["loc"])), "type": error["type"]} for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "Некорректные данные запроса",
            "details": {"fields": fields},
            "correlationId": current_correlation_id(),
        },
    )


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception type=%s", type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "Внутренняя ошибка сервера",
            "details": {},
            "correlationId": current_correlation_id(),
        },
    )


for router in (
    auth.router,
    admin.router,
    content.router,
    kim.router,
    session.router,
    review.router,
    results.router,
    ops.router,
):
    app.include_router(router, prefix="/api/v1", responses=COMMON_ERRORS)
