import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import rate_limit
from app.db import Base
from app.errors import ApiError
from app.models.auth import RateLimitBucket


@pytest.mark.asyncio
async def test_quota_is_shared_persistent_scoped_and_expires(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 120)
    for _ in range(rate_limit.LIMITS["export"]):
        async with maker() as db:
            await rate_limit.enforce_rate_limit(db, "export", "account")
    async with maker() as db:
        with pytest.raises(ApiError) as error:
            await rate_limit.enforce_rate_limit(db, "export", "account")
        assert error.value.status_code == 429
        await rate_limit.enforce_rate_limit(db, "answer", "account")
        await rate_limit.enforce_rate_limit(db, "export", "another-account")
    monkeypatch.setattr(rate_limit.time, "time", lambda: 180)
    async with maker() as db:
        await rate_limit.enforce_rate_limit(db, "export", "account")
        assert await db.scalar(select(func.count()).select_from(RateLimitBucket)) == 3
    await engine.dispose()
