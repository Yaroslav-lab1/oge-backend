"""Versioned examination content and provenance of imported KIM archives."""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.state import InstanceState

from app.db import Base, new_id, utcnow


def audio_play_limit(body: dict[str, Any]) -> int:
    """Read and validate the immutable per-task audio limit (legacy default: two)."""
    limit = body.get("audioPlayLimit", 2)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("audioPlayLimit должен быть целым числом от 1 до 10")
    return limit


def _id() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("subject", "code"),)

    id: Mapped[str] = _id()
    subject: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskVersion(Base):
    __tablename__ = "task_versions"
    __table_args__ = (
        UniqueConstraint("task_id", "version"),
        CheckConstraint("version > 0"),
        CheckConstraint("max_score >= 0"),
    )

    id: Mapped[str] = _id()
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)
    body: Mapped[dict] = mapped_column(JSON, default=dict)
    answer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[dict] = mapped_column(JSON, default=dict)
    normalization: Mapped[dict] = mapped_column(JSON, default=dict)
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    max_score: Mapped[int] = mapped_column(Integer, nullable=False)
    geometry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    audio_resource_id: Mapped[str | None] = mapped_column(ForeignKey("kim_resources.id"))
    package_version_id: Mapped[str | None] = mapped_column(ForeignKey("kim_package_versions.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExamVariant(Base):
    __tablename__ = "exam_variants"

    id: Mapped[str] = _id()
    subject: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExamVariantTask(Base):
    __tablename__ = "exam_variant_tasks"
    __table_args__ = (
        UniqueConstraint("variant_id", "ordinal"),
        UniqueConstraint("variant_id", "task_version_id"),
        CheckConstraint("ordinal > 0"),
    )

    variant_id: Mapped[str] = mapped_column(ForeignKey("exam_variants.id"), primary_key=True)
    task_version_id: Mapped[str] = mapped_column(ForeignKey("task_versions.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class ScoringRuleVersion(Base):
    __tablename__ = "scoring_rule_versions"
    __table_args__ = (
        UniqueConstraint("subject", "version"),
        CheckConstraint("version > 0"),
        CheckConstraint("max_primary >= 0"),
        CheckConstraint("geometry_min >= 0"),
        CheckConstraint("discrepancy_threshold >= 0"),
    )

    id: Mapped[str] = _id()
    subject: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    max_primary: Mapped[int] = mapped_column(Integer, nullable=False)
    geometry_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discrepancy_threshold: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Exam(Base):
    __tablename__ = "exams"
    __table_args__ = (CheckConstraint("duration_seconds > 0"),)

    id: Mapped[str] = _id()
    variant_id: Mapped[str] = mapped_column(ForeignKey("exam_variants.id"), index=True)
    scoring_rule_id: Mapped[str] = mapped_column(ForeignKey("scoring_rule_versions.id"))
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="SCHEDULED", nullable=False)


class ExamAssignment(Base):
    __tablename__ = "exam_assignments"
    __table_args__ = (UniqueConstraint("exam_id", "student_id"),)

    id: Mapped[str] = _id()
    exam_id: Mapped[str] = mapped_column(ForeignKey("exams.id"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KimPackage(Base):
    __tablename__ = "kim_packages"
    __table_args__ = (UniqueConstraint("subject", "name"),)

    id: Mapped[str] = _id()
    subject: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KimPackageVersion(Base):
    __tablename__ = "kim_package_versions"
    __table_args__ = (
        UniqueConstraint("package_id", "version"),
        UniqueConstraint("checksum"),
    )

    id: Mapped[str] = _id()
    package_id: Mapped[str] = mapped_column(ForeignKey("kim_packages.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(100), nullable=False)
    passport: Mapped[dict] = mapped_column(JSON, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KimPackageFile(Base):
    __tablename__ = "kim_package_files"
    __table_args__ = (UniqueConstraint("package_version_id", "path"),)

    id: Mapped[str] = _id()
    package_version_id: Mapped[str] = mapped_column(
        ForeignKey("kim_package_versions.id"), index=True
    )
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class KimImportJob(Base):
    __tablename__ = "kim_import_jobs"

    id: Mapped[str] = _id()
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    package_version_id: Mapped[str | None] = mapped_column(ForeignKey("kim_package_versions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KimImportIssue(Base):
    __tablename__ = "kim_import_issues"

    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("kim_import_jobs.id"), index=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    path: Mapped[str | None] = mapped_column(String(500))
    message: Mapped[str] = mapped_column(Text, nullable=False)


class Topic(Base):
    __tablename__ = "kim_topics"
    __table_args__ = (UniqueConstraint("package_version_id", "code"),)

    id: Mapped[str] = _id()
    package_version_id: Mapped[str] = mapped_column(
        ForeignKey("kim_package_versions.id"), index=True
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Prototype(Base):
    __tablename__ = "kim_prototypes"
    __table_args__ = (UniqueConstraint("package_version_id", "code"),)

    id: Mapped[str] = _id()
    package_version_id: Mapped[str] = mapped_column(
        ForeignKey("kim_package_versions.id"), index=True
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class AnswerKey(Base):
    __tablename__ = "kim_answer_keys"

    id: Mapped[str] = _id()
    task_version_id: Mapped[str] = mapped_column(ForeignKey("task_versions.id"), unique=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class Criterion(Base):
    __tablename__ = "kim_criteria"

    id: Mapped[str] = _id()
    task_version_id: Mapped[str] = mapped_column(ForeignKey("task_versions.id"), unique=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class Resource(Base):
    __tablename__ = "kim_resources"
    __table_args__ = (UniqueConstraint("package_version_id", "path"),)

    id: Mapped[str] = _id()
    package_version_id: Mapped[str] = mapped_column(
        ForeignKey("kim_package_versions.id"), index=True
    )
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


def _reject_published_update(target: TaskVersion | ExamVariant) -> None:
    state = cast(InstanceState[Any], inspect(target))
    status_history = state.attrs.status.history
    old_status = status_history.deleted[0] if status_history.deleted else target.status
    changed = {attr.key for attr in state.attrs if attr.history.has_changes()}
    if old_status == "PUBLISHED":
        raise ValueError("Published content is immutable")
    if target.status == "PUBLISHED" and changed - {"status", "published_at"}:
        raise ValueError("Publishing may only change status and published_at")


@event.listens_for(TaskVersion, "before_update")
def _protect_task_version(_mapper: object, _connection: object, target: TaskVersion) -> None:
    _reject_published_update(target)


@event.listens_for(ExamVariant, "before_update")
def _protect_variant(_mapper: object, _connection: object, target: ExamVariant) -> None:
    _reject_published_update(target)


@event.listens_for(ScoringRuleVersion, "before_update")
def _protect_scoring_rule(
    _mapper: object, _connection: object, _target: ScoringRuleVersion
) -> None:
    raise ValueError("Scoring rule versions are immutable")
