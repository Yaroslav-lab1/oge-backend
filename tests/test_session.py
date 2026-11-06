"""Session invariants across the API's transactional service layer."""

import asyncio
from datetime import timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.session as session_service
from app import main as main_module
from app.db import Base, new_id, utcnow
from app.errors import ApiError
from app.metrics import ANSWER_SAVES, REVISION_CONFLICTS
from app.models.auth import User
from app.models.content import (
    Exam,
    ExamAssignment,
    ExamVariant,
    ExamVariantTask,
    KimPackage,
    KimPackageVersion,
    Resource,
    ScoringRuleVersion,
    Task,
    TaskVersion,
)
from app.models.review import AutoCheckResult, ResultVersion
from app.models.session import Answer, AnswerRevision, AudioGrant, ExamSession, ProctorEvent
from app.routers.session import router
from app.session import (
    add_proctor_event,
    expire_due_sessions,
    play_audio,
    read_session,
    redeem_audio_token,
    save_answer,
    start_session,
    submit_session,
)


@pytest.fixture
async def seeded(request):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        now = utcnow()
        student = User(id=new_id(), login="student", password_hash="test")
        stranger = User(id=new_id(), login="stranger", password_hash="test")
        task = Task(id=new_id(), subject="MATH", code="1")
        package = KimPackage(id=new_id(), subject="MATH", name="synthetic")
        package_version = KimPackageVersion(
            id=new_id(),
            package_id=package.id,
            version=1,
            checksum="a" * 64,
            schema_version="1",
            source_version="test",
            passport={},
        )
        resource = Resource(
            id=new_id(),
            package_version_id=package_version.id,
            path="private/audio.mp3",
            media_type="audio/mpeg",
            checksum="b" * 64,
            content=b"synthetic-audio",
        )
        body = {"text": "Сколько будет один плюс один?"}
        if getattr(request, "param", None) is not None:
            body["audioPlayLimit"] = request.param
        version = TaskVersion(
            id=new_id(),
            task_id=task.id,
            version=1,
            status="PUBLISHED",
            body=body,
            answer_type="SINGLE_CHOICE",
            key={"value": "2"},
            normalization={},
            criteria={},
            max_score=1,
            geometry=False,
            audio_resource_id=resource.id,
        )
        variant = ExamVariant(id=new_id(), subject="MATH", status="PUBLISHED")
        item = ExamVariantTask(variant_id=variant.id, task_version_id=version.id, ordinal=1)
        rule = ScoringRuleVersion(
            id=new_id(),
            subject="MATH",
            version=1,
            effective_from=now - timedelta(days=1),
            thresholds={"3": 1, "4": 1, "5": 1},
            max_primary=1,
            geometry_min=0,
            discrepancy_threshold=1,
        )
        exam = Exam(
            id=new_id(),
            variant_id=variant.id,
            scoring_rule_id=rule.id,
            opens_at=now - timedelta(minutes=1),
            closes_at=now + timedelta(minutes=10),
            duration_seconds=300,
            status="SCHEDULED",
        )
        assignment = ExamAssignment(id=new_id(), exam_id=exam.id, student_id=student.id)
        db.add_all(
            [
                student,
                stranger,
                task,
                package,
                package_version,
                resource,
                version,
                variant,
                item,
                rule,
                exam,
                assignment,
            ]
        )
        await db.commit()
        yield db, student, stranger, exam, version, resource
    await engine.dispose()


