"""Atomic, shared SQL quotas. Commit the quota before starting the business transaction."""

import hashlib
import time

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models.auth import RateLimitBucket

LIMITS = {"refresh": 30, "audio": 10, "answer": 120, "export": 6}


async def enforce_rate_limit(db: AsyncSession, scope: str, identity: str) -> None:
    limit = LIMITS[scope]
    now = int(time.time())
    window = now // 60
    key_hash = hashlib.sha256(identity.encode()).hexdigest()
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    statement = insert(RateLimitBucket).values(
        scope=scope, key_hash=key_hash, window=window, count=1
    )
    returning = statement.on_conflict_do_update(
        index_elements=["scope", "key_hash", "window"],
        set_={"count": RateLimitBucket.count + 1},
    ).returning(RateLimitBucket.count)
    count = (await db.execute(returning)).scalar_one()
    await db.execute(
        delete(RateLimitBucket).where(
            RateLimitBucket.scope == scope,
            RateLimitBucket.key_hash == key_hash,
            RateLimitBucket.window < window,
        )
    )
    await db.commit()
    if count > limit:
        raise ApiError(
            429,
            "RATE_LIMITED",
            "Превышена частота запросов",
            {"retryAfterSeconds": 60 - now % 60, "scope": scope},
        )
