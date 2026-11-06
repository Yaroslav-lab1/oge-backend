"""Validate the saved contract and ensure it matches the application routes."""

import json
from pathlib import Path

from openapi_spec_validator import validate_spec

from app.main import app

REQUIRED = {
    ("post", "/api/v1/auth/login"),
    ("post", "/api/v1/auth/refresh"),
    ("post", "/api/v1/auth/logout"),
    ("get", "/api/v1/users/me"),
    ("post", "/api/v1/admin/users"),
    ("patch", "/api/v1/admin/users/{user_id}"),
    ("post", "/api/v1/tasks"),
    ("post", "/api/v1/tasks/{task_id}/versions"),
    ("post", "/api/v1/task-versions/{version_id}/publish"),
    ("post", "/api/v1/variants"),
    ("post", "/api/v1/variants/{variant_id}/publish"),
    ("post", "/api/v1/exams"),
    ("post", "/api/v1/exams/{exam_id}/assignments"),
    ("post", "/api/v1/exams/{exam_id}/sessions"),
    ("get", "/api/v1/sessions/{session_id}"),
    ("put", "/api/v1/sessions/{session_id}/answers/{task_id}"),
    ("post", "/api/v1/sessions/{session_id}/audio/{task_id}/play"),
    ("get", "/api/v1/sessions/{session_id}/audio-stream"),
    ("post", "/api/v1/sessions/{session_id}/proctor-events"),
    ("post", "/api/v1/sessions/{session_id}/submit"),
    ("get", "/api/v1/reviews/queue"),
    ("post", "/api/v1/reviews/{review_id}/claim"),
    ("put", "/api/v1/reviews/{review_id}/scores"),
    ("post", "/api/v1/reviews/{review_id}/finalize"),
    ("get", "/api/v1/results/{session_id}"),
    ("post", "/api/v1/results/{session_id}/recalculate"),
    ("get", "/api/v1/audit/events"),
    ("get", "/api/v1/exports/results"),
    ("get", "/api/v1/health/live"),
    ("get", "/api/v1/health/ready"),
    ("get", "/api/v1/metrics"),
    ("post", "/api/v1/kim-imports/validate"),
    ("post", "/api/v1/kim-imports"),
    ("get", "/api/v1/kim-imports/{job_id}"),
    ("get", "/api/v1/kim-packages"),
    ("get", "/api/v1/kim-packages/{package_id}/versions"),
}


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "openapi.yaml"
    document = json.loads(target.read_text(encoding="utf-8"))
    validate_spec(document)
    if document != app.openapi():
        raise SystemExit("openapi.yaml is stale; run python -m scripts.export_openapi")
    actual = {
        (method, path)
        for path, operation in document["paths"].items()
        for method in operation
        if method in {"get", "post", "put", "patch", "delete"}
    }
    missing = REQUIRED - actual
    if missing:
        raise SystemExit(f"Missing required operations: {sorted(missing)}")
    print(f"OpenAPI valid; {len(actual)} documented operations; {len(REQUIRED)} required")


if __name__ == "__main__":
    main()
