"""API checks for independent review, adjudication, result access and BI export."""

import json
from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth import issue_token_pair
from app.db import Base, get_db, utcnow
from app.errors import ApiError
from app.models import *  # noqa: F403
from app.models.auth import Role, User, UserRole
from app.models.content import (
    Exam,
    ExamAssignment,
    ExamVariant,
    ExamVariantTask,
    ScoringRuleVersion,
    Task,
    TaskVersion,
)
from app.models.review import ResultVersion, ReviewAssignment, ReviewDispute
from app.models.session import Answer, ExamSession
from app.routers import results, review
from app.routers.results import _pseudonym
from app.routers.review import _criterion_limits
from app.scoring import process_submission


@pytest_asyncio.fixture
async def api_state():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    people = {
        "owner": "STUDENT",
        "other": "STUDENT",
        "expert1": "EXPERT",
        "expert2": "EXPERT",
        "senior": "SENIOR_EXPERT",
        "admin": "ADMIN",
        "bi": "BI_SERVICE",
        "auditor": "AUDITOR",
    }
    async with maker() as db:
        roles = {name: Role(name=name) for name in set(people.values())}
        users = {name: User(login=f"{name}-login", password_hash="synthetic") for name in people}
        db.add_all([*roles.values(), *users.values()])
        await db.flush()
        for name, role_name in people.items():
            db.add(UserRole(user_id=users[name].id, role_id=roles[role_name].id))

        rule1 = ScoringRuleVersion(
            subject="MATH",
            version=1,
            effective_from=utcnow() - timedelta(days=1),
            thresholds={"3": 1, "4": 2, "5": 4},
            max_primary=4,
            geometry_min=2,
            discrepancy_threshold=0,
        )
        rule2 = ScoringRuleVersion(
            subject="MATH",
            version=2,
            effective_from=utcnow(),
            thresholds={"3": 1, "4": 2, "5": 5},
            max_primary=4,
            geometry_min=2,
            discrepancy_threshold=0,
        )
        variant = ExamVariant(subject="MATH", status="PUBLISHED")
        essay_task = Task(subject="MATH", code="ESSAY-1")
        geo_task = Task(subject="MATH", code="GEO-1")
        db.add_all([rule1, rule2, variant, essay_task, geo_task])
        await db.flush()

        essay_version = TaskVersion(
            task_id=essay_task.id,
            version=1,
            status="PUBLISHED",
            body={"text": "Синтетический развёрнутый ответ", "topicId": "ALG", "difficulty": 0.7},
            answer_type="ESSAY",
            key={},
            normalization={},
            criteria={"C1": 2},
            max_score=2,
            geometry=False,
        )
        geo_version = TaskVersion(
            task_id=geo_task.id,
            version=1,
            status="PUBLISHED",
            body={"text": "Синтетическая геометрия", "topicId": "GEO", "difficulty": 0.4},
            answer_type="NUMBER",
            key={"value": "2"},
            normalization={},
            criteria={},
            max_score=2,
            geometry=True,
        )
        db.add_all([essay_version, geo_version])
        await db.flush()
        db.add_all(
            [
                ExamVariantTask(variant_id=variant.id, task_version_id=essay_version.id, ordinal=1),
                ExamVariantTask(variant_id=variant.id, task_version_id=geo_version.id, ordinal=2),
            ]
        )
        exam = Exam(
            variant_id=variant.id,
            scoring_rule_id=rule1.id,
            opens_at=utcnow() - timedelta(hours=1),
            closes_at=utcnow() + timedelta(hours=1),
            duration_seconds=3600,
        )
        db.add(exam)
        await db.flush()
        assignment = ExamAssignment(exam_id=exam.id, student_id=users["owner"].id)
        db.add(assignment)
        await db.flush()
        exam_session = ExamSession(
            assignment_id=assignment.id,
            status="SUBMITTED",
            started_at=utcnow() - timedelta(minutes=5),
            deadline_at=utcnow() + timedelta(minutes=55),
        )
        db.add(exam_session)
        await db.flush()
        db.add_all(
            [
                Answer(
                    session_id=exam_session.id,
                    task_version_id=essay_version.id,
                    raw="private essay answer",
                    revision=1,
                ),
                Answer(
                    session_id=exam_session.id,
                    task_version_id=geo_version.id,
                    raw="2",
                    revision=1,
                ),
            ]
        )
        await db.flush()
        await process_submission(db, exam_session.id)
        reviews = (
            await db.scalars(
                select(ReviewAssignment)
                .where(ReviewAssignment.session_id == exam_session.id)
                .order_by(ReviewAssignment.round)
            )
        ).all()
        tokens = {name: issue_token_pair(user, db)["access_token"] for name, user in users.items()}
        ids = {
            "session": exam_session.id,
            "student": users["owner"].id,
            "essayTaskVersion": essay_version.id,
            "geoTaskVersion": geo_version.id,
            "rule1": rule1.id,
            "rule2": rule2.id,
            "review1": reviews[0].id,
            "review2": reviews[1].id,
        }
        await db.commit()

    async def override_get_db():
        async with maker() as db:
            yield db

    app = FastAPI()
    app.dependency_overrides[get_db] = override_get_db
    app.include_router(review.router, prefix="/api/v1")
    app.include_router(results.router, prefix="/api/v1")

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "correlationId": "test-correlation-id",
            },
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {name: {"Authorization": f"Bearer {token}"} for name, token in tokens.items()}
        yield client, maker, headers, ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_independent_review_dispute_access_and_versioned_result(api_state):
    client, maker, headers, ids = api_state
    base = "/api/v1"
    first, second = ids["review1"], ids["review2"]
    session_id = ids["session"]

    pending_result = await client.get(f"{base}/results/{session_id}", headers=headers["owner"])
    assert pending_result.status_code == 404
    queue = await client.get(f"{base}/reviews/queue", headers=headers["expert1"])
    assert queue.status_code == 200
    assert len(queue.json()["items"]) == 2
    queue_text = json.dumps(queue.json(), ensure_ascii=False)
    assert ids["student"] not in queue_text
    assert "owner-login" not in queue_text
    assert session_id not in queue_text
    assert "private essay answer" in queue_text

    claim = await client.post(f"{base}/reviews/{first}/claim", headers=headers["expert1"])
    assert claim.status_code == 200, claim.text
    competing = await client.post(f"{base}/reviews/{first}/claim", headers=headers["expert2"])
    assert competing.status_code == 409
    foreign = await client.put(
        f"{base}/reviews/{first}/scores", json={"scores": {"C1": 1}}, headers=headers["expert2"]
    )
    assert foreign.status_code == 403
    no_scores = await client.post(f"{base}/reviews/{first}/finalize", headers=headers["expert1"])
    assert no_scores.status_code == 409
    invalid = await client.put(
        f"{base}/reviews/{first}/scores", json={"scores": {"C1": 3}}, headers=headers["expert1"]
    )
    assert invalid.status_code == 422
    saved = await client.put(
        f"{base}/reviews/{first}/scores", json={"scores": {"C1": 1}}, headers=headers["expert1"]
    )
    assert saved.status_code == 200
    assert saved.json()["totalScore"] == 1
    finalized = await client.post(f"{base}/reviews/{first}/finalize", headers=headers["expert1"])
    assert finalized.status_code == 200
    assert finalized.json()["resultReady"] is False

    same_expert = await client.post(f"{base}/reviews/{second}/claim", headers=headers["expert1"])
    assert same_expert.status_code == 403
    assert (
        await client.post(f"{base}/reviews/{second}/claim", headers=headers["expert2"])
    ).status_code == 200
    assert (
        await client.put(
            f"{base}/reviews/{second}/scores",
            json={"scores": {"C1": 2}},
            headers=headers["expert2"],
        )
    ).status_code == 200
    second_final = await client.post(
        f"{base}/reviews/{second}/finalize", headers=headers["expert2"]
    )
    assert second_final.status_code == 200
    assert second_final.json()["resultReady"] is False

    assert (
        await client.get(f"{base}/reviews/disputes", headers=headers["expert1"])
    ).status_code == 403
    disputes = await client.get(f"{base}/reviews/disputes", headers=headers["senior"])
    assert disputes.status_code == 200
    assert len(disputes.json()["items"]) == 1
    dispute_id = disputes.json()["items"][0]["id"]
    assert (
        await client.post(
            f"{base}/reviews/disputes/{dispute_id}/resolve",
            json={"scores": {"C1": 3}},
            headers=headers["senior"],
        )
    ).status_code == 422
    resolved = await client.post(
        f"{base}/reviews/disputes/{dispute_id}/resolve",
        json={"scores": {"C1": 2}},
        headers=headers["senior"],
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["scores"] == {"C1": 2}
    assert resolved.json()["resultReady"] is True
    assert (
        await client.post(
            f"{base}/reviews/disputes/{dispute_id}/resolve",
            json={"scores": {"C1": 2}},
            headers=headers["senior"],
        )
    ).status_code == 409

    own_result = await client.get(f"{base}/results/{session_id}", headers=headers["owner"])
    assert own_result.status_code == 200, own_result.text
    assert own_result.json()["grade"] == 5
    assert own_result.json()["checkingStatus"] == "COMPLETED"
    assert own_result.json()["primaryScore"] == 4
    assert own_result.json()["scoringRuleVersion"] == 1
    assert (
        await client.get(f"{base}/results/{session_id}", headers=headers["other"])
    ).status_code == 403
    assert (
        await client.get(f"{base}/results/{session_id}", headers=headers["expert1"])
    ).status_code == 403

    recalc = await client.post(
        f"{base}/results/{session_id}/recalculate",
        json={"reason": "Новая шкала", "scoringRuleId": ids["rule2"]},
        headers=headers["admin"],
    )
    assert recalc.status_code == 200, recalc.text
    assert recalc.json()["version"] == 2
    assert recalc.json()["grade"] == 4
    assert recalc.json()["scoringRuleVersion"] == 2
    history = await client.get(f"{base}/results/{session_id}/history", headers=headers["owner"])
    assert history.status_code == 200
    assert [(item["version"], item["grade"]) for item in history.json()["items"]] == [
        (1, 5),
        (2, 4),
    ]
    assert (await client.get(f"{base}/results/{session_id}", headers=headers["owner"])).json()[
        "version"
    ] == 2

    assert (
        await client.get(f"{base}/exports/results", headers=headers["owner"])
    ).status_code == 403
    exported = await client.get(f"{base}/exports/results", headers=headers["bi"])
    assert exported.status_code == 200, exported.text
    assert exported.json()["count"] == 1
    row = exported.json()["items"][0]
    assert row["grade"] == 4
    assert row["scoringRuleVersion"] == 2
    assert row["respondent"] != session_id
    assert row["totalDifficulty"] == pytest.approx(1.1)
    assert row["averageDifficulty"] == pytest.approx(0.55)
    export_text = json.dumps(exported.json(), ensure_ascii=False)
    for forbidden_value in ("owner-login", "private essay answer", ids["student"], session_id):
        assert forbidden_value not in export_text

    assert (await client.get(f"{base}/audit/events", headers=headers["owner"])).status_code == 403
    events = await client.get(f"{base}/audit/events", headers=headers["auditor"])
    assert events.status_code == 200
    assert "RESULT_RECALCULATED" in {item["action"] for item in events.json()["items"]}

    async with maker() as db:
        versions = (
            await db.scalars(
                select(ResultVersion)
                .where(ResultVersion.session_id == session_id)
                .order_by(ResultVersion.version)
            )
        ).all()
        dispute = await db.scalar(select(ReviewDispute).where(ReviewDispute.id == dispute_id))
        assert len(versions) == 2
        assert versions[0].id != versions[1].id
        assert dispute.senior_expert_id is not None
        assert dispute.final_score == 2


def test_criterion_limits_accepts_versioned_list_and_mapping_shapes():
    criteria_list = {"criteria": [{"code": "C1", "maxScore": 2}, {"id": "C2", "max_score": 1}]}
    criteria_map = {"C1": {"maxScore": 2}, "C2": 1}
    assert _criterion_limits(criteria_list) == {"C1": 2, "C2": 1}
    assert _criterion_limits(criteria_map) == {"C1": 2, "C2": 1}
    assert _criterion_limits({}) == {}


def test_criterion_without_identifier_is_not_a_valid_scoring_key():
    # A malformed imported criterion must not become a scoreable key named "None".
    assert _criterion_limits({"criteria": [{"maxScore": 2}]}) == {}


@given(st.uuids())
def test_bi_pseudonym_is_stable_and_does_not_reveal_session_id(session_id):
    raw = str(session_id)
    pseudonym = _pseudonym(raw)
    assert pseudonym == _pseudonym(raw)
    assert len(pseudonym) == 24
    assert all(char in "0123456789abcdef" for char in pseudonym)
    assert pseudonym != raw


@pytest.mark.asyncio
async def test_protected_review_result_export_and_audit_routes_reject_wrong_roles(api_state):
    client, _maker, headers, ids = api_state
    base = "/api/v1"
    session_id = ids["session"]
    assert (await client.get(f"{base}/reviews/queue")).status_code == 401
    assert (await client.get(f"{base}/reviews/queue", headers=headers["owner"])).status_code == 403
    assert (
        await client.post(f"{base}/reviews/{ids['review1']}/claim", headers=headers["owner"])
    ).status_code == 403
    assert (
        await client.post(
            f"{base}/results/{session_id}/recalculate",
            json={"reason": "Недостаточно прав"},
            headers=headers["owner"],
        )
    ).status_code == 403
    assert (
        await client.get(f"{base}/exports/results", headers=headers["owner"])
    ).status_code == 403
    assert (await client.get(f"{base}/audit/events", headers=headers["owner"])).status_code == 403
    assert (
        await client.get(f"{base}/results/{session_id}", headers=headers["other"])
    ).status_code == 403
    assert (
        await client.get(f"{base}/results/{session_id}/history", headers=headers["other"])
    ).status_code == 403


@pytest.mark.asyncio
async def test_claim_is_unique_until_lease_expires_and_old_owner_loses_access(api_state):
    client, maker, headers, ids = api_state
    review_id = ids["review1"]
    url = f"/api/v1/reviews/{review_id}"
    assert (await client.post(f"{url}/claim", headers=headers["expert1"])).status_code == 200
    assert (await client.post(f"{url}/claim", headers=headers["expert1"])).status_code == 403
    assert (await client.post(f"{url}/claim", headers=headers["expert2"])).status_code == 409

    async with maker() as db:
        await db.execute(
            update(ReviewAssignment)
            .where(ReviewAssignment.id == review_id)
            .values(lease_until=utcnow() - timedelta(seconds=1))
        )
        await db.commit()
    assert (await client.post(f"{url}/claim", headers=headers["expert2"])).status_code == 200
    assert (
        await client.put(f"{url}/scores", json={"scores": {"C1": 1}}, headers=headers["expert1"])
    ).status_code == 403
    assert (
        await client.put(f"{url}/scores", json={"scores": {"C1": 1}}, headers=headers["expert2"])
    ).status_code == 200


@pytest.mark.asyncio
async def test_dispute_out_of_bounds_decisions_leave_pending_state_untouched(api_state):
    client, maker, headers, ids = api_state
    async with maker() as db:
        dispute = ReviewDispute(
            session_id=ids["session"],
            task_version_id=ids["essayTaskVersion"],
            first_score=0,
            second_score=2,
            threshold=0,
        )
        db.add(dispute)
        await db.commit()
        dispute_id = dispute.id
    url = f"/api/v1/reviews/disputes/{dispute_id}/resolve"
    assert (
        await client.post(url, json={"scores": {"C1": -1}}, headers=headers["senior"])
    ).status_code == 422
    assert (
        await client.post(url, json={"scores": {"C1": 3}}, headers=headers["senior"])
    ).status_code == 422
    async with maker() as db:
        unchanged = await db.get(ReviewDispute, dispute_id)
        assert unchanged.status == "PENDING"
        assert unchanged.final_score is None
        assert unchanged.senior_expert_id is None
    accepted = await client.post(url, json={"scores": {"C1": 2}}, headers=headers["senior"])
    assert accepted.status_code == 200
    assert accepted.json()["resultReady"] is False  # two independent reviews are still required


@pytest.mark.asyncio
async def test_bi_export_exposes_only_anonymized_latest_result_data(api_state):
    client, maker, headers, ids = api_state
    async with maker() as db:
        db.add(
            ResultVersion(
                session_id=ids["session"],
                version=1,
                primary_score=2,
                max_score=4,
                grade=4,
                reason=None,
                breakdown=[
                    {"taskVersionId": ids["essayTaskVersion"], "score": 0, "maxScore": 2},
                    {"taskVersionId": ids["geoTaskVersion"], "score": 2, "maxScore": 2},
                ],
                rule_id=ids["rule1"],
                rule_version=1,
                published_at=utcnow(),
            )
        )
        await db.commit()
    first = await client.get("/api/v1/exports/results", headers=headers["bi"])
    second = await client.get("/api/v1/exports/results", headers=headers["bi"])
    assert first.status_code == second.status_code == 200
    assert first.json()["count"] == 1
    row = first.json()["items"][0]
    assert row["respondent"] == second.json()["items"][0]["respondent"]
    assert row["subject"] == "MATH"
    assert row["primaryScore"] == 2
    assert {item["taskCode"] for item in row["items"]} == {"ESSAY-1", "GEO-1"}
    serialized = json.dumps(first.json(), ensure_ascii=False)
    for forbidden_value in (
        ids["session"],
        ids["student"],
        "owner-login",
        "private essay answer",
        "synthetic",
    ):
        assert forbidden_value not in serialized
