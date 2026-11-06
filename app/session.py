"""Transactional exam-session operations.

Every mutating operation locks the session row. PostgreSQL serializes concurrent
requests on that row; database uniqueness constraints remain the final guard.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import settings
from app.db import SessionLocal, utcnow
from app.errors import ApiError, conflict, not_found
from app.metrics import ANSWER_SAVES, REVISION_CONFLICTS
from app.models.content import (
    Exam,
    ExamAssignment,
    ExamVariantTask,
    Resource,
    TaskVersion,
    audio_play_limit,
)
from app.models.session import (
    Answer,
    AnswerRevision,
    AudioGrant,
    AudioPlayback,
    ExamSession,
    IdempotencyRecord,
    ProctorEvent,
)

AUDIO_TOKEN_SECONDS = 60


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def _request_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_key(key: str | None) -> str | None:
    if key is None:
        return None
    if not key or len(key) > 255 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ApiError(422, "INVALID_IDEMPOTENCY_KEY", "Некорректный Idempotency-Key")
    return key


async def _replay(db: AsyncSession, scope: str, key: str | None, request_hash: str) -> dict | None:
    if key is None:
        return None
    record = await db.scalar(
        select(IdempotencyRecord)
        .where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)
        .with_for_update()
    )
    if record is None:
        return None
    if not hmac.compare_digest(record.request_hash, request_hash):
        raise conflict("IDEMPOTENCY_KEY_REUSED", "Ключ уже использован для другого запроса")
    return record.response


def _remember(
    db: AsyncSession, scope: str, key: str | None, request_hash: str, response: dict
) -> None:
    if key is not None:
        db.add(
            IdempotencyRecord(
                scope=scope,
                key=key,
                request_hash=request_hash,
                response=response,
                status_code=200,
            )
        )


async def _owned_session(
    db: AsyncSession, session_id: str, student_id: str, *, lock: bool = False
) -> tuple[ExamSession, Exam]:
    query = (
        select(ExamSession, Exam)
        .join(ExamAssignment, ExamAssignment.id == ExamSession.assignment_id)
        .join(Exam, Exam.id == ExamAssignment.exam_id)
        .where(ExamSession.id == session_id, ExamAssignment.student_id == student_id)
    )
    if lock:
        query = query.with_for_update(of=ExamSession)
    row = (await db.execute(query)).one_or_none()
    if row is None:
        raise not_found("Сессия не найдена")
    return row[0], row[1]


async def _variant_task(db: AsyncSession, variant_id: str, task_id: str) -> TaskVersion:
    task = await db.scalar(
        select(TaskVersion)
        .join(ExamVariantTask, ExamVariantTask.task_version_id == TaskVersion.id)
        .where(ExamVariantTask.variant_id == variant_id, TaskVersion.id == task_id)
    )
    if task is None:
        raise not_found("Задание не входит в вариант экзамена")
    return task


async def _view(db: AsyncSession, session: ExamSession, exam: Exam) -> dict:
    task_rows = (
        await db.execute(
            select(ExamVariantTask.ordinal, TaskVersion)
            .join(TaskVersion, TaskVersion.id == ExamVariantTask.task_version_id)
            .where(ExamVariantTask.variant_id == exam.variant_id)
            .order_by(ExamVariantTask.ordinal)
        )
    ).all()
    answers = (await db.scalars(select(Answer).where(Answer.session_id == session.id))).all()
    by_task = {answer.task_version_id: answer for answer in answers}
    playbacks = (
        await db.scalars(select(AudioPlayback).where(AudioPlayback.session_id == session.id))
    ).all()
    plays_by_task = {playback.task_version_id: playback.play_count for playback in playbacks}
    now = utcnow()
    return {
        "id": session.id,
        "examId": exam.id,
        "assignmentId": session.assignment_id,
        "status": session.status,
        "startedAt": _iso(session.started_at),
        "deadlineAt": _iso(session.deadline_at),
        "submittedAt": _iso(session.submitted_at),
        "remainingSeconds": max(0, math.ceil((_utc(session.deadline_at) - now).total_seconds()))
        if session.status == "ACTIVE"
        else 0,
        "tasks": [
            {
                "taskId": task.id,
                "ordinal": ordinal,
                "body": task.body,
                "answerType": task.answer_type,
                "maxScore": task.max_score,
                "geometry": task.geometry,
                "hasAudio": task.audio_resource_id is not None,
                "audioPlayLimit": audio_play_limit(task.body)
                if task.audio_resource_id is not None
                else 0,
                "playCount": plays_by_task.get(task.id, 0),
                "remainingPlays": max(
                    0,
                    audio_play_limit(task.body) - plays_by_task.get(task.id, 0),
                )
                if task.audio_resource_id is not None
                else 0,
                "answer": {"raw": by_task[task.id].raw, "revision": by_task[task.id].revision}
                if task.id in by_task
                else None,
            }
            for ordinal, task in task_rows
        ],
    }


async def _finalize(
    db: AsyncSession, session: ExamSession, *, expired: bool, now: datetime
) -> None:
    if session.status != "ACTIVE":
        return
    session.status = "EXPIRED" if expired else "SUBMITTED"
    session.submitted_at = now
    session.submission_reason = "TIMEOUT" if expired else "MANUAL"
    await db.flush()
    # Scoring is part of the same transaction as the state transition.
    from app.scoring import process_submission

    await process_submission(db, session.id)
    record_audit(
        db,
        "SESSION_EXPIRED" if expired else "SESSION_SUBMITTED",
        "ExamSession",
        session.id,
        context={"reason": session.submission_reason},
    )


async def _require_active(db: AsyncSession, session: ExamSession, now: datetime) -> None:
    if session.status != "ACTIVE":
        raise conflict("SESSION_CLOSED", "Сессия уже завершена", {"status": session.status})
    if _utc(session.deadline_at) <= now:
        await _finalize(db, session, expired=True, now=now)
        await db.commit()
        raise conflict("SESSION_EXPIRED", "Время экзамена истекло")


async def start_session(db: AsyncSession, exam_id: str, student_id: str) -> dict:
    assignment = await db.scalar(
        select(ExamAssignment)
        .where(ExamAssignment.exam_id == exam_id, ExamAssignment.student_id == student_id)
        .with_for_update()
    )
    if assignment is None:
        raise not_found("Назначение на экзамен не найдено")
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise not_found("Экзамен не найден")
    existing = await db.scalar(
        select(ExamSession).where(ExamSession.assignment_id == assignment.id).with_for_update()
    )
    now = utcnow()
    if existing is not None:
        if existing.status == "ACTIVE" and _utc(existing.deadline_at) <= now:
            await _finalize(db, existing, expired=True, now=now)
            await db.commit()
        if existing.status == "ACTIVE":
            return await _view(db, existing, exam)
        raise conflict("SESSION_CLOSED", "Экзамен уже завершён", {"status": existing.status})
    if exam.status != "SCHEDULED":
        raise conflict("EXAM_UNAVAILABLE", "Экзамен недоступен")
    if not (_utc(exam.opens_at) <= now < _utc(exam.closes_at)):
        raise conflict("EXAM_OUTSIDE_WINDOW", "Экзамен сейчас не проводится")
    deadline = min(now + timedelta(seconds=exam.duration_seconds), _utc(exam.closes_at))
    session = ExamSession(
        assignment_id=assignment.id,
        status="ACTIVE",
        started_at=now,
        deadline_at=deadline,
    )
    db.add(session)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # Another request started this assignment concurrently. Never create a second attempt.
        existing = await db.scalar(
            select(ExamSession).where(ExamSession.assignment_id == assignment.id)
        )
        if existing is not None and existing.status == "ACTIVE":
            return await _view(db, existing, exam)
        raise conflict("SESSION_ALREADY_EXISTS", "Сессия уже создана") from exc
    record_audit(db, "SESSION_STARTED", "ExamSession", session.id, actor_id=student_id)
    await db.commit()
    return await _view(db, session, exam)


async def read_session(db: AsyncSession, session_id: str, student_id: str) -> dict:
    session, exam = await _owned_session(db, session_id, student_id, lock=True)
    now = utcnow()
    if session.status == "ACTIVE" and _utc(session.deadline_at) <= now:
        await _finalize(db, session, expired=True, now=now)
        await db.commit()
    return await _view(db, session, exam)


async def save_answer(
    db: AsyncSession,
    session_id: str,
    task_id: str,
    student_id: str,
    raw: Any,
    expected_revision: int,
    idempotency_key: str | None = None,
) -> dict:
    key = _check_key(idempotency_key)
    session, exam = await _owned_session(db, session_id, student_id, lock=True)
    scope = f"answer:{session.id}:{task_id}"
    request_hash = _request_hash({"raw": raw, "expectedRevision": expected_revision})
    replay = await _replay(db, scope, key, request_hash)
    if replay is not None:
        return replay
    now = utcnow()
    await _require_active(db, session, now)
    await _variant_task(db, exam.variant_id, task_id)
    answer = await db.scalar(
        select(Answer)
        .where(Answer.session_id == session.id, Answer.task_version_id == task_id)
        .with_for_update()
    )
    current_revision = answer.revision if answer is not None else 0
    if current_revision != expected_revision:
        REVISION_CONFLICTS.inc()
        raise conflict(
            "REVISION_CONFLICT",
            "Ответ уже изменён",
            {"currentRevision": current_revision},
        )
    if answer is None:
        answer = Answer(
            session_id=session.id,
            task_version_id=task_id,
            raw=raw,
            revision=1,
            updated_at=now,
        )
        db.add(answer)
        await db.flush()
    else:
        answer.raw = raw
        answer.revision += 1
        answer.updated_at = now
    db.add(
        AnswerRevision(
            answer_id=answer.id,
            revision=answer.revision,
            raw=raw,
            created_at=now,
        )
    )
    response = {
        "taskId": task_id,
        "revision": answer.revision,
        "raw": raw,
        "updatedAt": _iso(now),
    }
    _remember(db, scope, key, request_hash, response)
    record_audit(
        db,
        "ANSWER_SAVED",
        "Answer",
        answer.id,
        actor_id=student_id,
        context={"revision": answer.revision},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict(
            "CONCURRENT_UPDATE", "Параллельное изменение ответа; обновите сессию"
        ) from exc
    ANSWER_SAVES.inc()
    return response


def _sign_audio(session_id: str, expires: int) -> str:
    nonce = secrets.token_urlsafe(18)
    payload = f"{expires}|{nonce}".encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        settings.jwt_secret.encode(),
        b"oge-audio-v1:" + session_id.encode() + b":" + payload,
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _verify_audio(token: str, session_id: str) -> None:
    try:
        encoded, signature = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        expected = hmac.new(
            settings.jwt_secret.encode(),
            b"oge-audio-v1:" + session_id.encode() + b":" + payload,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        expires, nonce = payload.decode().split("|")
        if not nonce or int(expires) < int(utcnow().timestamp()):
            raise ValueError("expired")
    except (ValueError, UnicodeDecodeError, UnicodeEncodeError, binascii.Error) as exc:
        raise ApiError(403, "INVALID_AUDIO_TOKEN", "Недействительная ссылка на аудио") from exc


async def play_audio(
    db: AsyncSession,
    session_id: str,
    task_id: str,
    student_id: str,
    idempotency_key: str | None = None,
) -> dict:
    key = _check_key(idempotency_key)
    session, exam = await _owned_session(db, session_id, student_id, lock=True)
    scope = f"audio:{session.id}:{task_id}"
    request_hash = _request_hash({"taskId": task_id})
    replay = await _replay(db, scope, key, request_hash)
    if replay is not None:
        return replay
    now = utcnow()
    await _require_active(db, session, now)
    task = await _variant_task(db, exam.variant_id, task_id)
    if task.audio_resource_id is None:
        raise not_found("У задания нет аудиоматериала")
    resource = await db.get(Resource, task.audio_resource_id)
    if resource is None or not resource.media_type.startswith("audio/"):
        raise not_found("Аудиоматериал не найден")
    play_limit = audio_play_limit(task.body)
    playback = await db.scalar(
        select(AudioPlayback)
        .where(AudioPlayback.session_id == session.id, AudioPlayback.task_version_id == task_id)
        .with_for_update()
    )
    if playback is None:
        playback = AudioPlayback(session_id=session.id, task_version_id=task_id, play_count=0)
        db.add(playback)
        await db.flush()
    if playback.play_count >= play_limit:
        raise conflict("AUDIO_PLAY_LIMIT", "Лимит прослушиваний исчерпан")
    expires_at = now + timedelta(seconds=AUDIO_TOKEN_SECONDS)
    token = _sign_audio(session.id, int(expires_at.timestamp()))
    playback.play_count += 1
    playback.last_played_at = now
    db.add(
        AudioGrant(
            playback_id=playback.id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=expires_at,
        )
    )
    response = {
        "taskId": task_id,
        "playCount": playback.play_count,
        "remainingPlays": play_limit - playback.play_count,
        "audioUrl": f"/api/v1/sessions/{session.id}/audio-stream?token={token}",
        "expiresAt": _iso(expires_at),
    }
    _remember(db, scope, key, request_hash, response)
    record_audit(
        db,
        "AUDIO_PLAY_REQUESTED",
        "AudioPlayback",
        playback.id,
        actor_id=student_id,
        context={"playCount": playback.play_count},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict("CONCURRENT_UPDATE", "Параллельный запрос аудио; повторите запрос") from exc
    return response


async def redeem_audio_token(db: AsyncSession, session_id: str, token: str) -> tuple[bytes, str]:
    _verify_audio(token, session_id)
    row = (
        await db.execute(
            select(ExamSession, Exam)
            .join(ExamAssignment, ExamAssignment.id == ExamSession.assignment_id)
            .join(Exam, Exam.id == ExamAssignment.exam_id)
            .where(ExamSession.id == session_id)
            .with_for_update(of=ExamSession)
        )
    ).one_or_none()
    if row is None:
        raise not_found("Сессия не найдена")
    session, exam = row
    now = utcnow()
    if session.status != "ACTIVE" or _utc(session.deadline_at) <= now:
        raise ApiError(403, "SESSION_CLOSED", "Сессия уже завершена")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    grant_row = (
        await db.execute(
            select(AudioGrant, AudioPlayback)
            .join(AudioPlayback, AudioPlayback.id == AudioGrant.playback_id)
            .where(AudioPlayback.session_id == session_id, AudioGrant.token_hash == token_hash)
            .with_for_update(of=AudioGrant)
        )
    ).one_or_none()
    if grant_row is None:
        raise ApiError(403, "INVALID_AUDIO_TOKEN", "Недействительная ссылка на аудио")
    grant, playback = grant_row
    if (
        not hmac.compare_digest(grant.token_hash, token_hash)
        or grant.consumed_at is not None
        or _utc(grant.expires_at) < now
    ):
        raise ApiError(403, "INVALID_AUDIO_TOKEN", "Недействительная ссылка на аудио")
    task = await _variant_task(db, exam.variant_id, playback.task_version_id)
    if task.audio_resource_id is None:
        raise ApiError(403, "INVALID_AUDIO_TOKEN", "Недействительная ссылка на аудио")
    resource = await db.get(Resource, task.audio_resource_id)
    if resource is None or not resource.media_type.startswith("audio/"):
        raise not_found("Аудиоматериал не найден")
    grant.consumed_at = now
    record_audit(db, "AUDIO_DELIVERED", "AudioPlayback", playback.id)
    await db.commit()
    return resource.content, resource.media_type


async def add_proctor_event(
    db: AsyncSession,
    session_id: str,
    student_id: str,
    event_type: str,
    payload: dict,
    occurred_at: datetime | None,
    idempotency_key: str | None = None,
) -> dict:
    key = _check_key(idempotency_key)
    session, _exam = await _owned_session(db, session_id, student_id, lock=True)
    now = utcnow()
    when = _utc(occurred_at) if occurred_at else now
    scope = f"proctor:{session.id}"
    request_hash = _request_hash(
        {
            "eventType": event_type,
            "payload": payload,
            "occurredAt": _iso(occurred_at),
        }
    )
    replay = await _replay(db, scope, key, request_hash)
    if replay is not None:
        return replay
    await _require_active(db, session, now)
    if when > now + timedelta(minutes=5) or when < _utc(session.started_at) - timedelta(minutes=5):
        raise ApiError(422, "INVALID_EVENT_TIME", "Время события вне окна сессии")
    event = ProctorEvent(
        session_id=session.id,
        event_type=event_type,
        payload=payload,
        occurred_at=when,
        recorded_at=now,
    )
    db.add(event)
    await db.flush()
    response = {
        "id": event.id,
        "eventType": event.event_type,
        "occurredAt": _iso(event.occurred_at),
        "recordedAt": _iso(event.recorded_at),
    }
    _remember(db, scope, key, request_hash, response)
    record_audit(
        db,
        "PROCTOR_EVENT_RECORDED",
        "ProctorEvent",
        event.id,
        actor_id=student_id,
        context={"eventType": event_type},
    )
    await db.commit()
    return response


async def submit_session(
    db: AsyncSession, session_id: str, student_id: str, idempotency_key: str | None = None
) -> dict:
    key = _check_key(idempotency_key)
    session, exam = await _owned_session(db, session_id, student_id, lock=True)
    scope = f"submit:{session.id}"
    request_hash = _request_hash({"sessionId": session.id})
    replay = await _replay(db, scope, key, request_hash)
    if replay is not None:
        return replay
    if session.status in {"SUBMITTED", "CHECKING", "COMPLETED"}:
        if session.submission_reason != "MANUAL":
            raise conflict("SESSION_CLOSED", "Сессия уже завершена", {"status": session.status})
        return await _view(db, session, exam)
    now = utcnow()
    await _require_active(db, session, now)
    await _finalize(db, session, expired=False, now=now)
    response = await _view(db, session, exam)
    _remember(db, scope, key, request_hash, response)
    await db.commit()
    return response


async def expire_due_sessions(db: AsyncSession, *, limit: int = 100) -> int:
    """Atomically close and score due sessions; safe for parallel PostgreSQL workers."""
    if limit < 1:
        return 0
    now = utcnow()
    due = (
        await db.scalars(
            select(ExamSession)
            .where(ExamSession.status == "ACTIVE", ExamSession.deadline_at <= now)
            .order_by(ExamSession.deadline_at)
            .limit(min(limit, 1000))
            .with_for_update(skip_locked=True)
        )
    ).all()
    for session in due:
        await _finalize(db, session, expired=True, now=now)
    await db.commit()
    return len(due)


async def run_expiration_worker(
    stop: asyncio.Event, *, interval_seconds: float = 5.0, batch_size: int = 100
) -> None:
    """Poll due attempts until the application asks the worker to stop."""
    while not stop.is_set():
        async with SessionLocal() as db:
            await expire_due_sessions(db, limit=batch_size)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
