"""HTTP persistence probe for the isolated Compose CI stack; no token is printed."""

import asyncio
import json
import os
import sys
import urllib.request

from sqlalchemy import select

from app.db import SessionLocal
from app.models.auth import User
from app.models.content import ExamAssignment


def request(method: str, path: str, payload: dict | None = None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:8000/api/v1{path}", data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


async def assigned_exam() -> str:
    async with SessionLocal() as db:
        exam_id = await db.scalar(
            select(ExamAssignment.exam_id)
            .join(User, User.id == ExamAssignment.student_id)
            .where(User.login == "student")
            .order_by(ExamAssignment.exam_id)
            .limit(1)
        )
        if exam_id is None:
            raise RuntimeError("Run the synthetic seed first")
        return exam_id


def main() -> None:
    token = request(
        "POST",
        "/auth/login",
        {"login": "student", "password": os.environ["SEED_PASSWORD"]},
    )["access_token"]
    if sys.argv[1] == "prepare":
        exam = asyncio.run(assigned_exam())
        session = request("POST", f"/exams/{exam}/sessions", token=token)
        task = session["tasks"][0]
        answer = request(
            "PUT",
            f"/sessions/{session['id']}/answers/{task['taskId']}",
            {"raw": "synthetic-restart-proof", "expectedRevision": 0},
            token,
        )
        print(json.dumps({"sessionId": session["id"], "answer": answer}))
    elif sys.argv[1] == "verify":
        state = json.load(sys.stdin)
        session = request("GET", f"/sessions/{state['sessionId']}", token=token)
        task = next(t for t in session["tasks"] if t["taskId"] == state["answer"]["taskId"])
        assert task["answer"]["revision"] == state["answer"]["revision"]
        assert task["answer"]["raw"] == state["answer"]["raw"]
        print("Confirmed answer survived API restart")
    else:
        raise ValueError("Expected prepare or verify")


if __name__ == "__main__":
    main()
