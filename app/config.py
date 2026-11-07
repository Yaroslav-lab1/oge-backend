import os
from dataclasses import dataclass
from urllib.parse import urlsplit

_DEVELOPMENT_SECRETS = {
    "development-only-change-me",
    "development-only-change-me-at-least-32-bytes",
    "development-metrics-token",
    "replace-with-a-long-random-local-secret",
    "replace-with-a-long-random-metrics-token",
}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./oge.db")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    jwt_secret: str = os.getenv("JWT_SECRET", "development-only-change-me-at-least-32-bytes")
    access_minutes: int = int(os.getenv("ACCESS_TOKEN_MINUTES", "15"))
    refresh_days: int = int(os.getenv("REFRESH_TOKEN_DAYS", "7"))
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    )
    metrics_token: str = os.getenv("METRICS_TOKEN", "development-metrics-token")
    audit_retention_days: int = int(os.getenv("AUDIT_RETENTION_DAYS", "180"))
    environment: str = os.getenv("ENVIRONMENT", "development")
    max_request_bytes: int = int(os.getenv("MAX_REQUEST_BYTES", str(52 * 1024 * 1024)))

    def __post_init__(self) -> None:
        if self.environment.lower() not in {"prod", "production"}:
            return
        for name, secret in (
            ("JWT_SECRET", self.jwt_secret),
            ("METRICS_TOKEN", self.metrics_token),
        ):
            if len(secret.encode()) < 32 or secret in _DEVELOPMENT_SECRETS:
                raise ValueError(f"{name} must be set to a non-default value in production")

        database = urlsplit(self.database_url)
        if (
            database.scheme != "postgresql+asyncpg"
            or not database.hostname
            or not database.path.strip("/")
        ):
            raise ValueError("DATABASE_URL must be a PostgreSQL asyncpg URL in production")

        if not self.cors_origins:
            raise ValueError("CORS_ORIGINS must list explicit origins in production")
        for origin in self.cors_origins:
            try:
                parsed = urlsplit(origin)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname is not None
                    and parsed.port != 0
                    and parsed.path == ""
                    and parsed.query == ""
                    and parsed.fragment == ""
                    and parsed.username is None
                    and parsed.password is None
                    and "*" not in origin
                    and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("CORS_ORIGINS must list explicit public origins in production")


settings = Settings()
