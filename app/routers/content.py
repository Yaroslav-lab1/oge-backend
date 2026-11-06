"""Content versions, published variants, exams and scoring-rule contracts."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth import require_roles
from app.db import get_db, utcnow
from app.errors import ApiError, conflict, not_found
from app.models.auth import Role, User, UserRole
from app.models.content import (
    Exam,
    ExamAssignment,
    ExamVariant,
    ExamVariantTask,
    ScoringRuleVersion,
    Task,
    TaskVersion,
    audio_play_limit,
)

router = APIRouter(tags=["content"])
Subject = Literal["MATH", "PHYS"]
AnswerType = Literal["SINGLE_CHOICE", "MULTIPLE_CHOICE", "NUMBER", "TEXT", "ESSAY"]
Manager = Annotated[User, Depends(require_roles("CONTENT_MANAGER", "ADMIN"))]
Admin = Annotated[User, Depends(require_roles("ADMIN"))]
Db = Annotated[AsyncSession, Depends(get_db)]


class TaskCreate(BaseModel):
    subject: Subject
    code: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")


class TaskVersionCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    body: dict
    answer_type: AnswerType = Field(alias="answerType")
    key: dict = Field(default_factory=dict)
    normalization: dict = Field(default_factory=dict)
    criteria: dict[str, Any] | list[dict[str, Any]] = {}
    max_score: int = Field(alias="maxScore", ge=0, le=100)
    geometry: bool = False
    audio_resource_id: str | None = Field(default=None, alias="audioResourceId")

    @model_validator(mode="after")
    def check_key(self) -> "TaskVersionCreate":
        limit = audio_play_limit(self.body)
        self.body = {**self.body, "audioPlayLimit": limit}
        if self.answer_type == "ESSAY" and not self.criteria:
            raise ValueError("Для развёрнутого ответа нужны критерии")
        if self.answer_type != "ESSAY" and not self.key:
            raise ValueError("Для автоматической проверки нужен ключ")
        return self


class VariantCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subject: Subject
    task_version_ids: list[str] = Field(alias="taskVersionIds", min_length=1)

    @model_validator(mode="after")
    def unique_tasks(self) -> "VariantCreate":
        if len(self.task_version_ids) != len(set(self.task_version_ids)):
            raise ValueError("Повторяющиеся версии заданий")
        return self


def aware_utc(value: datetime, name: str, *, stored: bool = False) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        if stored:  # SQLite strips timezone information from UTC DateTime columns.
            return value.replace(tzinfo=UTC)
        raise ValueError(f"{name} должен содержать часовой пояс")
    return value.astimezone(UTC)


class ScoringRuleCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subject: Subject
    effective_from: datetime = Field(alias="effectiveFrom")
    effective_to: datetime | None = Field(default=None, alias="effectiveTo")
    thresholds: dict[str, int]
    max_primary: int = Field(alias="maxPrimary", ge=0)
    geometry_min: int | None = Field(default=None, alias="geometryMin", ge=0)
    discrepancy_threshold: int = Field(default=1, alias="discrepancyThreshold", ge=0)

    @model_validator(mode="after")
    def validate_rule(self) -> "ScoringRuleCreate":
        self.effective_from = aware_utc(self.effective_from, "effectiveFrom")
        if self.effective_to:
            self.effective_to = aware_utc(self.effective_to, "effectiveTo")
            if self.effective_to <= self.effective_from:
                raise ValueError("effectiveTo должен быть позже effectiveFrom")
        if set(self.thresholds) != {"3", "4", "5"}:
            raise ValueError("Требуются пороги отметок 3, 4 и 5")
        values = [self.thresholds[k] for k in ("3", "4", "5")]
        if not all(0 <= n <= self.max_primary for n in values) or values != sorted(values):
            raise ValueError("Пороги должны быть монотонны и в пределах maxPrimary")
        if self.geometry_min is None:
            self.geometry_min = 2 if self.subject == "MATH" else 0
        if self.geometry_min > self.max_primary:
            raise ValueError("geometryMin не может превышать maxPrimary")
        return self


class ExamCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    variant_id: str = Field(alias="variantId")
    scoring_rule_id: str | None = Field(default=None, alias="scoringRuleId")
    opens_at: datetime = Field(alias="opensAt")
    closes_at: datetime = Field(alias="closesAt")
    duration_seconds: int = Field(alias="durationSeconds", gt=0, le=8 * 3600)

    @model_validator(mode="after")
    def valid_window(self) -> "ExamCreate":
        self.opens_at = aware_utc(self.opens_at, "opensAt")
        self.closes_at = aware_utc(self.closes_at, "closesAt")
        if self.closes_at <= self.opens_at:
            raise ValueError("closesAt должен быть позже opensAt")
        return self


class AssignmentCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    student_ids: list[str] = Field(alias="studentIds", min_length=1)

    @model_validator(mode="after")
    def unique_students(self) -> "AssignmentCreate":
        if len(self.student_ids) != len(set(self.student_ids)):
            raise ValueError("Повторяющиеся участники")
        return self


class TaskOut(BaseModel):
    id: str
    subject: Subject
    code: str


class TaskVersionOut(BaseModel):
    id: str
    taskId: str
    version: int
    status: str


class VariantOut(BaseModel):
    id: str
    subject: Subject
    status: str
    taskVersionIds: list[str]


class PublishOut(BaseModel):
    id: str
    status: str
    publishedAt: datetime


class ScoringRuleOut(BaseModel):
    id: str
    subject: Subject
    version: int
    effectiveFrom: datetime
    effectiveTo: datetime | None
    thresholds: dict[str, int]
    maxPrimary: int
    geometryMin: int
    discrepancyThreshold: int


class ExamOut(BaseModel):
    id: str
    variantId: str
    scoringRuleId: str
    opensAt: datetime
    closesAt: datetime
    durationSeconds: int
    status: str


class AssignmentOut(BaseModel):
    id: str
    studentId: str


class AssignmentsOut(BaseModel):
    examId: str
    assignments: list[AssignmentOut]


async def commit_or_conflict(db: AsyncSession, message: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict("ALREADY_EXISTS", message) from exc


async def flush_or_conflict(db: AsyncSession, message: str) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict("ALREADY_EXISTS", message) from exc


@router.post("/tasks", status_code=status.HTTP_201_CREATED, response_model=TaskOut)
async def create_task(data: TaskCreate, db: Db, actor: Manager) -> dict:
    task = Task(subject=data.subject, code=data.code)
    db.add(task)
    await flush_or_conflict(db, "Задание с таким кодом уже существует")
    record_audit(db, "TASK_CREATED", "Task", task.id, actor_id=actor.id)
    await commit_or_conflict(db, "Задание с таким кодом уже существует")
    return {"id": task.id, "subject": task.subject, "code": task.code}


@router.post(
    "/tasks/{task_id}/versions", status_code=status.HTTP_201_CREATED, response_model=TaskVersionOut
)
async def create_task_version(
    task_id: str, data: TaskVersionCreate, db: Db, actor: Manager
) -> dict:
    task = await db.get(Task, task_id)
    if task is None:
        raise not_found("Задание не найдено")
    next_version = (
        await db.scalar(select(func.max(TaskVersion.version)).where(TaskVersion.task_id == task_id))
    ) or 0
    version = TaskVersion(
        task_id=task_id,
        version=next_version + 1,
        status="DRAFT",
        body=data.body,
        answer_type=data.answer_type,
        key=data.key,
        normalization=data.normalization,
        criteria=data.criteria,
        max_score=data.max_score,
        geometry=data.geometry,
        audio_resource_id=data.audio_resource_id,
    )
    db.add(version)
    await flush_or_conflict(db, "Версия уже существует или аудиоматериал не найден")
    record_audit(db, "TASK_VERSION_CREATED", "TaskVersion", version.id, actor_id=actor.id)
    await commit_or_conflict(db, "Версия уже существует или аудиоматериал не найден")
    return {
        "id": version.id,
        "taskId": task_id,
        "version": version.version,
        "status": version.status,
    }


@router.post("/task-versions/{version_id}/publish", response_model=PublishOut)
async def publish_task_version(version_id: str, db: Db, actor: Manager) -> dict:
    version = await db.get(TaskVersion, version_id)
    if version is None:
        raise not_found("Версия задания не найдена")
    if version.status != "DRAFT":
        raise conflict("IMMUTABLE_VERSION", "Версия уже опубликована")
    version.status = "PUBLISHED"
    version.published_at = utcnow()
    record_audit(db, "TASK_VERSION_PUBLISHED", "TaskVersion", version.id, actor_id=actor.id)
    await db.commit()
    return {"id": version.id, "status": version.status, "publishedAt": version.published_at}


@router.post("/variants", status_code=status.HTTP_201_CREATED, response_model=VariantOut)
async def create_variant(data: VariantCreate, db: Db, actor: Manager) -> dict:
    versions = (
        await db.scalars(select(TaskVersion).where(TaskVersion.id.in_(data.task_version_ids)))
    ).all()
    if len(versions) != len(data.task_version_ids):
        raise not_found("Некоторые версии заданий не найдены")
    tasks = (await db.scalars(select(Task).where(Task.id.in_([v.task_id for v in versions])))).all()
    if any(t.subject != data.subject for t in tasks):
        raise ApiError(422, "SUBJECT_MISMATCH", "Задания должны соответствовать предмету варианта")
    if any(v.status != "PUBLISHED" for v in versions):
        raise conflict("UNPUBLISHED_TASK", "Вариант может содержать только опубликованные версии")
    variant = ExamVariant(subject=data.subject, status="DRAFT")
    db.add(variant)
    await flush_or_conflict(db, "Вариант не удалось создать")
    for ordinal, version_id in enumerate(data.task_version_ids, start=1):
        db.add(ExamVariantTask(variant_id=variant.id, task_version_id=version_id, ordinal=ordinal))
    record_audit(db, "VARIANT_CREATED", "ExamVariant", variant.id, actor_id=actor.id)
    await commit_or_conflict(db, "Вариант не удалось создать")
    return {
        "id": variant.id,
        "subject": variant.subject,
        "status": variant.status,
        "taskVersionIds": data.task_version_ids,
    }


@router.post("/variants/{variant_id}/publish", response_model=PublishOut)
async def publish_variant(variant_id: str, db: Db, actor: Manager) -> dict:
    variant = await db.get(ExamVariant, variant_id)
    if variant is None:
        raise not_found("Вариант не найден")
    if variant.status != "DRAFT":
        raise conflict("IMMUTABLE_VARIANT", "Вариант уже опубликован")
    count = await db.scalar(
        select(func.count())
        .select_from(ExamVariantTask)
        .where(ExamVariantTask.variant_id == variant_id)
    )
    if not count:
        raise conflict("EMPTY_VARIANT", "Пустой вариант публиковать нельзя")
    variant.status = "PUBLISHED"
    variant.published_at = utcnow()
    record_audit(db, "VARIANT_PUBLISHED", "ExamVariant", variant.id, actor_id=actor.id)
    await db.commit()
    return {"id": variant.id, "status": variant.status, "publishedAt": variant.published_at}


@router.post("/scoring-rules", status_code=status.HTTP_201_CREATED, response_model=ScoringRuleOut)
async def create_scoring_rule(data: ScoringRuleCreate, db: Db, actor: Admin) -> dict:
    last = (
        await db.scalar(
            select(func.max(ScoringRuleVersion.version)).where(
                ScoringRuleVersion.subject == data.subject
            )
        )
    ) or 0
    rule = ScoringRuleVersion(
        subject=data.subject,
        version=last + 1,
        effective_from=data.effective_from,
        effective_to=data.effective_to,
        thresholds=data.thresholds,
        max_primary=data.max_primary,
        geometry_min=data.geometry_min,
        discrepancy_threshold=data.discrepancy_threshold,
    )
    db.add(rule)
    await flush_or_conflict(db, "Версия правила уже существует")
    record_audit(db, "SCORING_RULE_CREATED", "ScoringRuleVersion", rule.id, actor_id=actor.id)
    await commit_or_conflict(db, "Версия правила уже существует")
    return {
        "id": rule.id,
        "subject": rule.subject,
        "version": rule.version,
        "effectiveFrom": rule.effective_from,
        "effectiveTo": rule.effective_to,
        "thresholds": rule.thresholds,
        "maxPrimary": rule.max_primary,
        "geometryMin": rule.geometry_min,
        "discrepancyThreshold": rule.discrepancy_threshold,
    }


@router.get("/scoring-rules", response_model=list[ScoringRuleOut])
async def list_scoring_rules(db: Db, actor: Manager, subject: Subject | None = None) -> list[dict]:
    query = select(ScoringRuleVersion).order_by(
        ScoringRuleVersion.subject, ScoringRuleVersion.version
    )
    if subject:
        query = query.where(ScoringRuleVersion.subject == subject)
    rules = (await db.scalars(query)).all()
    return [
        {
            "id": r.id,
            "subject": r.subject,
            "version": r.version,
            "effectiveFrom": r.effective_from,
            "effectiveTo": r.effective_to,
            "thresholds": r.thresholds,
            "maxPrimary": r.max_primary,
            "geometryMin": r.geometry_min,
            "discrepancyThreshold": r.discrepancy_threshold,
        }
        for r in rules
    ]


@router.post("/exams", status_code=status.HTTP_201_CREATED, response_model=ExamOut)
async def create_exam(data: ExamCreate, db: Db, actor: Admin) -> dict:
    variant = await db.get(ExamVariant, data.variant_id)
    if variant is None:
        raise not_found("Вариант не найден")
    if variant.status != "PUBLISHED":
        raise conflict("UNPUBLISHED_VARIANT", "Экзамен требует опубликованный вариант")
    rule = await db.scalar(
        select(ScoringRuleVersion)
        .where(
            ScoringRuleVersion.subject == variant.subject,
            ScoringRuleVersion.effective_from <= data.opens_at,
            or_(
                ScoringRuleVersion.effective_to.is_(None),
                ScoringRuleVersion.effective_to > data.opens_at,
            ),
        )
        .order_by(ScoringRuleVersion.version.desc())
        .limit(1)
    )
    if rule is None:
        raise ApiError(422, "RULE_NOT_EFFECTIVE", "Нет действующего правила для экзамена")
    if data.scoring_rule_id is not None and data.scoring_rule_id != rule.id:
        explicit = await db.get(ScoringRuleVersion, data.scoring_rule_id)
        if explicit is None:
            raise not_found("Правило оценивания не найдено")
        if explicit.subject != variant.subject:
            raise ApiError(
                422, "SUBJECT_MISMATCH", "Правило и вариант относятся к разным предметам"
            )
        raise ApiError(422, "RULE_NOT_LATEST", "Указано не последнее действующее правило")
    exam = Exam(
        variant_id=variant.id,
        scoring_rule_id=rule.id,
        opens_at=data.opens_at,
        closes_at=data.closes_at,
        duration_seconds=data.duration_seconds,
        status="SCHEDULED",
    )
    db.add(exam)
    await flush_or_conflict(db, "Экзамен не удалось создать")
    record_audit(db, "EXAM_CREATED", "Exam", exam.id, actor_id=actor.id)
    await db.commit()
    return {
        "id": exam.id,
        "variantId": exam.variant_id,
        "scoringRuleId": exam.scoring_rule_id,
        "opensAt": exam.opens_at,
        "closesAt": exam.closes_at,
        "durationSeconds": exam.duration_seconds,
        "status": exam.status,
    }


@router.post(
    "/exams/{exam_id}/assignments",
    status_code=status.HTTP_201_CREATED,
    response_model=AssignmentsOut,
)
async def create_assignments(exam_id: str, data: AssignmentCreate, db: Db, actor: Admin) -> dict:
    exam = await db.get(Exam, exam_id)
    if exam is None:
        raise not_found("Экзамен не найден")
    users = (await db.scalars(select(User).where(User.id.in_(data.student_ids)))).all()
    if len(users) != len(data.student_ids) or any(not u.is_active for u in users):
        raise ApiError(422, "INVALID_STUDENT", "Некоторые участники не найдены или заблокированы")
    student_role_ids = set(
        (
            await db.scalars(
                select(UserRole.user_id)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.name == "STUDENT", UserRole.user_id.in_(data.student_ids))
            )
        ).all()
    )
    if student_role_ids != set(data.student_ids):
        raise ApiError(422, "INVALID_STUDENT", "Назначать можно только участников с ролью STUDENT")
    assignments = [
        ExamAssignment(exam_id=exam_id, student_id=student_id) for student_id in data.student_ids
    ]
    db.add_all(assignments)
    await flush_or_conflict(db, "Участник уже назначен на экзамен")
    for assignment in assignments:
        record_audit(db, "EXAM_ASSIGNED", "ExamAssignment", assignment.id, actor_id=actor.id)
    await commit_or_conflict(db, "Участник уже назначен на экзамен")
    return {
        "examId": exam_id,
        "assignments": [{"id": a.id, "studentId": a.student_id} for a in assignments],
    }
