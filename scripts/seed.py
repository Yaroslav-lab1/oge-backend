"""Idempotent synthetic demonstration data. Run after `alembic upgrade head`."""

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password
from app.db import SessionLocal, utcnow
from app.kim import import_archive
from app.models.auth import Role, User, UserRole
from app.models.content import (
    Exam,
    ExamAssignment,
    ExamVariant,
    KimPackageVersion,
    ScoringRuleVersion,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO_USERS = {
    "admin": "ADMIN",
    "student": "STUDENT",
    "expert1": "EXPERT",
    "expert2": "EXPERT",
    "senior": "SENIOR_EXPERT",
    "content": "CONTENT_MANAGER",
    "auditor": "AUDITOR",
    "bi": "BI_SERVICE",
}


async def seed_users(db: AsyncSession) -> dict[str, User]:
    password = os.environ.get("SEED_PASSWORD", "Demo-only-ChangeMe-123!")
    if len(password) < 12:
        raise ValueError("SEED_PASSWORD должен содержать минимум 12 символов")
    users: dict[str, User] = {}
    for login, role_name in DEMO_USERS.items():
        role = await db.scalar(select(Role).where(Role.name == role_name))
        if role is None:
            role = Role(name=role_name)
            db.add(role)
            await db.flush()
        user = await db.scalar(select(User).where(User.login == login))
        if user is None:
            user = User(login=login, password_hash=hash_password(password), is_active=True)
            db.add(user)
            await db.flush()
        user_role = await db.get(UserRole, (user.id, role.id))
        if user_role is None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
        users[login] = user
    await db.commit()
    return users


async def seed_packages(db: AsyncSession, content_user: User) -> None:
    for filename in ("math-valid.zip", "physics-valid.zip"):
        payload = (ROOT / "examples" / "kim" / filename).read_bytes()
        checksum = hashlib.sha256(payload).hexdigest()
        if await db.scalar(select(KimPackageVersion).where(KimPackageVersion.checksum == checksum)):
            continue
        await import_archive(db, payload, content_user.id)


async def seed_rules_and_exams(db: AsyncSession, student: User) -> None:
    for subject in ("MATH", "PHYS"):
        rule = await db.scalar(
            select(ScoringRuleVersion)
            .where(ScoringRuleVersion.subject == subject)
            .order_by(ScoringRuleVersion.version)
        )
        if rule is None:
            rule = ScoringRuleVersion(
                subject=subject,
                version=1,
                effective_from=datetime(2020, 1, 1, tzinfo=UTC),
                thresholds={"3": 1, "4": 2, "5": 2},
                max_primary=2,
                geometry_min=2 if subject == "MATH" else 0,
                discrepancy_threshold=1,
            )
            db.add(rule)
            await db.flush()
        variant = await db.scalar(
            select(ExamVariant)
            .where(ExamVariant.subject == subject, ExamVariant.status == "PUBLISHED")
            .order_by(ExamVariant.created_at)
        )
        if variant is None:
            raise RuntimeError(f"Не найден опубликованный синтетический вариант: {subject}")
        exam = await db.scalar(select(Exam).where(Exam.variant_id == variant.id))
        if exam is None:
            now = utcnow()
            exam = Exam(
                variant_id=variant.id,
                scoring_rule_id=rule.id,
                opens_at=now - timedelta(hours=1),
                closes_at=now + timedelta(days=7),
                duration_seconds=3 * 3600,
                status="SCHEDULED",
            )
            db.add(exam)
            await db.flush()
        assignment = await db.scalar(
            select(ExamAssignment).where(
                ExamAssignment.exam_id == exam.id, ExamAssignment.student_id == student.id
            )
        )
        if assignment is None:
            db.add(ExamAssignment(exam_id=exam.id, student_id=student.id))
    await db.commit()


async def main() -> None:
    async with SessionLocal() as db:
        users = await seed_users(db)
        await seed_packages(db, users["content"])
        await seed_rules_and_exams(db, users["student"])
        versions = (await db.scalars(select(KimPackageVersion))).all()
        print(f"Synthetic demo ready: {len(users)} users, {len(versions)} KIM package versions")


if __name__ == "__main__":
    asyncio.run(main())
