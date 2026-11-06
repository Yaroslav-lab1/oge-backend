"""Create an isolated, repeatable cohort for the k6 scenarios.

Run only against the local development database after ``python -m scripts.seed``.
Each cohort gets its own assignment, so warmups cannot consume measured attempts.
"""

import argparse
import asyncio
import json
import os
import re

from sqlalchemy import select

from app.auth import aware_utc, hash_password
from app.config import settings
from app.db import SessionLocal, utcnow
from app.models.auth import Role, User, UserRole
from app.models.content import Exam, ExamAssignment, ExamVariant


async def seed_load(prefix: str, students: int, exam_id: str | None) -> dict:
    if settings.environment != "development":
        raise RuntimeError("Нагрузочные учётные записи создаются только в development")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", prefix):
        raise ValueError("prefix: 1–41 символ, строчные латинские буквы, цифры, _ или -")
    if not 1 <= students <= 5000:
        raise ValueError("students должен быть в диапазоне 1..5000")
    password = os.environ.get("LOAD_PASSWORD")
    if password is None or len(password) < 12:
        raise ValueError("Задайте LOAD_PASSWORD длиной не менее 12 символов")

    async with SessionLocal() as db:
        role = await db.scalar(select(Role).where(Role.name == "STUDENT"))
        if role is None:
            raise RuntimeError("Сначала выполните python -m scripts.seed")
        if exam_id:
            exam = await db.get(Exam, exam_id)
        else:
            exam = await db.scalar(
                select(Exam)
                .join(ExamVariant, ExamVariant.id == Exam.variant_id)
                .where(ExamVariant.subject == "MATH")
                .order_by(Exam.opens_at.desc())
            )
        if exam is None:
            raise ValueError("Экзамен не найден")
        now = utcnow()
        opens = aware_utc(exam.opens_at)
        closes = aware_utc(exam.closes_at)
        if exam.status != "SCHEDULED" or not opens <= now < closes:
            raise ValueError("Экзамен должен быть открыт во время нагрузочного теста")

        created = 0
        for number in range(1, students + 1):
            login = f"{prefix}{number:04d}"
            user = await db.scalar(select(User).where(User.login == login))
            if user is None:
                user = User(login=login, password_hash=hash_password(password), is_active=True)
                db.add(user)
                await db.flush()
                created += 1
            if await db.get(UserRole, (user.id, role.id)) is None:
                db.add(UserRole(user_id=user.id, role_id=role.id))
            assignment = await db.scalar(
                select(ExamAssignment).where(
                    ExamAssignment.exam_id == exam.id,
                    ExamAssignment.student_id == user.id,
                )
            )
            if assignment is None:
                db.add(ExamAssignment(exam_id=exam.id, student_id=user.id))
            if number % 50 == 0:
                await db.commit()
        await db.commit()
        return {
            "examId": exam.id,
            "prefix": prefix,
            "students": students,
            "createdUsers": created,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--students", type=int, default=300)
    parser.add_argument("--exam-id")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(seed_load(args.prefix, args.students, args.exam_id))))


if __name__ == "__main__":
    main()
