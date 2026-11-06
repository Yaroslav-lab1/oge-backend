"""KIM validation, provenance and duplicate-import invariants."""

import json
from pathlib import Path

import pytest
import pytest_asyncio
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.errors import ApiError
from app.kim import import_archive, validate_archive
from app.models import *  # noqa: F403
from app.models.auth import User
from app.models.content import (
    KimImportIssue,
    KimImportJob,
    KimPackage,
    KimPackageFile,
    KimPackageVersion,
    Resource,
    TaskVersion,
)
from examples.kim.build_examples import encode, files_for, pack

FIXTURES = Path(__file__).resolve().parents[1] / "examples" / "kim"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_valid_math_and_physics_packages():
    for name, subject in (("math-valid.zip", "MATH"), ("physics-valid.zip", "PHYS")):
        report = validate_archive(fixture(name)).report()
        assert report["valid"], report["issues"]
        assert report["subject"] == subject
        assert report["schemaVersion"] == f"{subject}-OGE-SCHEMA-1.0"


def test_checksum_and_traversal_rejected():
    checksum = validate_archive(fixture("invalid-checksum.zip"))
    traversal = validate_archive(fixture("invalid-traversal.zip"))
    assert "CHECKSUM_MISMATCH" in {i.code for i in checksum.issues}
    assert "UNSAFE_PATH" in {i.code for i in traversal.issues}


def test_external_link_and_pii_rejected():
    files = files_for("MATH")
    files["instruction.md"] = b"See https://example.invalid and admin@example.invalid\n"
    result = validate_archive(pack(files))
    assert not result.valid
    assert {"EXTERNAL_LINK", "POTENTIAL_PII"} <= {issue.code for issue in result.issues}


def test_invalid_relation_and_schema_rejected():
    files = files_for("MATH")
    tasks = [
        {
            "code": "M-GEO-01",
            "topicCode": "UNKNOWN",
            "answerType": "NUMBER",
            "body": {"text": "Учебная задача"},
            "maxScore": 1,
        }
    ]
    files["tasks.json"] = encode(tasks)
    assert "UNKNOWN_TOPIC" in {i.code for i in validate_archive(pack(files)).issues}
    passport = {
        "subject": "MATH",
        "name": "Synthetic mathematics KIM",
        "schemaVersion": "UNKNOWN",
        "sourceVersion": "1.0",
    }
    files["passport.json"] = encode(passport)
    assert "SCHEMA_MISMATCH" in {i.code for i in validate_archive(pack(files)).issues}


def test_missing_key_malformed_json_variant_and_resource_rejected():
    files = files_for("MATH")
    files["keys.json"] = encode({})
    assert "MISSING_KEY" in {i.code for i in validate_archive(pack(files)).issues}

    files = files_for("MATH")
    files["topics.json"] = b"not-json"
    assert "INVALID_JSON" in {i.code for i in validate_archive(pack(files)).issues}

    files = files_for("MATH")
    files["variants/variant-01.json"] = encode({"taskCodes": ["UNKNOWN"]})
    assert "INVALID_VARIANT" in {i.code for i in validate_archive(pack(files)).issues}

    files = files_for("MATH")
    files["materials.json"] = encode([{"path": "resources/missing.wav"}])
    assert "MISSING_RESOURCE" in {i.code for i in validate_archive(pack(files)).issues}


@pytest.mark.parametrize("limit", [0, 11, True, "3", 1.5])
def test_kim_rejects_invalid_audio_play_limit(limit):
    files = files_for("PHYS")
    tasks = json.loads(files["tasks.json"])
    tasks[0]["body"]["audioPlayLimit"] = limit
    files["tasks.json"] = encode(tasks)
    assert "INVALID_AUDIO_PLAY_LIMIT" in {
        issue.code for issue in validate_archive(pack(files)).issues
    }


