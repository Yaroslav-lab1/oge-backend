"""Anonymized, leased double review and senior adjudication."""

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth import require_roles
from app.db import get_db, utcnow
from app.errors import ApiError, conflict, forbidden, not_found
from app.models.auth import User
from app.models.content import TaskVersion
from app.models.review import ReviewAssignment, ReviewDispute, ReviewScore
from app.models.session import Answer
from app.scoring import create_result_if_ready

router = APIRouter(tags=["review"])
Db = Annotated[AsyncSession, Depends(get_db)]
Expert = Annotated[User, Depends(require_roles("EXPERT"))]
Senior = Annotated[User, Depends(require_roles("SENIOR_EXPERT"))]


class ScoresBody(BaseModel):
    scores: dict[str, int] = Field(min_length=1)
    comments: dict[str, Annotated[str, Field(max_length=2000)]] = Field(default_factory=dict)


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scores: dict[str, int] = Field(min_length=1)


def _criterion_limits(criteria: Any) -> dict[str, int]:
    source = criteria.get("criteria", criteria) if isinstance(criteria, dict) else criteria
    if isinstance(source, list):
        result: dict[str, int] = {}
        for item in source:
            if not isinstance(item, dict):
                continue
            code = item.get("code", item.get("id"))
            maximum = item.get("maxScore", item.get("max_score"))
            if isinstance(code, str) and code.strip() and type(maximum) is int and maximum >= 0:
                result[code] = maximum
        return result
    if isinstance(source, dict):
        result = {}
        for code, value in source.items():
            maximum = (
                value.get("maxScore", value.get("max_score")) if isinstance(value, dict) else value
            )
            if isinstance(code, str) and code.strip() and type(maximum) is int and maximum >= 0:
                result[code] = maximum
        return result
    return {}


async def _owned_review(db: AsyncSession, review_id: str, expert_id: str) -> ReviewAssignment:
    review = await db.get(ReviewAssignment, review_id)
    if review is None:
        raise not_found("Проверка не найдена")
    if review.expert_id != expert_id:
        raise forbidden("Проверка назначена другому эксперту")
    if review.status != "CLAIMED":
        raise conflict("REVIEW_NOT_CLAIMED", "Проверка не находится в работе")
    deadline = review.lease_until
    if (
        deadline is None
        or (deadline.replace(tzinfo=utcnow().tzinfo) if deadline.tzinfo is None else deadline)
        <= utcnow()
    ):
        raise conflict("REVIEW_LEASE_EXPIRED", "Срок назначения проверки истёк")
    return review


@router.get("/reviews/queue", summary="Обезличенная очередь экспертной проверки")
async def review_queue(db: Db, expert: Expert) -> dict:
    now = utcnow()
    reviews = (
        await db.scalars(
            select(ReviewAssignment)
            .where(
                or_(
                    ReviewAssignment.status == "PENDING",
                    (ReviewAssignment.status == "CLAIMED") & (ReviewAssignment.lease_until < now),
                )
            )
            .order_by(ReviewAssignment.created_at)
            .limit(100)
        )
    ).all()
    items = []
    for review in reviews:
        prior = await db.scalar(
            select(ReviewAssignment.id).where(
                ReviewAssignment.session_id == review.session_id,
                ReviewAssignment.task_version_id == review.task_version_id,
                ReviewAssignment.expert_id == expert.id,
            )
        )
        if prior:
            continue
        task = await db.get(TaskVersion, review.task_version_id)
        answer = await db.scalar(
            select(Answer).where(
                Answer.session_id == review.session_id,
                Answer.task_version_id == review.task_version_id,
            )
        )
        if task is not None:
            items.append(
                {
                    "id": review.id,
                    "round": review.round,
                    "task": task.body,
                    "answer": answer.raw if answer else None,
                    "criteria": task.criteria,
                    "maxScore": task.max_score,
                }
            )
    return {"items": items}


