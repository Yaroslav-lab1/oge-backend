import contextvars
from typing import Any
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import AuditEvent

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")
_blocked = ("password", "token", "answer", "login", "name", "email", "secret", "body", "key")


def set_correlation_id(value: str) -> contextvars.Token[str]:
    return _correlation_id.set(value)


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    _correlation_id.reset(token)


def current_correlation_id() -> str:
    return _correlation_id.get() or str(uuid4())


def _safe_context(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if not any(blocked in str(key).lower() for blocked in _blocked)
        and isinstance(item, str | int | float | bool | type(None))
    }


def record_audit(
    db: AsyncSession,
    action: str,
    object_type: str,
    object_id: str,
    actor_id: str | None = None,
    context: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> None:
    """Append an event to the caller's transaction; no separate commit."""
    db.add(
        AuditEvent(
            actor_id=actor_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            context=_safe_context(context),
            correlation_id=correlation_id or current_correlation_id(),
        )
    )


@event.listens_for(AuditEvent, "before_update")
def _reject_audit_update(*_: Any) -> None:
    raise ValueError("Audit events are append only")


@event.listens_for(AuditEvent, "before_delete")
def _reject_audit_delete(*_: Any) -> None:
    raise ValueError("Audit events are append only")