def test_executable_and_zip_bomb_rejected():
    files = files_for("MATH")
    assert "UNSAFE_FILE" in {
        i.code for i in validate_archive(pack(files, extra_path="resources/x.js")).issues
    }
    files["resources/high-ratio.pdf"] = b"0" * (2 * 1024 * 1024)
    assert "ZIP_BOMB" in {i.code for i in validate_archive(pack(files)).issues}


@pytest.mark.asyncio
async def test_import_is_atomic_and_duplicate_checksum_does_not_add_version(db):
    actor = User(login="synthetic-manager", password_hash="synthetic")
    db.add(actor)
    await db.commit()
    with pytest.raises(ApiError) as exc:
        await import_archive(db, fixture("invalid-checksum.zip"), actor.id)
    assert exc.value.status_code == 422
    assert await db.scalar(select(func.count()).select_from(KimPackageVersion)) == 0
    failed_job = await db.get(KimImportJob, exc.value.details["jobId"])
    assert failed_job.status == "FAILED"
    assert await db.scalar(select(func.count()).select_from(KimImportIssue)) >= 1

    payload = fixture("math-valid.zip")
    first = await import_archive(db, payload, actor.id)
    again = await import_archive(db, payload, actor.id)
    assert first["status"] == "SUCCEEDED"
    assert again["status"] == "DUPLICATE"
    assert again["packageVersionId"] == first["packageVersionId"]
    assert await db.scalar(select(func.count()).select_from(KimPackageVersion)) == 1


@pytest.mark.asyncio
async def test_subject_versions_and_resource_provenance(db):
    actor = User(login="synthetic-manager", password_hash="synthetic")
    db.add(actor)
    await db.commit()
    math = await import_archive(db, fixture("math-valid.zip"), actor.id)
    phys = await import_archive(db, fixture("physics-valid.zip"), actor.id)
    assert math["version"] == phys["version"] == 1
    assert {math["subject"], phys["subject"]} == {"MATH", "PHYS"}
    assert await db.scalar(select(func.count()).select_from(KimPackage)) == 2
    package_file = await db.scalar(
        select(KimPackageFile).where(
            KimPackageFile.package_version_id == phys["packageVersionId"],
            KimPackageFile.path == "resources/instruction.wav",
        )
    )
    resource = await db.scalar(
        select(Resource).where(Resource.package_version_id == phys["packageVersionId"])
    )
    assert package_file.content == resource.content
    task_version = await db.scalar(
        select(TaskVersion).where(TaskVersion.package_version_id == phys["packageVersionId"])
    )
    assert task_version.audio_resource_id == resource.id
    assert task_version.status == "PUBLISHED"
    assert task_version.body["audioPlayLimit"] == 2


@pytest.mark.asyncio
async def test_new_package_version_preserves_older_published_task(db):
    actor = User(login="synthetic-manager", password_hash="synthetic")
    db.add(actor)
    await db.commit()
    first = await import_archive(db, fixture("math-valid.zip"), actor.id)
    files = files_for("MATH")
    passport = {
        "subject": "MATH",
        "name": "Synthetic mathematics KIM",
        "schemaVersion": "MATH-OGE-SCHEMA-1.0",
        "sourceVersion": "1.1",
    }
    files["passport.json"] = encode(passport)
    tasks = [
        {
            "code": "M-GEO-01",
            "topicCode": "GEOMETRY",
            "answerType": "NUMBER",
            "body": {"text": "Новая синтетическая редакция"},
            "maxScore": 2,
            "geometry": True,
        }
    ]
    files["tasks.json"] = encode(tasks)
    second = await import_archive(db, pack(files), actor.id)
    assert first["version"] == 1
    assert second["version"] == 2
    versions = (await db.scalars(select(TaskVersion).order_by(TaskVersion.version))).all()
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].body != versions[1].body
    assert versions[0].status == versions[1].status == "PUBLISHED"


@given(st.binary(max_size=256))
def test_arbitrary_bytes_never_pass_validation(raw):
    assert not validate_archive(raw).valid
