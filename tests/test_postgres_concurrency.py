"""Run against a disposable migrated PostgreSQL database in CI."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import utcnow
from app.errors import ApiError
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
from app.models.review import ResultVersion, ReviewAssignment
from app.routers.review import ScoresBody, claim_review, finalize_review, save_scores
from app.session import save_answer, start_session, submit_session


@pytest.mark.asyncio
async def test_postgresql_serializes_attempts_answers_and_independent_reviews():
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL is only set for the disposable CI database")
    assert url.startswith("postgresql+asyncpg://")
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex
    async with maker() as db:
        users = [User(login=f"pg-{suffix}-{n}", password_hash="synthetic") for n in range(3)]
        task = Task(subject="MATH", code=f"pg-{suffix}")
        variant = ExamVariant(subject="MATH", status="PUBLISHED")
        rule = ScoringRuleVersion(
            subject="MATH",
            version=100000 + int(suffix[:6], 16),
            effective_from=utcnow() - timedelta(days=1),
            thresholds={"3": 1, "4": 2, "5": 2},
            max_primary=2,
            geometry_min=0,
            discrepancy_threshold=1,
        )
        db.add_all([*users, task, variant, rule])
        await db.flush()
        version = TaskVersion(
            task_id=task.id,
            version=1,
            status="PUBLISHED",
            body={"text": "Synthetic essay"},
            answer_type="ESSAY",
            key={},
            criteria={"C1": 2},
            max_score=2,
        )
        exam = Exam(
            variant_id=variant.id,
            scoring_rule_id=rule.id,
            opens_at=utcnow() - timedelta(minutes=1),
            closes_at=utcnow() + timedelta(hours=1),
            duration_seconds=1800,
        )
        db.add_all([version, exam])
        await db.flush()
        db.add(ExamVariantTask(variant_id=variant.id, task_version_id=version.id, ordinal=1))
        db.add(ExamAssignment(exam_id=exam.id, student_id=users[0].id))
        await db.commit()

    async def start():
        async with maker() as db:
            return await start_session(db, exam.id, users[0].id)

    first, second = await asyncio.gather(start(), start())
    assert first["id"] == second["id"]
    session_id = first["id"]

    async def save(key):
        async with maker() as db:
            try:
                return await save_answer(db, session_id, version.id, users[0].id, key, 0, key)
            except ApiError as error:
                return error.code

    outcomes = await asyncio.gather(save("writer-a"), save("writer-b"))
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    assert "REVISION_CONFLICT" in outcomes
    async with maker() as db:
        await submit_session(db, session_id, users[0].id)
        review_ids = list(
            await db.scalars(
                select(ReviewAssignment.id)
                .where(ReviewAssignment.session_id == session_id)
                .order_by(ReviewAssignment.round)
            )
        )

    async def claim(review_id, expert):
        async with maker() as db:
            try:
                return await claim_review(review_id, db, expert)
            except ApiError as error:
                return error.status_code

    claims = await asyncio.gather(*(claim(review_id, users[1]) for review_id in review_ids))
    assert sum(isinstance(value, dict) for value in claims) == 1
    assert 403 in claims
    owners = []
    for review_id, claimed in zip(review_ids, claims, strict=True):
        owner = users[1] if isinstance(claimed, dict) else users[2]
        if owner is users[2]:
            assert isinstance(await claim(review_id, owner), dict)
        owners.append(owner)
        async with maker() as db:
            await save_scores(review_id, ScoresBody(scores={"C1": 2}), db, owner)

    async def finish(review_id, expert):
        async with maker() as db:
            return await finalize_review(review_id, db, expert)

    await asyncio.gather(*(finish(r, u) for r, u in zip(review_ids, owners, strict=True)))
    async with maker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ResultVersion)
                .where(ResultVersion.session_id == session_id)
            )
            == 1
        )
    await engine.dispose()
