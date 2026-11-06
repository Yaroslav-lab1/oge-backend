from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, new_id, utcnow


class AutoCheckResult(Base):
    __tablename__ = "auto_check_results"
    __table_args__ = (
        UniqueConstraint("session_id", "task_version_id"),
        CheckConstraint("score >= 0 AND score <= max_score"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    task_version_id: Mapped[str] = mapped_column(ForeignKey("task_versions.id"))
    score: Mapped[int] = mapped_column(Integer)
    max_score: Mapped[int] = mapped_column(Integer)
    normalized: Mapped[Any] = mapped_column(JSON, nullable=True)
    rule_id: Mapped[str] = mapped_column(ForeignKey("scoring_rule_versions.id"))
    rule_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewAssignment(Base):
    __tablename__ = "review_assignments"
    __table_args__ = (
        UniqueConstraint("session_id", "task_version_id", "round"),
        CheckConstraint("round IN (1, 2)"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    task_version_id: Mapped[str] = mapped_column(ForeignKey("task_versions.id"))
    round: Mapped[int] = mapped_column(Integer)
    expert_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewScore(Base):
    __tablename__ = "review_scores"
    __table_args__ = (
        UniqueConstraint("assignment_id", "criterion_code"),
        CheckConstraint("score >= 0"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("review_assignments.id"), index=True)
    criterion_code: Mapped[str] = mapped_column(String(100))
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewDispute(Base):
    __tablename__ = "review_disputes"
    __table_args__ = (UniqueConstraint("session_id", "task_version_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    task_version_id: Mapped[str] = mapped_column(ForeignKey("task_versions.id"))
    first_score: Mapped[int] = mapped_column(Integer)
    second_score: Mapped[int] = mapped_column(Integer)
    threshold: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    senior_expert_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    final_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_criteria: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResultVersion(Base):
    __tablename__ = "result_versions"
    __table_args__ = (
        UniqueConstraint("session_id", "version"),
        CheckConstraint("primary_score >= 0 AND primary_score <= max_score"),
        CheckConstraint("grade BETWEEN 2 AND 5"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    primary_score: Mapped[int] = mapped_column(Integer)
    max_score: Mapped[int] = mapped_column(Integer)
    grade: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    breakdown: Mapped[Any] = mapped_column(JSON)
    rule_id: Mapped[str] = mapped_column(ForeignKey("scoring_rule_versions.id"))
    rule_version: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    object_type: Mapped[str] = mapped_column(String(100))
    object_id: Mapped[str] = mapped_column(String(100))
    context: Mapped[Any] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
