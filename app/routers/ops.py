import hmac

from fastapi import APIRouter, Header
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text

from app.config import settings
from app.db import SessionLocal
from app.errors import ApiError
from app.metrics import ACTIVE_SESSIONS, DATABASE_UP, REVIEW_QUEUE
from app.models.review import ReviewAssignment
from app.models.session import ExamSession

router = APIRouter(tags=["operations"])


@router.get("/health/live", summary="Проверить работу процесса")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Проверить обязательные зависимости")
async def ready() -> dict[str, str]:
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        DATABASE_UP.set(1)
    except Exception as exc:
        DATABASE_UP.set(0)
        raise ApiError(503, "NOT_READY", "База данных недоступна") from exc
    return {"status": "ready"}


@router.get("/metrics", summary="Метрики Prometheus", response_class=Response)
async def metrics(x_metrics_token: str | None = Header(default=None)) -> Response:
    if not x_metrics_token or not hmac.compare_digest(x_metrics_token, settings.metrics_token):
        raise ApiError(403, "METRICS_FORBIDDEN", "Доступ к метрикам запрещён")
    try:
        async with SessionLocal() as db:
            active = await db.scalar(
                select(func.count()).select_from(ExamSession).where(ExamSession.status == "ACTIVE")
            )
            queue = await db.scalar(
                select(func.count())
                .select_from(ReviewAssignment)
                .where(ReviewAssignment.status == "PENDING")
            )
            ACTIVE_SESSIONS.set(active or 0)
            REVIEW_QUEUE.set(queue or 0)
            DATABASE_UP.set(1)
    except Exception:
        DATABASE_UP.set(0)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
