"""Production configuration must reject development credentials and open CORS."""

import pytest

from app.config import Settings

_PRODUCTION = {
    "environment": "production",
    "database_url": "postgresql+asyncpg://oge:private-password@db/oge",
    "jwt_secret": "custom-jwt-secret-for-production-tests",
    "metrics_token": "custom-metrics-token-for-production-tests",
    "cors_origins": ("https://school.example",),
}


def test_production_accepts_explicit_configuration():
    settings = Settings(**_PRODUCTION)
    assert settings.environment == "production"


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"jwt_secret": "development-only-change-me"}, "JWT_SECRET"),
        ({"metrics_token": "development-metrics-token"}, "METRICS_TOKEN"),
        ({"database_url": "sqlite+aiosqlite:///./oge.db"}, "DATABASE_URL"),
        ({"cors_origins": ("*",)}, "CORS_ORIGINS"),
        ({"cors_origins": ("http://localhost:3000",)}, "CORS_ORIGINS"),
        ({"cors_origins": ("https://school.example/path",)}, "CORS_ORIGINS"),
        ({"cors_origins": ()}, "CORS_ORIGINS"),
    ],
)
def test_production_rejects_insecure_configuration(override, field):
    with pytest.raises(ValueError, match=field):
        Settings(**(_PRODUCTION | override))
