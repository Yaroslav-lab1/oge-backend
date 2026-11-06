"""Content invariants are tested through the same service functions as the API."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.errors import ApiError
from app.models import *  # noqa: F403
from app.models.auth import Role, User, UserRole
from app.models.content import Exam, ExamAssignment, ExamVariantTask, TaskVersion
from app.routers.content import (
    AssignmentCreate,
    ExamCreate,
    ScoringRuleCreate,
    TaskCreate,
    TaskVersionCreate,
    VariantCreate,
    create_assignments,
    create_exam,
    create_scoring_rule,
    create_task,
    create_task_version,
    create_variant,
    publish_task_version,
    publish_variant,
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def actor(db):
    user = User(login="manager", password_hash="synthetic")
    db.add(user)
    await db.commit()
    return user


async def published_variant(db, user):
    task = await create_task(TaskCreate(subject="MATH", code="GEO-1"), db, user)
    version = await create_task_version(
        task["id"],
        TaskVersionCreate(
            body={"text": "Синтетическая геометрическая задача"},
            answerType="NUMBER",
            key={"value": "7"},
            maxScore=2,
            geometry=True,
        ),
        db,
        user,
    )
    await publish_task_version(version["id"], db, user)
    variant = await create_variant(
        VariantCreate(subject="MATH", taskVersionIds=[version["id"]]), db, user
    )
    await publish_variant(variant["id"], db, user)
    return task, version, variant


@pytest.mark.asyncio
async def test_published_snapshot_and_variant_order(db):
    user = await actor(db)
    _, version, variant = await published_variant(db, user)
    saved = await db.get(TaskVersion, version["id"])
    assert saved.status == "PUBLISHED"
    assert saved.body == {
        "text": "Синтетическая геометрическая задача",
        "audioPlayLimit": 2,
    }
    mapping = await db.scalar(
        select(ExamVariantTask).where(ExamVariantTask.variant_id == variant["id"])
    )
    assert mapping.task_version_id == version["id"]
    assert mapping.ordinal == 1
    with pytest.raises(ApiError) as exc:
        await publish_task_version(version["id"], db, user)
    assert exc.value.status_code == 409
    saved.body = {"text": "Незаконное изменение"}
    with pytest.raises(ValueError, match="immutable"):
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_unpublished_task_cannot_enter_variant(db):
    user = await actor(db)
    task = await create_task(TaskCreate(subject="PHYS", code="P-1"), db, user)
    version = await create_task_version(
        task["id"],
        TaskVersionCreate(
            body={"text": "Учебная задача"},
            answerType="TEXT",
            key={"value": "ответ"},
            maxScore=1,
        ),
        db,
        user,
    )
    with pytest.raises(ApiError) as exc:
        await create_variant(
            VariantCreate(subject="PHYS", taskVersionIds=[version["id"]]), db, user
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_exam_binds_specific_scoring_rule_version(db):
    user = await actor(db)
    _, _, variant = await published_variant(db, user)
    rule = await create_scoring_rule(
        ScoringRuleCreate(
            subject="MATH",
            effectiveFrom="2020-01-01T00:00:00Z",
            thresholds={"3": 1, "4": 2, "5": 3},
            maxPrimary=3,
        ),
        db,
        user,
    )
    assert rule["geometryMin"] == 2
    exam = await create_exam(
        ExamCreate(
            variantId=variant["id"],
            scoringRuleId=rule["id"],
            opensAt="2030-01-01T09:00:00Z",
            closesAt="2030-01-01T13:00:00Z",
            durationSeconds=9000,
        ),
        db,
        user,
    )
    saved = await db.get(Exam, exam["id"])
    assert saved.scoring_rule_id == rule["id"]
    assert saved.opens_at.replace(tzinfo=UTC) == datetime(2030, 1, 1, 9, tzinfo=UTC)


@pytest.mark.asyncio
async def test_new_exam_selects_newest_effective_rule_and_old_exam_keeps_snapshot(db):
    user = await actor(db)
    _, _, variant = await published_variant(db, user)
    first_rule = await create_scoring_rule(
        ScoringRuleCreate(
            subject="MATH",
            effectiveFrom="2020-01-01T00:00:00Z",
            thresholds={"3": 1, "4": 2, "5": 3},
            maxPrimary=3,
        ),
        db,
        user,
    )
    exam_data = {
        "variantId": variant["id"],
        "opensAt": "2030-01-01T09:00:00Z",
        "closesAt": "2030-01-01T13:00:00Z",
        "durationSeconds": 9000,
    }
    old_exam = await create_exam(ExamCreate(**exam_data), db, user)
    assert old_exam["scoringRuleId"] == first_rule["id"]

    new_rule = await create_scoring_rule(
        ScoringRuleCreate(
            subject="MATH",
            effectiveFrom="2029-01-01T00:00:00Z",
            thresholds={"3": 1, "4": 2, "5": 3},
            maxPrimary=3,
        ),
        db,
        user,
    )
    new_exam = await create_exam(ExamCreate(**exam_data), db, user)
    assert new_exam["scoringRuleId"] == new_rule["id"]
    assert (await db.get(Exam, old_exam["id"])).scoring_rule_id == first_rule["id"]

    with pytest.raises(ApiError) as exc:
        await create_exam(ExamCreate(**exam_data, scoringRuleId=first_rule["id"]), db, user)
    assert exc.value.code == "RULE_NOT_LATEST"


@pytest.mark.asyncio
async def test_assignment_requires_student_role_and_is_unique(db):
    user = await actor(db)
    _, _, variant = await published_variant(db, user)
    rule = await create_scoring_rule(
        ScoringRuleCreate(
            subject="MATH",
            effectiveFrom="2020-01-01T00:00:00Z",
            thresholds={"3": 1, "4": 2, "5": 3},
            maxPrimary=3,
        ),
        db,
        user,
    )
    exam = await create_exam(
        ExamCreate(
            variantId=variant["id"],
            scoringRuleId=rule["id"],
            opensAt="2030-01-01T09:00:00Z",
            closesAt="2030-01-01T13:00:00Z",
            durationSeconds=9000,
        ),
        db,
        user,
    )
    student = User(login="student", password_hash="synthetic")
    role = Role(name="STUDENT")
    db.add_all([student, role])
    await db.flush()
    with pytest.raises(ApiError) as exc:
        await create_assignments(exam["id"], AssignmentCreate(studentIds=[student.id]), db, user)
    assert exc.value.status_code == 422
    db.add(UserRole(user_id=student.id, role_id=role.id))
    await db.commit()
    result = await create_assignments(
        exam["id"], AssignmentCreate(studentIds=[student.id]), db, user
    )
    assert len(result["assignments"]) == 1
    with pytest.raises(ApiError) as exc:
        await create_assignments(exam["id"], AssignmentCreate(studentIds=[student.id]), db, user)
    assert exc.value.status_code == 409
    count = await db.scalar(select(func.count()).select_from(ExamAssignment))
    assert count == 1


@given(
    max_score=st.integers(min_value=2, max_value=100),
    a=st.integers(min_value=0, max_value=100),
    b=st.integers(min_value=0, max_value=100),
    c=st.integers(min_value=0, max_value=100),
)
def test_monotone_rule_validation(max_score, a, b, c):
    values = sorted([a % (max_score + 1), b % (max_score + 1), c % (max_score + 1)])
    rule = ScoringRuleCreate(
        subject="MATH",
        effectiveFrom="2020-01-01T00:00:00Z",
        thresholds=dict(zip(("3", "4", "5"), values, strict=True)),
        maxPrimary=max_score,
    )
    assert rule.thresholds["3"] <= rule.thresholds["4"] <= rule.thresholds["5"]


def test_bad_rule_and_naive_dates_rejected():
    with pytest.raises(ValidationError):
        ScoringRuleCreate(
            subject="MATH",
            effectiveFrom="2020-01-01T00:00:00Z",
            thresholds={"3": 2, "4": 1, "5": 3},
            maxPrimary=3,
        )
    with pytest.raises(ValidationError):
        ExamCreate(
            variantId="v",
            scoringRuleId="r",
            opensAt="2030-01-01T09:00:00",
            closesAt="2030-01-01T13:00:00Z",
            durationSeconds=3600,
        )


@pytest.mark.parametrize("limit", [0, 11, True, "3", 1.5])
def test_invalid_audio_play_limit_rejected_when_creating_version(limit):
    with pytest.raises(ValidationError):
        TaskVersionCreate(
            body={"text": "Учебная задача", "audioPlayLimit": limit},
            answerType="TEXT",
            key={"value": "ответ"},
            maxScore=1,
        )


def test_valid_audio_play_limit_is_materialized_in_version_body():
    for limit in (1, 3):
        data = TaskVersionCreate(
            body={"text": "Учебная задача", "audioPlayLimit": limit},
            answerType="TEXT",
            key={"value": "ответ"},
            maxScore=1,
        )
        assert data.body["audioPlayLimit"] == limit