@pytest.mark.asyncio
async def test_only_assigned_student_can_start_and_resume(seeded):
    db, student, stranger, exam, version, _resource = seeded
    with pytest.raises(ApiError) as exc:
        await start_session(db, exam.id, stranger.id)
    assert exc.value.status_code == 404

    first = await start_session(db, exam.id, student.id)
    second = await start_session(db, exam.id, student.id)
    assert first["id"] == second["id"]
    assert first["tasks"][0]["taskId"] == version.id
    assert "key" not in first["tasks"][0]
    assert first["status"] == "ACTIVE"
    assert await db.scalar(select(func.count()).select_from(ExamSession)) == 1

    with pytest.raises(ApiError) as exc:
        await read_session(db, first["id"], stranger.id)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_database_allows_only_one_active_attempt_per_assignment(seeded):
    db, student, _stranger, exam, _version, _resource = seeded
    assignment = await db.scalar(
        select(ExamAssignment).where(
            ExamAssignment.exam_id == exam.id, ExamAssignment.student_id == student.id
        )
    )
    assignment_id = assignment.id
    now = utcnow()
    first = ExamSession(
        assignment_id=assignment_id,
        status="ACTIVE",
        started_at=now,
        deadline_at=now + timedelta(minutes=5),
    )
    db.add(first)
    await db.commit()
    db.add(
        ExamSession(
            assignment_id=assignment_id,
            status="ACTIVE",
            started_at=now,
            deadline_at=now + timedelta(minutes=5),
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    await db.refresh(first)
    first.status = "COMPLETED"
    first.submission_reason = "MANUAL"
    first.submitted_at = now
    await db.commit()
    db.add(
        ExamSession(
            assignment_id=assignment_id,
            status="ACTIVE",
            started_at=now,
            deadline_at=now + timedelta(minutes=5),
        )
    )
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(ExamSession)) == 2


@pytest.mark.asyncio
async def test_answer_revision_and_idempotency(seeded):
    db, student, _stranger, exam, version, _resource = seeded
    session_id = (await start_session(db, exam.id, student.id))["id"]
    saves_before = ANSWER_SAVES._value.get()
    conflicts_before = REVISION_CONFLICTS._value.get()
    first = await save_answer(db, session_id, version.id, student.id, "2", 0, "save-1")
    replay = await save_answer(db, session_id, version.id, student.id, "2", 0, "save-1")
    assert first == replay
    assert first["revision"] == 1
    assert ANSWER_SAVES._value.get() == saves_before + 1

    with pytest.raises(ApiError) as exc:
        await save_answer(db, session_id, version.id, student.id, "3", 0, "save-2")
    assert exc.value.code == "REVISION_CONFLICT"
    assert exc.value.details["currentRevision"] == 1
    assert REVISION_CONFLICTS._value.get() == conflicts_before + 1
    with pytest.raises(ApiError) as exc:
        await save_answer(db, session_id, version.id, student.id, "3", 0, "save-1")
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"

    second = await save_answer(db, session_id, version.id, student.id, "3", 1, "save-2")
    assert second["revision"] == 2
    assert ANSWER_SAVES._value.get() == saves_before + 2
    answer = await db.scalar(select(Answer).where(Answer.session_id == session_id))
    history = (
        await db.scalars(
            select(AnswerRevision)
            .where(AnswerRevision.answer_id == answer.id)
            .order_by(AnswerRevision.revision)
        )
    ).all()
    assert [(row.revision, row.raw) for row in history] == [(1, "2"), (2, "3")]


@pytest.mark.asyncio
async def test_submission_scores_once_and_closes_answers(seeded):
    db, student, _stranger, exam, version, _resource = seeded
    session_id = (await start_session(db, exam.id, student.id))["id"]
    await save_answer(db, session_id, version.id, student.id, "2", 0)
    submitted = await submit_session(db, session_id, student.id, "submit-1")
    assert submitted["status"] == "COMPLETED"
    assert submitted["submittedAt"] is not None
    assert (await submit_session(db, session_id, student.id, "submit-1")) == submitted
    assert await db.scalar(select(func.count()).select_from(AutoCheckResult)) == 1
    result = await db.scalar(select(ResultVersion).where(ResultVersion.session_id == session_id))
    assert result.primary_score == 1

    with pytest.raises(ApiError) as exc:
        await save_answer(db, session_id, version.id, student.id, "3", 1)
    assert exc.value.code == "SESSION_CLOSED"


@pytest.mark.asyncio
async def test_audio_is_limited_signed_and_single_use(seeded):
    db, student, _stranger, exam, version, resource = seeded
    session_id = (await start_session(db, exam.id, student.id))["id"]
    first = await play_audio(db, session_id, version.id, student.id, "play-1")
    assert first == await play_audio(db, session_id, version.id, student.id, "play-1")
    second = await play_audio(db, session_id, version.id, student.id, "play-2")
    assert second["playCount"] == 2
    assert second["remainingPlays"] == 0
    assert first["audioUrl"] != second["audioUrl"]
    assert resource.path not in first["audioUrl"]
    assert resource.path not in second["audioUrl"]
    for issued in (first, second):
        token = issued["audioUrl"].split("token=", 1)[1]
        content, media_type = await redeem_audio_token(db, session_id, token)
        assert (content, media_type) == (resource.content, "audio/mpeg")
        with pytest.raises(ApiError) as exc:
            await redeem_audio_token(db, session_id, token)
        assert exc.value.code == "INVALID_AUDIO_TOKEN"
    assert await db.scalar(select(func.count()).select_from(AudioGrant)) == 2
    with pytest.raises(ApiError) as exc:
        await play_audio(db, session_id, version.id, student.id, "play-3")
    assert exc.value.code == "AUDIO_PLAY_LIMIT"


@pytest.mark.parametrize(
    ("seeded", "expected_limit"),
    [(None, 2), (1, 1), (3, 3)],
    indirect=["seeded"],
)
@pytest.mark.asyncio
async def test_audio_limit_is_snapshot_config_and_visible_in_session(seeded, expected_limit):
    db, student, _stranger, exam, version, _resource = seeded
    session = await start_session(db, exam.id, student.id)
    task_view = session["tasks"][0]
    assert task_view["audioPlayLimit"] == expected_limit
    assert task_view["playCount"] == 0
    assert task_view["remainingPlays"] == expected_limit

    for count in range(1, expected_limit + 1):
        played = await play_audio(db, session["id"], version.id, student.id, f"play-{count}")
        assert played["playCount"] == count
        assert played["remainingPlays"] == expected_limit - count
        current = await read_session(db, session["id"], student.id)
        assert current["tasks"][0]["playCount"] == count
        assert current["tasks"][0]["remainingPlays"] == expected_limit - count

    with pytest.raises(ApiError) as exc:
        await play_audio(db, session["id"], version.id, student.id, "excess")
    assert exc.value.code == "AUDIO_PLAY_LIMIT"


def test_audio_stream_is_documented_in_openapi():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    spec = app.openapi()
    operation = spec["paths"]["/api/v1/sessions/{session_id}/audio-stream"]["get"]
    token_parameter = next(p for p in operation["parameters"] if p["name"] == "token")
    assert token_parameter["required"] is True
    assert "Одноразовый" in token_parameter["description"]


@pytest.mark.asyncio
async def test_proctor_event_is_append_only_and_expiry_is_scored(seeded):
    db, student, _stranger, exam, version, _resource = seeded
    session_id = (await start_session(db, exam.id, student.id))["id"]
    event = await add_proctor_event(
        db, session_id, student.id, "TAB_HIDDEN", {"durationMs": 3000}, None, "event-1"
    )
    assert event == await add_proctor_event(
        db, session_id, student.id, "TAB_HIDDEN", {"durationMs": 3000}, None, "event-1"
    )
    assert await db.scalar(select(func.count()).select_from(ProctorEvent)) == 1

    session = await db.get(ExamSession, session_id)
    session.started_at = utcnow() - timedelta(minutes=2)
    session.deadline_at = utcnow() - timedelta(seconds=1)
    await db.commit()
    assert await expire_due_sessions(db) == 1
    await db.refresh(session)
    assert session.status == "COMPLETED"
    assert session.submission_reason == "TIMEOUT"
    assert await expire_due_sessions(db) == 0
    with pytest.raises(ApiError) as exc:
        await save_answer(db, session_id, version.id, student.id, "2", 0)
    assert exc.value.code == "SESSION_CLOSED"


@pytest.mark.asyncio
async def test_lifespan_expires_due_session_without_request(seeded, monkeypatch):
    db, student, _stranger, exam, _version, _resource = seeded
    session_id = (await start_session(db, exam.id, student.id))["id"]
    session = await db.get(ExamSession, session_id)
    assert session is not None
    session.started_at = utcnow() - timedelta(minutes=2)
    session.deadline_at = utcnow() - timedelta(seconds=1)
    await db.commit()

    worker_sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(session_service, "SessionLocal", worker_sessions)
    monkeypatch.setattr(main_module, "EXPIRATION_POLL_SECONDS", 0.01)
    processed = asyncio.Event()
    expire_due_sessions_original = session_service.expire_due_sessions

    async def expire_and_signal(worker_db, *, limit=100):
        count = await expire_due_sessions_original(worker_db, limit=limit)
        if count:
            processed.set()
        return count

    monkeypatch.setattr(session_service, "expire_due_sessions", expire_and_signal)
    async with main_module.app.router.lifespan_context(main_module.app):
        await asyncio.wait_for(processed.wait(), timeout=3)

    async with worker_sessions() as probe:
        state = await probe.get(ExamSession, session_id)
    assert state is not None
    assert state.status == "COMPLETED"
    assert state.submission_reason == "TIMEOUT"


@pytest.mark.asyncio
async def test_lifespan_retries_worker_without_logging_sensitive_error(monkeypatch, caplog):
    attempts = 0
    restarted = asyncio.Event()

    async def one_failure_then_wait(stop, *, interval_seconds):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("secret-bearing database URL")
        restarted.set()
        await stop.wait()

    monkeypatch.setattr(main_module, "run_expiration_worker", one_failure_then_wait)
    monkeypatch.setattr(main_module, "EXPIRATION_POLL_SECONDS", 0.01)
    with caplog.at_level("ERROR", logger="oge"):
        async with main_module.app.router.lifespan_context(main_module.app):
            await asyncio.wait_for(restarted.wait(), timeout=1)

    assert attempts == 2
    assert "expiration_worker_retry error_type=RuntimeError" in caplog.text
    assert "secret-bearing database URL" not in caplog.text
