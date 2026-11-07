"""Reproducible answer checking and versioned scoring."""

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.errors import ApiError
from app.models.content import (
    Exam,
    ExamAssignment,
    ExamVariantTask,
    ScoringRuleVersion,
    TaskVersion,
)
from app.models.review import (
    AutoCheckResult,
    ResultVersion,
    ReviewAssignment,
    ReviewDispute,
    ReviewScore,
)
from app.models.session import Answer, ExamSession


def _text(value: Any, options: dict[str, Any]) -> str:
    result = unicodedata.normalize("NFKC", str(value))
    if options.get("strip", True):
        result = result.strip()
    if options.get("collapse_whitespace", True):
        result = " ".join(result.split())
    if options.get("casefold", True):
        result = result.casefold()
    return result


def _number_and_unit(value: Any) -> tuple[Decimal, str]:
    if isinstance(value, dict):
        number, unit = value.get("value"), str(value.get("unit", "")).strip()
    else:
        raw = str(value).strip()
        match = re.fullmatch(r"([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*(.*?)", raw)
        if not match:
            raise ValueError("Invalid number")
        number, unit = match.group(1), match.group(2).strip()
    try:
        parsed = Decimal(str(number).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid number") from exc
    if not parsed.is_finite():
        raise ValueError("Invalid number")
    return parsed, unit


def check_answer(
    answer_type: str, raw: Any, key: Any, normalization: dict[str, Any] | None, max_score: int
) -> tuple[int, Any]:
    """Return score and normalized answer. The original answer is never changed."""
    options = normalization or {}
    expected = key.get("value", key.get("answer", key)) if isinstance(key, dict) else key
    if answer_type == "ESSAY":
        raise ValueError("Essay requires expert review")
    if raw is None:
        return 0, None
    try:
        if answer_type == "NUMBER":
            got_number, got_unit = _number_and_unit(raw)
            expected_number, expected_unit = _number_and_unit(
                key if isinstance(key, dict) else expected
            )
            if not expected_unit and isinstance(key, dict):
                expected_unit = str(key.get("unit", "")).strip()
            tolerance = Decimal(str(options.get("tolerance", "0")))
            correct = abs(got_number - expected_number) <= tolerance and got_unit == expected_unit
            return (max_score if correct else 0), {"value": str(got_number), "unit": got_unit}
        if answer_type == "MULTIPLE_CHOICE":
            got_choices = raw if isinstance(raw, list) else [raw]
            want_choices = expected if isinstance(expected, list) else [expected]
            got_values = [_text(item, options) for item in got_choices]
            want_values = [_text(item, options) for item in want_choices]
            correct = len(got_values) == len(set(got_values)) and set(got_values) == set(
                want_values
            )
            return (max_score if correct else 0), sorted(got_values)
        if answer_type in ("SINGLE_CHOICE", "TEXT"):
            got = _text(raw, options)
            if isinstance(expected, list):
                correct = got in {_text(item, options) for item in expected}
            else:
                correct = got == _text(expected, options)
            return (max_score if correct else 0), got
    except (TypeError, ValueError, InvalidOperation):
        return 0, None
    raise ValueError(f"Unsupported answer type: {answer_type}")


def grade_for_score(
    primary_score: int, max_score: int, subject: str, geometry_score: int, rule: ScoringRuleVersion
) -> tuple[int, str | None]:
    if not 0 <= primary_score <= max_score:
        raise ValueError("Primary score outside range")
    thresholds = rule.thresholds or {}
    boundaries = [int(thresholds.get(str(grade), max_score + 1)) for grade in (3, 4, 5)]
    if boundaries != sorted(boundaries) or any(boundary < 0 for boundary in boundaries):
        raise ValueError("Scoring scale is not monotonic")
    grade = 2
    for candidate, boundary in zip((3, 4, 5), boundaries, strict=True):
        if primary_score >= boundary:
            grade = candidate
    if subject == "MATH" and geometry_score < rule.geometry_min:
        return 2, "GEOMETRY_MIN_NOT_MET"
    return grade, None


async def _context(
    db: AsyncSession, session_id: str
) -> tuple[ExamSession, Exam, ScoringRuleVersion, list[TaskVersion]]:
    session = await db.scalar(
        select(ExamSession)
        .where(ExamSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if session is None:
        raise ApiError(404, "SESSION_NOT_FOUND", "Сессия не найдена")
    assignment = await db.get(ExamAssignment, session.assignment_id)
    if assignment is None:
        raise ApiError(500, "BROKEN_ASSIGNMENT", "Нарушена связность экзамена")
    exam = await db.get(Exam, assignment.exam_id)
    if exam is None:
        raise ApiError(500, "BROKEN_EXAM", "Нарушена связность экзамена")
    rule = await db.get(ScoringRuleVersion, exam.scoring_rule_id)
    if rule is None:
        raise ApiError(500, "BROKEN_SCORING_RULE", "Правило оценивания не найдено")
    task_ids = (
        await db.scalars(
            select(ExamVariantTask.task_version_id)
            .where(ExamVariantTask.variant_id == exam.variant_id)
            .order_by(ExamVariantTask.ordinal)
        )
    ).all()
    tasks = [task for task_id in task_ids if (task := await db.get(TaskVersion, task_id))]
    return session, exam, rule, tasks


async def process_submission(db: AsyncSession, session_id: str) -> None:
    """Create checks and review assignments once, inside the submit transaction."""
    session, _exam, rule, tasks = await _context(db, session_id)
    for task in tasks:
        answer = await db.scalar(
            select(Answer).where(Answer.session_id == session_id, Answer.task_version_id == task.id)
        )
        if task.answer_type == "ESSAY":
            for review_round in (1, 2):
                existing = await db.scalar(
                    select(ReviewAssignment).where(
                        ReviewAssignment.session_id == session_id,
                        ReviewAssignment.task_version_id == task.id,
                        ReviewAssignment.round == review_round,
                    )
                )
                if existing is None:
                    db.add(
                        ReviewAssignment(
                            session_id=session_id, task_version_id=task.id, round=review_round
                        )
                    )
            continue
        existing = await db.scalar(
            select(AutoCheckResult).where(
                AutoCheckResult.session_id == session_id,
                AutoCheckResult.task_version_id == task.id,
            )
        )
        if existing is not None:
            continue
        score, normalized = check_answer(
            task.answer_type,
            answer.raw if answer else None,
            task.key,
            task.normalization,
            task.max_score,
        )
        db.add(
            AutoCheckResult(
                session_id=session_id,
                task_version_id=task.id,
                score=score,
                max_score=task.max_score,
                normalized=normalized,
                rule_id=rule.id,
                rule_version=rule.version,
            )
        )
    await db.flush()
    session.status = "CHECKING"
    await create_result_if_ready(db, session_id)


async def create_result_if_ready(
    db: AsyncSession, session_id: str, *, force: bool = False, rule_override_id: str | None = None
) -> ResultVersion | None:
    session, exam, original_rule, tasks = await _context(db, session_id)
    rule = await db.get(ScoringRuleVersion, rule_override_id) if rule_override_id else original_rule
    if rule is None or rule.subject != original_rule.subject:
        raise ApiError(422, "INVALID_SCORING_RULE", "Правило оценивания недоступно для предмета")
    latest = await db.scalar(
        select(ResultVersion)
        .where(ResultVersion.session_id == session_id)
        .order_by(ResultVersion.version.desc())
    )
    if latest is not None and not force:
        return latest
    breakdown: list[dict[str, Any]] = []
    geometry_score = 0
    for task in tasks:
        if task.answer_type != "ESSAY":
            check = await db.scalar(
                select(AutoCheckResult).where(
                    AutoCheckResult.session_id == session_id,
                    AutoCheckResult.task_version_id == task.id,
                )
            )
            if check is None:
                return None
            score = check.score
        else:
            reviews = (
                await db.scalars(
                    select(ReviewAssignment).where(
                        ReviewAssignment.session_id == session_id,
                        ReviewAssignment.task_version_id == task.id,
                        ReviewAssignment.status == "FINALIZED",
                    )
                )
            ).all()
            if len(reviews) != 2:
                return None
            totals = []
            for review in reviews:
                value = await db.scalar(
                    select(func.coalesce(func.sum(ReviewScore.score), 0)).where(
                        ReviewScore.assignment_id == review.id
                    )
                )
                totals.append(int(value or 0))
            dispute = await db.scalar(
                select(ReviewDispute).where(
                    ReviewDispute.session_id == session_id,
                    ReviewDispute.task_version_id == task.id,
                )
            )
            if abs(totals[0] - totals[1]) > rule.discrepancy_threshold:
                if dispute is None:
                    db.add(
                        ReviewDispute(
                            session_id=session_id,
                            task_version_id=task.id,
                            first_score=totals[0],
                            second_score=totals[1],
                            threshold=rule.discrepancy_threshold,
                        )
                    )
                    await db.flush()
                    return None
                if dispute.status != "RESOLVED" or dispute.final_score is None:
                    return None
                score = dispute.final_score
            else:
                score = int(
                    (Decimal(sum(totals)) / 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
        if not 0 <= score <= task.max_score:
            raise ApiError(422, "SCORE_OUT_OF_RANGE", "Балл задания вне допустимого диапазона")
        breakdown.append(
            {
                "taskVersionId": task.id,
                "score": score,
                "maxScore": task.max_score,
                "geometry": bool(task.geometry),
                "answerType": task.answer_type,
            }
        )
        if task.geometry:
            geometry_score += score
    primary = sum(item["score"] for item in breakdown)
    max_score = sum(item["maxScore"] for item in breakdown)
    grade, reason = grade_for_score(primary, max_score, rule.subject, geometry_score, rule)
    result = ResultVersion(
        session_id=session_id,
        version=(latest.version + 1 if latest else 1),
        primary_score=primary,
        max_score=max_score,
        grade=grade,
        reason=reason,
        breakdown=breakdown,
        rule_id=rule.id,
        rule_version=rule.version,
        published_at=utcnow(),
    )
    db.add(result)
    session.status = "COMPLETED"
    await db.flush()
    return result
