"""KIM ZIP validation, import provenance and version history endpoints."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_roles
from app.db import get_db
from app.errors import ApiError, not_found
from app.kim import MAX_ARCHIVE_BYTES, import_archive, validate_archive
from app.metrics import KIM_IMPORT_ERRORS
from app.models.auth import User
from app.models.content import KimImportIssue, KimImportJob, KimPackage, KimPackageVersion

router = APIRouter(tags=["kim"])
Manager = Annotated[User, Depends(require_roles("CONTENT_MANAGER", "ADMIN"))]
Db = Annotated[AsyncSession, Depends(get_db)]
ZipBody = Annotated[
    bytes, Body(media_type="application/zip", description="ZIP-пакет КИМ, максимум 50 МБ")
]


class IssueOut(BaseModel):
    code: str
    message: str
    path: str | None


class ValidationOut(BaseModel):
    valid: bool
    checksum: str
    subject: str | None
    schemaVersion: str | None
    sourceVersion: str | None
    files: int
    issues: list[IssueOut]


class ImportOut(BaseModel):
    id: str
    status: str
    packageVersionId: str
    checksum: str
    packageId: str | None = None
    version: int | None = None
    subject: str | None = None
    variantIds: list[str] | None = None


class JobOut(BaseModel):
    id: str
    status: str
    checksum: str
    packageVersionId: str | None
    createdAt: datetime
    finishedAt: datetime | None
    issues: list[IssueOut]


class PackageOut(BaseModel):
    id: str
    subject: str
    name: str


class PackageVersionOut(BaseModel):
    id: str
    version: int
    schemaVersion: str
    sourceVersion: str
    checksum: str
    importedAt: datetime


@router.post("/kim-imports/validate", response_model=ValidationOut)
async def validate_kim(payload: ZipBody, actor: Manager) -> dict:
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ApiError(413, "ARCHIVE_TOO_LARGE", "Размер ZIP превышает 50 МБ")
    return validate_archive(payload).report()


@router.post("/kim-imports", status_code=status.HTTP_201_CREATED, response_model=ImportOut)
async def import_kim(payload: ZipBody, db: Db, actor: Manager) -> dict:
    if len(payload) > MAX_ARCHIVE_BYTES:
        KIM_IMPORT_ERRORS.inc()
        raise ApiError(413, "ARCHIVE_TOO_LARGE", "Размер ZIP превышает 50 МБ")
    return await import_archive(db, payload, actor.id)


@router.get("/kim-imports/{job_id}", response_model=JobOut)
async def get_import(job_id: str, db: Db, actor: Manager) -> dict:
    job = await db.get(KimImportJob, job_id)
    if job is None:
        raise not_found("Импорт не найден")
    issues = (await db.scalars(select(KimImportIssue).where(KimImportIssue.job_id == job_id))).all()
    return {
        "id": job.id,
        "status": job.status,
        "checksum": job.checksum,
        "packageVersionId": job.package_version_id,
        "createdAt": job.created_at,
        "finishedAt": job.finished_at,
        "issues": [{"code": i.code, "message": i.message, "path": i.path} for i in issues],
    }


@router.get("/kim-packages", response_model=list[PackageOut])
async def list_packages(
    db: Db, actor: Manager, subject: Literal["MATH", "PHYS"] | None = None
) -> list[dict]:
    query = select(KimPackage).order_by(KimPackage.subject, KimPackage.name)
    if subject:
        query = query.where(KimPackage.subject == subject)
    packages = (await db.scalars(query)).all()
    return [{"id": p.id, "subject": p.subject, "name": p.name} for p in packages]


@router.get("/kim-packages/{package_id}/versions", response_model=list[PackageVersionOut])
async def list_package_versions(package_id: str, db: Db, actor: Manager) -> list[dict]:
    package = await db.get(KimPackage, package_id)
    if package is None:
        raise not_found("Пакет КИМ не найден")
    versions = (
        await db.scalars(
            select(KimPackageVersion)
            .where(KimPackageVersion.package_id == package_id)
            .order_by(KimPackageVersion.version)
        )
    ).all()
    return [
        {
            "id": v.id,
            "version": v.version,
            "schemaVersion": v.schema_version,
            "sourceVersion": v.source_version,
            "checksum": v.checksum,
            "importedAt": v.imported_at,
        }
        for v in versions
    ]