@router.post("/reviews/{review_id}/claim", summary="Атомарно назначить проверку эксперту")
async def claim_review(review_id: str, db: Db, expert: Expert) -> dict:
    review = await db.get(ReviewAssignment, review_id)
    if review is None:
        raise not_found("Проверка не найдена")
    prior = await db.scalar(
        select(ReviewAssignment.id).where(
            ReviewAssignment.session_id == review.session_id,
            ReviewAssignment.task_version_id == review.task_version_id,
            ReviewAssignment.expert_id == expert.id,
        )
    )
    if prior:
        raise forbidden("Повторная независимая проверка должна выполняться другим экспертом")
    now = utcnow()
    result = await db.execute(
        update(ReviewAssignment)
        .where(
            ReviewAssignment.id == review_id,
            or_(
                ReviewAssignment.status == "PENDING",
                (ReviewAssignment.status == "CLAIMED") & (ReviewAssignment.lease_until < now),
            ),
        )
        .values(status="CLAIMED", expert_id=expert.id, lease_until=now + timedelta(minutes=15))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise conflict("REVIEW_ALREADY_CLAIMED", "Проверка уже занята")
    record_audit(db, "REVIEW_CLAIMED", "ReviewAssignment", review_id, actor_id=expert.id)
    await db.commit()
    return {"id": review_id, "leaseUntil": now + timedelta(minutes=15)}


@router.put("/reviews/{review_id}/scores", summary="Сохранить баллы по критериям")
async def save_scores(review_id: str, body: ScoresBody, db: Db, expert: Expert) -> dict:
    review = await _owned_review(db, review_id, expert.id)
    task = await db.get(TaskVersion, review.task_version_id)
    if task is None:
        raise not_found("Задание не найдено")
    limits = _criterion_limits(task.criteria)
    if not limits or set(body.scores) != set(limits):
        raise ApiError(422, "INVALID_CRITERIA", "Нужны баллы по всем критериям")
    if not set(body.comments) <= set(limits):
        raise ApiError(422, "INVALID_CRITERIA", "Комментарий относится к неизвестному критерию")
    if any(not 0 <= score <= limits[code] for code, score in body.scores.items()):
        raise ApiError(422, "SCORE_OUT_OF_RANGE", "Балл критерия вне диапазона")
    if sum(body.scores.values()) > task.max_score:
        raise ApiError(422, "SCORE_OUT_OF_RANGE", "Сумма превышает максимум задания")
    await db.execute(delete(ReviewScore).where(ReviewScore.assignment_id == review_id))
    for code, score in body.scores.items():
        db.add(
            ReviewScore(
                assignment_id=review_id,
                criterion_code=code,
                score=score,
                comment=body.comments.get(code),
            )
        )
    record_audit(db, "REVIEW_SCORES_SAVED", "ReviewAssignment", review_id, actor_id=expert.id)
    await db.commit()
    return {"id": review_id, "totalScore": sum(body.scores.values())}


@router.post("/reviews/{review_id}/finalize", summary="Завершить назначенную проверку")
async def finalize_review(review_id: str, db: Db, expert: Expert) -> dict:
    review = await _owned_review(db, review_id, expert.id)
    count = await db.scalar(
        select(ReviewScore.id).where(ReviewScore.assignment_id == review_id).limit(1)
    )
    if count is None:
        raise conflict("MISSING_SCORES", "Сначала сохраните баллы")
    review.status = "FINALIZED"
    review.finalized_at = utcnow()
    await db.flush()
    result = await create_result_if_ready(db, review.session_id)
    record_audit(db, "REVIEW_FINALIZED", "ReviewAssignment", review_id, actor_id=expert.id)
    await db.commit()
    return {"id": review_id, "status": "FINALIZED", "resultReady": result is not None}


@router.get("/reviews/disputes", summary="Очередь спорных проверок")
async def disputes_queue(db: Db, _senior: Senior) -> dict:
    disputes = (
        await db.scalars(select(ReviewDispute).where(ReviewDispute.status == "PENDING").limit(100))
    ).all()
    items = []
    for item in disputes:
        task = await db.get(TaskVersion, item.task_version_id)
        answer = await db.scalar(
            select(Answer).where(
                Answer.session_id == item.session_id,
                Answer.task_version_id == item.task_version_id,
            )
        )
        reviews = (
            await db.scalars(
                select(ReviewAssignment).where(
                    ReviewAssignment.session_id == item.session_id,
                    ReviewAssignment.task_version_id == item.task_version_id,
                    ReviewAssignment.status == "FINALIZED",
                )
            )
        ).all()
        scores_by_round: dict[int, dict[str, int]] = {}
        for review in reviews:
            scores = (
                await db.scalars(select(ReviewScore).where(ReviewScore.assignment_id == review.id))
            ).all()
            scores_by_round[review.round] = {score.criterion_code: score.score for score in scores}
        items.append(
            {
                "id": item.id,
                "firstScore": item.first_score,
                "secondScore": item.second_score,
                "threshold": item.threshold,
                "taskVersionId": item.task_version_id,
                "task": task.body if task else None,
                "answer": answer.raw if answer else None,
                "criteria": task.criteria if task else None,
                "firstCriteria": scores_by_round.get(1, {}),
                "secondCriteria": scores_by_round.get(2, {}),
            }
        )
    return {"items": items}


@router.post("/reviews/disputes/{dispute_id}/resolve", summary="Решение старшего эксперта")
async def resolve_dispute(dispute_id: str, body: ResolveBody, db: Db, senior: Senior) -> dict:
    dispute = await db.get(ReviewDispute, dispute_id)
    if dispute is None:
        raise not_found("Спор не найден")
    if dispute.status != "PENDING":
        raise conflict("DISPUTE_RESOLVED", "Спор уже разрешён")
    task = await db.get(TaskVersion, dispute.task_version_id)
    if task is None:
        raise not_found("Задание не найдено")
    limits = _criterion_limits(task.criteria)
    if not limits or set(body.scores) != set(limits):
        raise ApiError(422, "INVALID_CRITERIA", "Нужны баллы по всем критериям")
    if any(not 0 <= score <= limits[code] for code, score in body.scores.items()):
        raise ApiError(422, "SCORE_OUT_OF_RANGE", "Балл критерия вне диапазона")
    final_score = sum(body.scores.values())
    if final_score > task.max_score:
        raise ApiError(422, "SCORE_OUT_OF_RANGE", "Сумма превышает максимум задания")
    dispute.status = "RESOLVED"
    dispute.senior_expert_id = senior.id
    dispute.final_score = final_score
    dispute.final_criteria = dict(body.scores)
    dispute.resolved_at = utcnow()
    await db.flush()
    result = await create_result_if_ready(db, dispute.session_id)
    record_audit(db, "REVIEW_DISPUTE_RESOLVED", "ReviewDispute", dispute.id, actor_id=senior.id)
    await db.commit()
    return {
        "id": dispute.id,
        "finalScore": dispute.final_score,
        "scores": dispute.final_criteria,
        "resultReady": result is not None,
    }
