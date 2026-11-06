"""Exam attempts, append-only answer history and short-lived audio grants."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, new_id, utcnow


class ExamSession(Base):
    __tablename__ = "exam_sessions"
    __table_args__ = (
        Index(
            "uq_exam_sessions_active_assignment",
            "assignment_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
        CheckConstraint(
            "status IN ('CREATED', 'ACTIVE', 'SUBMITTED', 'EXPIRED', "
            "'CHECKING', 'COMPLETED', 'CANCELLED')"
        ),
        CheckConstraint("submission_reason IS NULL OR submission_reason IN ('MANUAL', 'TIMEOUT')"),
        CheckConstraint("deadline_at > started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("exam_assignments.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_reason: Mapped[str | None] = mapped_column(String(16))


class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (
        UniqueConstraint("session_id", "task_version_id"),
        CheckConstraint("revision > 0"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("exam_sessions.id"), nullable=False, index=True
    )
    task_version_id: Mapped[str] = mapped_column(
        ForeignKey("task_versions.id"), nullable=False, index=True
    )
    raw: Mapped[Any] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class AnswerRevision(Base):
    __tablename__ = "answer_revisions"
    __table_args__ = (
        UniqueConstraint("answer_id", "revision"),
        CheckConstraint("revision > 0"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    answer_id: Mapped[str] = mapped_column(ForeignKey("answers.id"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    raw: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("scope", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(160), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class AudioPlayback(Base):
    __tablename__ = "audio_playbacks"
    __table_args__ = (
        UniqueConstraint("session_id", "task_version_id"),
        CheckConstraint("play_count >= 0 AND play_count <= 10"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("exam_sessions.id"), nullable=False, index=True
    )
    task_version_id: Mapped[str] = mapped_column(
        ForeignKey("task_versions.id"), nullable=False, index=True
    )
    play_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AudioGrant(Base):
    __tablename__ = "audio_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    playback_id: Mapped[str] = mapped_column(
        ForeignKey("audio_playbacks.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ProctorEvent(Base):
    __tablename__ = "proctor_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("exam_sessions.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


@event.listens_for(AnswerRevision, "before_update")
@event.listens_for(AnswerRevision, "before_delete")
@event.listens_for(ProctorEvent, "before_update")
@event.listens_for(ProctorEvent, "before_delete")
def _reject_history_mutation(*_: object) -> None:
    raise ValueError("Session history is append only")
