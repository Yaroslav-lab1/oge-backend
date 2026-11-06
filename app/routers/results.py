"""Published results, recalculation, audit and anonymized BI read model."""

import hashlib
import hmac
from collections import Counter
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth import get_current_user, require_roles, role_names
from app.config import settings
from app.db import get_db
from app.errors import ApiError, forbidden, not_found
from app.models.auth import User
from app.models.content import Exam, ExamAssignment, Task, TaskVersion
from app.models.review import AuditEvent, ResultVersion
from app.models.session import ExamSession
from app.rate_limit import enforce_rate_limit
from app.scoring import create_result_if_ready

router = APIRouter(tags=["results", "audit", "analytics"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
Admin = Annotated[User, Depends(require_roles("ADMIN"))]
Auditor = Annotated[User, Depends(require_roles("AUDITOR"))]
BI = Annotated[User, Depends(require_roles("BI_SERVICE"))]


class RecalculateBody(BaseModel):
    reason: str = Field(min_length=3, max_length=255)
    scoringRuleId: str | None = None


async def _can_view(db: AsyncSession, session_id: str, user: User) -> ExamSession:
    session = await db.get(ExamSession, session_id)
    if session is None:
        raise not_found("Результат не найден")
    assignment = await db.get(ExamAssignment, session.assignment_id)
    roles = await role_names(db, user.id)
    if assignment is None or (assignment.student_id != user.id and "ADMIN" not in roles):
        raise forbidden("Чужой результат недоступен")
    return session


def _result_data(item: ResultVersion, checking_status: str = "COMPLETED") -> dict:
    return {
        "id": item.id,
        "sessionId": item.session_id,
        "version": item.version,
        "primaryScore": item.primary_score,
        "maxScore": item.max_score,
        "grade": item.grade,
        "checkingStatus": checking_status,
        "reason": item.reason,
        "breakdown": item.breakdown,
        "scoringRuleId": item.rule_id,
        "scoringRuleVersion": item.rule_version,
        "publishedAt": item.published_at,
        "createdAt": item.created_at,
    }


@router.get("/results/{session_id}", summary="Получить опубликованный собственный результат")
async def get_result(session_id: str, db: Db, user: CurrentUser) -> dict:
    session = await _can_view(db, session_id, user)
    result = await db.scalar(
        select(ResultVersion)
        .where(ResultVersion.session_id == session_id, ResultVersion.published_at.is_not(None))
        .order_by(ResultVersion.version.desc())
    )
    if result is None:
        raise not_found("Опубликованный результат не найден")
    return _result_data(result, session.status)


@router.get("/results/{session_id}/history", summary="История опубликованных версий результата")
async def result_history(session_id: str, db: Db, user: CurrentUser) -> dict:
    session = await _can_view(db, session_id, user)
    results = (
        await db.scalars(
            select(ResultVersion)
            .where(ResultVersion.session_id == session_id, ResultVersion.published_at.is_not(None))
            .order_by(ResultVersion.version)
        )
    ).all()
    return {"items": [_result_data(item, session.status) for item in results]}


@router.post("/results/{session_id}/recalculate", summary="Создать новую версию результата")
async def recalculate_result(session_id: str, body: RecalculateBody, db: Db, admin: Admin) -> dict:
    existing = await db.scalar(
        select(ResultVersion).where(ResultVersion.session_id == session_id).limit(1)
    )
    if existing is None:
        raise not_found("Исходный результат не найден")
    result = await create_result_if_ready(
        db, session_id, force=True, rule_override_id=body.scoringRuleId
    )
    if result is None:
        raise ApiError(409, "RESULT_NOT_READY", "Проверка работы не завершена")
    record_audit(
        db,
        "RESULT_RECALCULATED",
        "ResultVersion",
        result.id,
        actor_id=admin.id,
        context={"reason": body.reason, "version": result.version},
    )
    await db.commit()
    return _result_data(result)


@router.get("/audit/events", summary="Читать события аудита")
async def audit_events(
    db: Db,
    _auditor: Auditor,
    action: str | None = None,
    actor_id: str | None = None,
    object_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    statement = (
        select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)
    )
    if action:
        statement = statement.where(AuditEvent.action == action)
    if actor_id:
        statement = statement.where(AuditEvent.actor_id == actor_id)
    if object_id:
        statement = statement.where(AuditEvent.object_id == object_id)
    for boundary in (since, until):
        if boundary is not None and boundary.tzinfo is None:
            raise ApiError(422, "TIMEZONE_REQUIRED", "Укажите часовой пояс периода")
    if since:
        statement = statement.where(AuditEvent.created_at >= since)
    if until:
        statement = statement.where(AuditEvent.created_at <= until)
    events = (await db.scalars(statement)).all()
    return {
        "items": [
            {
                "id": item.id,
                "actorId": item.actor_id,
                "action": item.action,
                "objectType": item.object_type,
                "objectId": item.object_id,
                "context": item.context,
                "correlationId": item.correlation_id,
                "createdAt": item.created_at,
            }
            for item in events
        ]
    }


def _pseudonym(session_id: str) -> str:
    return hmac.new(settings.jwt_secret.encode(), session_id.encode(), hashlib.sha256).hexdigest()[
        :24
    ]


@router.get("/exports/results", summary="Выгрузить обезличенные результаты")
async def export_results(db: Db, _bi: BI) -> dict:
    await enforce_rate_limit(db, "export", _bi.id)
    results = (
        await db.scalars(
            select(ResultVersion)
            .where(ResultVersion.published_at.is_not(None))
            .order_by(ResultVersion.session_id, ResultVersion.version.desc())
        )
    ).all()
    latest: dict[str, ResultVersion] = {}
    for item in results:
        latest.setdefault(item.session_id, item)
    rows = []
    for result in latest.values():
        session = await db.get(ExamSession, result.session_id)
        assignment = await db.get(ExamAssignment, session.assignment_id) if session else None
        exam = await db.get(Exam, assignment.exam_id) if assignment else None
        if exam is None:
            continue
        tasks = [await db.get(TaskVersion, item["taskVersionId"]) for item in result.breakdown]
        topics = [
            task.body.get("topicId") for task in tasks if task and isinstance(task.body, dict)
        ]
        counts = Counter(topic for topic in topics if topic is not None)
        difficulties = [
            float(task.body["difficulty"])
            for task in tasks
            if task
            and isinstance(task.body, dict)
            and isinstance(task.body.get("difficulty"), int | float)
        ]
        items = []
        for part, task in zip(result.breakdown, tasks, strict=True):
            parent = await db.get(Task, task.task_id) if task else None
            topic = task.body.get("topicId") if task and isinstance(task.body, dict) else None
            items.append(
                {
                    "taskCode": parent.code if parent else None,
                    "topicId": topic,
                    "difficulty": task.body.get("difficulty")
                    if task and isinstance(task.body, dict)
                    else None,
                    "score": part["score"],
                    "maxScore": part["maxScore"],
                }
            )
        rows.append(
            {
                "respondent": _pseudonym(result.session_id),
                "subject": next(
                    (
                        task.subject
                        for task in [await db.get(Task, t.task_id) for t in tasks if t]
                        if task
                    ),
                    None,
                ),
                "primaryScore": result.primary_score,
                "maxScore": result.max_score,
                "grade": result.grade,
                "scoringRuleVersion": result.rule_version,
                "totalDifficulty": sum(difficulties) if difficulties else None,
                "averageDifficulty": sum(difficulties) / len(difficulties)
                if difficulties
                else None,
                "topicRepeats": {str(topic): count for topic, count in counts.items() if count > 1},
                "items": items,
            }
        )
    return {"items": rows, "count": len(rows)}
