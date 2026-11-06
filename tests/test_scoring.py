"""Numeric checking and versioned grade invariants."""

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, utcnow
from app.models import *  # noqa: F403
from app.models.auth import User
from app.models.content import (
    Exam,
    ExamAssignment,
    ExamVariant,
    ExamVariantTask,
    ScoringRuleVersion,
    Task,
    TaskVersion,
)
from app.models.review import AutoCheckResult, ResultVersion
from app.models.session import Answer, ExamSession
from app.scoring import check_answer, create_result_if_ready, grade_for_score, process_submission


def rule(subject: str = "MATH", geometry_min: int = 2) -> ScoringRuleVersion:
    return ScoringRuleVersion(
        subject=subject,
        version=1,
        effective_from=utcnow(),
        thresholds={"3": 10, "4": 16, "5": 22},
        max_primary=30,
        geometry_min=geometry_min,
        discrepancy_threshold=1,
    )


@pytest.mark.parametrize("answer", ["12,5", "12.5", "12.50", {"value": "12,50"}])
def test_numeric_comma_dot_and_trailing_zero_are_equivalent(answer):
    score, normalized = check_answer("NUMBER", answer, {"value": "12.5"}, {}, 2)
    assert score == 2
    assert normalized["unit"] == ""


@pytest.mark.parametrize("answer", ["7", "7.0", "07,00", {"value": 7}])
def test_integer_and_decimal_forms_are_equivalent(answer):
    assert check_answer("NUMBER", answer, {"value": "7"}, {}, 1)[0] == 1


def test_physics_unit_is_checked_without_implicit_conversion():
    key = {"value": "12.5", "unit": "kg"}
    assert check_answer("NUMBER", "12,50 kg", key, {}, 2)[0] == 2
    assert check_answer("NUMBER", "12500 g", key, {}, 2)[0] == 0
    assert check_answer("NUMBER", "12.5", key, {}, 2)[0] == 0
    assert check_answer("NUMBER", "NaN kg", key, {}, 2)[0] == 0


def test_text_normalization_does_not_mutate_original_answer():
    answer = "  ПРАВИЛЬНЫЙ   Ответ  "
    score, normalized = check_answer(
        "TEXT", answer, {"value": "правильный ответ"}, {"casefold": True}, 3
    )
    assert score == 3
    assert normalized == "правильный ответ"
    assert answer == "  ПРАВИЛЬНЫЙ   Ответ  "


def test_multiple_choice_rejects_duplicate_options():
    key = {"value": ["A", "B"]}
    assert check_answer("MULTIPLE_CHOICE", ["B", "A"], key, {}, 2)[0] == 2
    assert check_answer("MULTIPLE_CHOICE", ["A", "A", "B"], key, {}, 2)[0] == 0


def test_geometry_boundary_one_and_two():
    scoring = rule()
    assert grade_for_score(29, 30, "MATH", 1, scoring) == (2, "GEOMETRY_MIN_NOT_MET")
    assert grade_for_score(29, 30, "MATH", 2, scoring) == (5, None)
    assert grade_for_score(29, 30, "PHYS", 0, scoring) == (5, None)


def test_invalid_score_or_nonmonotone_scale_is_rejected():
    scoring = rule()
    with pytest.raises(ValueError, match="outside range"):
        grade_for_score(31, 30, "MATH", 2, scoring)
    scoring.thresholds = {"3": 10, "4": 9, "5": 22}
    with pytest.raises(ValueError, match="not monotonic"):
        grade_for_score(15, 30, "MATH", 2, scoring)


@given(st.integers(min_value=0, max_value=29))
def test_grade_never_decreases_when_primary_score_increases(primary: int):
    scoring = rule()
    before = grade_for_score(primary, 30, "MATH", 2, scoring)[0]
    after = grade_for_score(primary + 1, 30, "MATH", 2, scoring)[0]
    assert 2 <= before <= after <= 5


@given(st.integers(min_value=-1_000_000, max_value=1_000_000))
def test_numeric_equivalent_forms_keep_score(value: int):
    key = {"value": str(value)}
    assert check_answer("NUMBER", f"{value},0", key, {}, 2)[0] == 2


@pytest.mark.asyncio
async def test_submission_is_idempotent_and_recalculation_preserves_result_versions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        student = User(login="student", password_hash="synthetic")
        task = Task(subject="MATH", code="GEO-1")
        scoring = ScoringRuleVersion(
            subject="MATH",
            version=1,
            effective_from=utcnow(),
            thresholds={"3": 1, "4": 2, "5": 3},
            max_primary=2,
            geometry_min=2,
            discrepancy_threshold=1,
        )
        variant = ExamVariant(subject="MATH", status="PUBLISHED")
        db.add_all([student, task, scoring, variant])
        await db.flush()
        version = TaskVersion(
            task_id=task.id,
            version=1,
            status="PUBLISHED",
            body={"text": "Синтетическая геометрия"},
            answer_type="NUMBER",
            key={"value": "7"},
            normalization={},
            criteria={},
            max_score=2,
            geometry=True,
        )
        db.add(version)
        await db.flush()
        db.add(ExamVariantTask(variant_id=variant.id, task_version_id=version.id, ordinal=1))
        exam = Exam(
            variant_id=variant.id,
            scoring_rule_id=scoring.id,
            opens_at=utcnow() - timedelta(hours=1),
            closes_at=utcnow() + timedelta(hours=1),
            duration_seconds=3600,
        )
        db.add(exam)
        await db.flush()
        assignment = ExamAssignment(exam_id=exam.id, student_id=student.id)
        db.add(assignment)
        await db.flush()
        session = ExamSession(
            assignment_id=assignment.id,
            status="SUBMITTED",
            started_at=utcnow() - timedelta(minutes=5),
            deadline_at=utcnow() + timedelta(minutes=55),
        )
        db.add(session)
        await db.flush()
        answer = Answer(session_id=session.id, task_version_id=version.id, raw="7.0", revision=1)
        db.add(answer)
        await db.commit()

        await process_submission(db, session.id)
        await db.commit()
        await process_submission(db, session.id)
        await db.commit()
        assert answer.raw == "7.0"
        assert await db.scalar(select(func.count()).select_from(AutoCheckResult)) == 1
        assert await db.scalar(select(func.count()).select_from(ResultVersion)) == 1
        original = await db.scalar(
            select(ResultVersion).where(ResultVersion.session_id == session.id)
        )
        assert original.grade == 4  # score 2 and geometry threshold 2
        assert original.rule_version == 1

        newer = await create_result_if_ready(db, session.id, force=True)
        await db.commit()
        assert newer.version == 2
        assert newer.id != original.id
        assert original.version == 1
        assert await db.scalar(select(func.count()).select_from(ResultVersion)) == 2
    await engine.dispose()
