"""Student exam-session endpoints. taskId is the immutable TaskVersion.id."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_roles
from app.db import get_db
from app.models.auth import User
from app.rate_limit import enforce_rate_limit
from app.session import (
    add_proctor_event,
    play_audio,
    read_session,
    redeem_audio_token,
    save_answer,
    start_session,
    submit_session,
)

router = APIRouter(tags=["session"])
Db = Annotated[AsyncSession, Depends(get_db)]
Student = Annotated[User, Depends(require_roles("STUDENT"))]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class AnswerSave(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    raw: Any = Field(...)
    expected_revision: int = Field(alias="expectedRevision", ge=0)


class ProctorEventCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    event_type: str = Field(alias="eventType", pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    payload: dict = Field(default_factory=dict)
    occurred_at: datetime | None = Field(default=None, alias="occurredAt")

    @field_validator("occurred_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurredAt должен содержать часовой пояс")
        return value


class SavedAnswerView(BaseModel):
    taskId: str
    revision: int
    raw: Any
    updatedAt: datetime


class SessionAnswerView(BaseModel):
    raw: Any
    revision: int


class SessionTaskView(BaseModel):
    taskId: str
    ordinal: int
    body: dict
    answerType: str
    maxScore: int
    geometry: bool
    hasAudio: bool
    audioPlayLimit: int
    playCount: int
    remainingPlays: int
    answer: SessionAnswerView | None


class SessionView(BaseModel):
    id: str
    examId: str
    assignmentId: str
    status: str
    startedAt: datetime
    deadlineAt: datetime
    submittedAt: datetime | None
    remainingSeconds: int
    tasks: list[SessionTaskView]


class AudioPlayView(BaseModel):
    taskId: str
    playCount: int
    remainingPlays: int
    audioUrl: str
    expiresAt: datetime


class ProctorEventView(BaseModel):
    id: str
    eventType: str
    occurredAt: datetime
    recordedAt: datetime


@router.post(
    "/exams/{exam_id}/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionView,
)
async def create_exam_session(exam_id: str, db: Db, student: Student) -> dict:
    """Start or resume the student's sole active attempt for this exam."""
    return await start_session(db, exam_id, student.id)


@router.get("/sessions/{session_id}", response_model=SessionView)
async def get_exam_session(session_id: str, db: Db, student: Student) -> dict:
    return await read_session(db, session_id, student.id)


@router.put("/sessions/{session_id}/answers/{task_id}", response_model=SavedAnswerView)
async def put_answer(
    session_id: str,
    task_id: str,
    data: AnswerSave,
    db: Db,
    student: Student,
    idempotency_key: IdempotencyKey = None,
) -> dict:
    await enforce_rate_limit(db, "answer", student.id)
    return await save_answer(
        db,
        session_id,
        task_id,
        student.id,
        data.raw,
        data.expected_revision,
        idempotency_key,
    )


@router.post("/sessions/{session_id}/audio/{task_id}/play", response_model=AudioPlayView)
async def request_audio_play(
    session_id: str,
    task_id: str,
    db: Db,
    student: Student,
    idempotency_key: IdempotencyKey = None,
) -> dict:
    await enforce_rate_limit(db, "audio", student.id)
    return await play_audio(db, session_id, task_id, student.id, idempotency_key)


@router.get(
    "/sessions/{session_id}/audio-stream",
    response_class=Response,
    summary="Однократная выдача аудиобайтов",
    description=(
        "Ссылка из операции воспроизведения действует 60 секунд и может быть "
        "использована один раз. Путь и содержимое ресурса не раскрываются в URL."
    ),
    responses={
        200: {
            "description": "Аудиоданные задания",
            "content": {"audio/*": {"schema": {"type": "string", "format": "binary"}}},
        },
        403: {"description": "Токен истёк, уже использован или сессия завершена"},
    },
)
async def get_audio_stream(
    session_id: str,
    db: Db,
    token: Annotated[
        str,
        Query(
            min_length=1,
            max_length=600,
            description="Одноразовый HMAC-подписанный токен со сроком 60 секунд",
        ),
    ],
) -> Response:
    """Redeem one HMAC-signed URL without exposing the resource path or answer key."""
    content, media_type = await redeem_audio_token(db, session_id, token)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "/sessions/{session_id}/proctor-events",
    status_code=status.HTTP_201_CREATED,
    response_model=ProctorEventView,
)
async def create_proctor_event(
    session_id: str,
    data: ProctorEventCreate,
    db: Db,
    student: Student,
    idempotency_key: IdempotencyKey = None,
) -> dict:
    return await add_proctor_event(
        db,
        session_id,
        student.id,
        data.event_type,
        data.payload,
        data.occurred_at,
        idempotency_key,
    )


@router.post("/sessions/{session_id}/submit", response_model=SessionView)
async def submit_exam_session(
    session_id: str,
    db: Db,
    student: Student,
    idempotency_key: IdempotencyKey = None,
) -> dict:
    return await submit_session(db, session_id, student.id, idempotency_key)
