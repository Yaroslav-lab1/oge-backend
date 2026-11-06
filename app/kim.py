"""Local KIM archive contract and atomic import service.

The schema parser is kept here, separate from persistence. Official JSON Schemas can
replace ``validate_archive`` without changing the import transaction or public API.
"""

import hashlib
import io
import json
import mimetypes
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.db import utcnow
from app.errors import ApiError
from app.metrics import KIM_IMPORT_ERRORS
from app.models.content import (
    AnswerKey,
    Criterion,
    ExamVariant,
    ExamVariantTask,
    KimImportIssue,
    KimImportJob,
    KimPackage,
    KimPackageFile,
    KimPackageVersion,
    Prototype,
    Resource,
    Task,
    TaskVersion,
    Topic,
    audio_play_limit,
)

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_FILES = 1000
REQUIRED_FILES = {
    "passport.json",
    "topics.json",
    "plan.json",
    "tree.json",
    "tasks.json",
    "prototypes.json",
    "keys.json",
    "criteria.json",
    "instruction.md",
    "materials.json",
    "manifest.json",
}
SCHEMAS = {"MATH": "MATH-OGE-SCHEMA-1.0", "PHYS": "PHYS-OGE-SCHEMA-1.0"}
TEXT_SUFFIXES = {".json", ".md", ".txt", ".csv"}
RESOURCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".mp3", ".wav", ".pdf"}
EXECUTABLE_SUFFIXES = {
    ".py",
    ".sh",
    ".bash",
    ".js",
    ".mjs",
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".ps1",
    ".html",
    ".svg",
}
LINK_RE = re.compile(r"(?:https?://|ftp://|file://|www\.)", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(\-]*\d{3}[)\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
FULL_NAME_RE = re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\b")
CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass
class PackageData:
    checksum: str
    files: dict[str, bytes] = field(default_factory=dict)
    parsed: dict[str, Any] = field(default_factory=dict)
    variants: dict[str, Any] = field(default_factory=dict)
    subject: str | None = None
    name: str | None = None
    schema_version: str | None = None
    source_version: str | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    def report(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checksum": self.checksum,
            "subject": self.subject,
            "schemaVersion": self.schema_version,
            "sourceVersion": self.source_version,
            "files": len(self.files),
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _issue(data: PackageData, code: str, message: str, path: str | None = None) -> None:
    data.issues.append(Issue(code, message, path))


def _safe_path(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    parts = name.rstrip("/").split("/")
    return (
        bool(parts) and all(part not in {".", "..", ""} for part in parts) and ":" not in parts[0]
    )


def _read_json(data: PackageData, name: str) -> Any:
    try:
        value = json.loads(data.files[name].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
        _issue(data, "INVALID_JSON", "Файл должен содержать корректный UTF-8 JSON", name)
        return None
    data.parsed[name] = value
    return value


def validate_archive(payload: bytes) -> PackageData:
    """Validate an archive without writing anything to the database or filesystem."""
    data = PackageData(checksum=hashlib.sha256(payload).hexdigest())
    if len(payload) > MAX_ARCHIVE_BYTES:
        _issue(data, "ARCHIVE_TOO_LARGE", "Размер ZIP превышает 50 МБ")
        return data
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (OSError, zipfile.BadZipFile):
        _issue(data, "INVALID_ZIP", "Не удалось прочитать ZIP-архив")
        return data
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            _issue(data, "TOO_MANY_FILES", "В архиве более 1000 записей")
            return data
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            _issue(data, "UNPACKED_TOO_LARGE", "Размер распакованных файлов превышает 50 МБ")
            return data
        seen: set[str] = set()
        directories: set[str] = set()
        actual_total = 0
        for info in infos:
            name = info.filename
            if not _safe_path(name):
                _issue(data, "UNSAFE_PATH", "Недопустимый путь в архиве", name)
                continue
            if name in seen:
                _issue(data, "DUPLICATE_PATH", "Повторяющийся путь в архиве", name)
                continue
            seen.add(name)
            if info.is_dir():
                directories.add(name)
                continue
            if name not in REQUIRED_FILES and not name.startswith(("resources/", "variants/")):
                _issue(data, "UNEXPECTED_PATH", "Файл вне разрешённых каталогов", name)
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or info.flag_bits & 0x1:
                _issue(data, "UNSAFE_FILE", "Ссылки и зашифрованные записи запрещены", name)
                continue
            suffix = PurePosixPath(name).suffix.lower()
            if suffix in EXECUTABLE_SUFFIXES or suffix not in TEXT_SUFFIXES | RESOURCE_SUFFIXES:
                _issue(data, "UNSAFE_FILE", "Тип файла не разрешён", name)
                continue
            if name.startswith("resources/") and suffix not in RESOURCE_SUFFIXES:
                _issue(data, "INVALID_RESOURCE", "В resources допускаются только медиафайлы", name)
                continue
            if name.startswith("variants/") and suffix != ".json":
                _issue(data, "INVALID_VARIANT", "В variants допускаются только JSON", name)
                continue
            if (
                info.compress_size
                and info.file_size > 100 * info.compress_size
                and info.file_size > 1024 * 1024
            ):
                _issue(data, "ZIP_BOMB", "Подозрительный коэффициент сжатия", name)
                continue
            try:
                with archive.open(info) as source:
                    content = source.read(MAX_ARCHIVE_BYTES + 1)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                _issue(data, "UNREADABLE_FILE", "Не удалось проверить содержимое файла", name)
                continue
            actual_total += len(content)
            if len(content) > MAX_ARCHIVE_BYTES or actual_total > MAX_ARCHIVE_BYTES:
                _issue(data, "UNPACKED_TOO_LARGE", "Распакованные данные превышают 50 МБ")
                return data
            if len(content) != info.file_size:
                _issue(data, "SIZE_MISMATCH", "Размер ZIP-записи не совпал", name)
                continue
            data.files[name] = content
            if suffix in TEXT_SUFFIXES:
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    _issue(data, "INVALID_UTF8", "Текстовый файл должен быть UTF-8", name)
                    continue
                if LINK_RE.search(text):
                    _issue(data, "EXTERNAL_LINK", "Внешние ссылки в пакете запрещены", name)
                if EMAIL_RE.search(text) or PHONE_RE.search(text) or FULL_NAME_RE.search(text):
                    _issue(data, "POTENTIAL_PII", "Обнаружены возможные персональные данные", name)
    missing = REQUIRED_FILES - data.files.keys()
    for name in sorted(missing):
        _issue(data, "MISSING_FILE", "Отсутствует обязательный файл", name)
    if not ("resources/" in directories or any(n.startswith("resources/") for n in data.files)):
        _issue(data, "MISSING_DIRECTORY", "Отсутствует каталог resources/", "resources/")
    variant_names = sorted(
        n for n in data.files if n.startswith("variants/") and n.endswith(".json")
    )
    if not variant_names:
        _issue(data, "MISSING_VARIANT", "Нужен хотя бы один файл варианта", "variants/")
    if missing:
        return data

    manifest = _read_json(data, "manifest.json")
    passport = _read_json(data, "passport.json")
    if not isinstance(manifest, dict) or not isinstance(passport, dict):
        return data
    data.subject = passport.get("subject")
    data.name = passport.get("name")
    data.schema_version = passport.get("schemaVersion")
    data.source_version = passport.get("sourceVersion")
    if data.subject not in SCHEMAS or data.schema_version != SCHEMAS.get(data.subject):
        _issue(
            data, "SCHEMA_MISMATCH", "Неподдерживаемая версия схемы или предмет", "passport.json"
        )
    if not isinstance(data.name, str) or not 1 <= len(data.name) <= 200:
        _issue(data, "INVALID_PASSPORT", "Требуется название пакета", "passport.json")
    if not isinstance(data.source_version, str) or not data.source_version:
        _issue(data, "INVALID_PASSPORT", "Требуется sourceVersion", "passport.json")
    if (
        manifest.get("schemaVersion") != data.schema_version
        or manifest.get("sourceVersion") != data.source_version
    ):
        _issue(
            data, "MANIFEST_MISMATCH", "Версия в manifest не совпадает с паспортом", "manifest.json"
        )
    manifest_files = manifest.get("files")
    expected = set(data.files) - {"manifest.json"}
    if not isinstance(manifest_files, dict) or set(manifest_files) != expected:
        _issue(
            data,
            "MANIFEST_MISMATCH",
            "Manifest должен перечислять все файлы кроме себя",
            "manifest.json",
        )
    else:
        for name, meta in manifest_files.items():
            digest = hashlib.sha256(data.files[name]).hexdigest()
            if (
                not isinstance(meta, dict)
                or meta.get("sha256") != digest
                or meta.get("size") != len(data.files[name])
            ):
                _issue(data, "CHECKSUM_MISMATCH", "Размер или SHA-256 файла не совпал", name)

    for name in (
        "topics.json",
        "plan.json",
        "tree.json",
        "tasks.json",
        "prototypes.json",
        "keys.json",
        "criteria.json",
        "materials.json",
    ):
        _read_json(data, name)
    for name in variant_names:
        data.variants[name] = _read_json(data, name)
    if data.issues:
        return data
    _validate_relations(data)
    return data


def _validate_relations(data: PackageData) -> None:
    topics = data.parsed["topics.json"]
    tasks = data.parsed["tasks.json"]
    prototypes = data.parsed["prototypes.json"]
    keys = data.parsed["keys.json"]
    criteria = data.parsed["criteria.json"]
    materials = data.parsed["materials.json"]
    if (
        not isinstance(topics, list)
        or not isinstance(tasks, list)
        or not isinstance(prototypes, list)
    ):
        _issue(data, "INVALID_SCHEMA", "topics, tasks и prototypes должны быть массивами")
        return
    if (
        not isinstance(keys, dict)
        or not isinstance(criteria, dict)
        or not isinstance(materials, list)
    ):
        _issue(data, "INVALID_SCHEMA", "keys и criteria должны быть объектами; materials массивом")
        return
    topic_codes: set[str] = set()
    for topic in topics:
        if (
            not isinstance(topic, dict)
            or not isinstance(topic.get("code"), str)
            or not isinstance(topic.get("title"), str)
        ):
            _issue(data, "INVALID_TOPIC", "Тема требует code и title", "topics.json")
            continue
        if topic["code"] in topic_codes:
            _issue(data, "DUPLICATE_CODE", "Повторяющаяся тема", "topics.json")
        topic_codes.add(topic["code"])
    task_codes: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            _issue(data, "INVALID_TASK", "Задание должно быть объектом", "tasks.json")
            continue
        code = task.get("code")
        if not isinstance(code, str) or not CODE_RE.fullmatch(code):
            _issue(data, "INVALID_TASK", "Некорректный код задания", "tasks.json")
            continue
        if code in task_codes:
            _issue(data, "DUPLICATE_CODE", "Повторяющееся задание", "tasks.json")
        task_codes.add(code)
        if task.get("topicCode") not in topic_codes:
            _issue(data, "UNKNOWN_TOPIC", "Задание ссылается на неизвестную тему", "tasks.json")
        answer_type = task.get("answerType")
        if answer_type not in {"SINGLE_CHOICE", "MULTIPLE_CHOICE", "NUMBER", "TEXT", "ESSAY"}:
            _issue(data, "INVALID_ANSWER_TYPE", "Неподдерживаемый тип ответа", "tasks.json")
        if (
            not isinstance(task.get("body"), dict)
            or not isinstance(task.get("maxScore"), int)
            or task["maxScore"] < 0
        ):
            _issue(data, "INVALID_TASK", "Требуются body и неотрицательный maxScore", "tasks.json")
        elif isinstance(task["body"], dict):
            try:
                audio_play_limit(task["body"])
            except ValueError as exc:
                _issue(data, "INVALID_AUDIO_PLAY_LIMIT", str(exc), "tasks.json")
        if answer_type == "ESSAY":
            if code not in criteria or not isinstance(criteria[code], dict | list):
                _issue(
                    data,
                    "MISSING_CRITERIA",
                    "Для развёрнутого ответа нужны критерии",
                    "criteria.json",
                )
        elif code not in keys or not isinstance(keys[code], dict):
            _issue(data, "MISSING_KEY", "Для задания нужен ключ", "keys.json")
        resource = task.get("audioResource")
        if resource is not None and resource not in data.files:
            _issue(data, "MISSING_RESOURCE", "Аудиофайл не найден", "tasks.json")
    for prototype in prototypes:
        if not isinstance(prototype, dict) or not isinstance(prototype.get("code"), str):
            _issue(data, "INVALID_PROTOTYPE", "Прототип требует code", "prototypes.json")
    for material in materials:
        if (
            not isinstance(material, dict)
            or material.get("path") not in data.files
            or not str(material.get("path", "")).startswith("resources/")
        ):
            _issue(
                data,
                "MISSING_RESOURCE",
                "Материал ссылается на отсутствующий файл",
                "materials.json",
            )
    material_paths = {m.get("path") for m in materials if isinstance(m, dict)}
    for task in tasks:
        if isinstance(task, dict) and task.get("audioResource") is not None:
            if task["audioResource"] not in material_paths:
                _issue(
                    data, "UNDECLARED_RESOURCE", "Аудиофайл должен быть в materials", "tasks.json"
                )
    for name, variant in data.variants.items():
        if not isinstance(variant, dict) or not isinstance(variant.get("taskCodes"), list):
            _issue(data, "INVALID_VARIANT", "Вариант требует taskCodes", name)
            continue
        codes = variant["taskCodes"]
        if (
            not codes
            or len(codes) != len(set(codes))
            or any(code not in task_codes for code in codes)
        ):
            _issue(data, "INVALID_VARIANT", "Вариант содержит повтор или неизвестное задание", name)


async def import_archive(db: AsyncSession, payload: bytes, actor_id: str) -> dict[str, Any]:
    data = validate_archive(payload)
    if not data.valid:
        job = KimImportJob(checksum=data.checksum, status="FAILED", finished_at=utcnow())
        db.add(job)
        await db.flush()
        db.add_all(
            [
                KimImportIssue(
                    job_id=job.id, code=issue.code, path=issue.path, message=issue.message
                )
                for issue in data.issues
            ]
        )
        await db.commit()
        report = data.report()
        report["jobId"] = job.id
        KIM_IMPORT_ERRORS.inc()
        raise ApiError(422, "KIM_INVALID", "Пакет КИМ не прошёл проверку", report)
    existing = await db.scalar(
        select(KimPackageVersion).where(KimPackageVersion.checksum == data.checksum)
    )
    if existing is not None:
        job = KimImportJob(
            checksum=data.checksum,
            status="DUPLICATE",
            package_version_id=existing.id,
            finished_at=utcnow(),
        )
        db.add(job)
        await db.commit()
        return {
            "id": job.id,
            "status": job.status,
            "packageVersionId": existing.id,
            "checksum": data.checksum,
        }
    try:
        return await _write_import(db, data, actor_id)
    except IntegrityError as exc:
        await db.rollback()
        existing = await db.scalar(
            select(KimPackageVersion).where(KimPackageVersion.checksum == data.checksum)
        )
        if existing is not None:
            job = KimImportJob(
                checksum=data.checksum,
                status="DUPLICATE",
                package_version_id=existing.id,
                finished_at=utcnow(),
            )
            db.add(job)
            await db.commit()
            return {
                "id": job.id,
                "status": job.status,
                "packageVersionId": existing.id,
                "checksum": data.checksum,
            }
        KIM_IMPORT_ERRORS.inc()
        raise ApiError(409, "KIM_CONFLICT", "Пакет конфликтует с существующими данными") from exc
    except Exception:
        await db.rollback()
        raise


async def _write_import(db: AsyncSession, data: PackageData, actor_id: str) -> dict[str, Any]:
    package = await db.scalar(
        select(KimPackage).where(KimPackage.subject == data.subject, KimPackage.name == data.name)
    )
    if package is None:
        package = KimPackage(subject=data.subject, name=data.name)
        db.add(package)
        await db.flush()
    last = (
        await db.scalar(
            select(func.max(KimPackageVersion.version)).where(
                KimPackageVersion.package_id == package.id
            )
        )
    ) or 0
    package_version = KimPackageVersion(
        package_id=package.id,
        version=last + 1,
        checksum=data.checksum,
        schema_version=data.schema_version,
        source_version=data.source_version,
        passport=data.parsed["passport.json"],
    )
    db.add(package_version)
    await db.flush()
    for path, content in data.files.items():
        db.add(
            KimPackageFile(
                package_version_id=package_version.id,
                path=path,
                checksum=hashlib.sha256(content).hexdigest(),
                size=len(content),
                content=content,
            )
        )
    for topic in data.parsed["topics.json"]:
        db.add(
            Topic(
                package_version_id=package_version.id,
                code=topic["code"],
                title=topic["title"],
                data=topic,
            )
        )
    for prototype in data.parsed["prototypes.json"]:
        db.add(
            Prototype(package_version_id=package_version.id, code=prototype["code"], data=prototype)
        )
    resource_ids: dict[str, str] = {}
    for material in data.parsed["materials.json"]:
        path = material["path"]
        content = data.files[path]
        resource = Resource(
            package_version_id=package_version.id,
            path=path,
            media_type=material.get("mediaType")
            or mimetypes.guess_type(path)[0]
            or "application/octet-stream",
            checksum=hashlib.sha256(content).hexdigest(),
            content=content,
        )
        db.add(resource)
        await db.flush()
        resource_ids[path] = resource.id
    keys = data.parsed["keys.json"]
    criteria = data.parsed["criteria.json"]
    task_versions: dict[str, str] = {}
    for item in data.parsed["tasks.json"]:
        task = await db.scalar(
            select(Task).where(Task.subject == data.subject, Task.code == item["code"])
        )
        if task is None:
            task = Task(subject=data.subject, code=item["code"])
            db.add(task)
            await db.flush()
        last_task_version = (
            await db.scalar(
                select(func.max(TaskVersion.version)).where(TaskVersion.task_id == task.id)
            )
        ) or 0
        key = keys.get(item["code"], {})
        criterion = criteria.get(item["code"], {})
        version = TaskVersion(
            task_id=task.id,
            version=last_task_version + 1,
            status="PUBLISHED",
            body={**item["body"], "audioPlayLimit": audio_play_limit(item["body"])},
            answer_type=item["answerType"],
            key=key,
            normalization=item.get("normalization", {}),
            criteria=criterion,
            max_score=item["maxScore"],
            geometry=bool(item.get("geometry", False)),
            audio_resource_id=resource_ids.get(item.get("audioResource")),
            package_version_id=package_version.id,
            published_at=utcnow(),
        )
        db.add(version)
        await db.flush()
        task_versions[item["code"]] = version.id
        if key:
            db.add(AnswerKey(task_version_id=version.id, data=key))
        if criterion:
            db.add(Criterion(task_version_id=version.id, data=criterion))
    variant_ids: list[str] = []
    for item in data.variants.values():
        variant = ExamVariant(subject=data.subject, status="PUBLISHED", published_at=utcnow())
        db.add(variant)
        await db.flush()
        for ordinal, code in enumerate(item["taskCodes"], 1):
            db.add(
                ExamVariantTask(
                    variant_id=variant.id, task_version_id=task_versions[code], ordinal=ordinal
                )
            )
        variant_ids.append(variant.id)
    job = KimImportJob(
        checksum=data.checksum,
        status="SUCCEEDED",
        package_version_id=package_version.id,
        finished_at=utcnow(),
    )
    db.add(job)
    await db.flush()
    record_audit(
        db,
        "KIM_IMPORTED",
        "KimPackageVersion",
        package_version.id,
        actor_id=actor_id,
        context={
            "checksum": data.checksum,
            "subject": data.subject,
            "version": package_version.version,
        },
    )
    await db.commit()
    return {
        "id": job.id,
        "status": job.status,
        "packageVersionId": package_version.id,
        "packageId": package.id,
        "version": package_version.version,
        "subject": data.subject,
        "checksum": data.checksum,
        "variantIds": variant_ids,
    }
