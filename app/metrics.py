from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter("oge_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("oge_http_request_duration_seconds", "HTTP latency", ["method", "route"])
ANSWER_SAVES = Counter("oge_answer_saves_total", "Committed answer saves")
REVISION_CONFLICTS = Counter("oge_answer_revision_conflicts_total", "Revision conflicts")
KIM_IMPORT_ERRORS = Counter("oge_kim_import_errors_total", "Rejected KIM imports")
ACTIVE_SESSIONS = Gauge("oge_active_sessions", "Active examination sessions")
REVIEW_QUEUE = Gauge("oge_review_queue", "Unclaimed review assignments")
DATABASE_UP = Gauge("oge_database_up", "Database readiness")
