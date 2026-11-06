from typing import Any

from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(
        self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None
    ):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.details = details or {}


def conflict(code: str, message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, code, message, details)


def forbidden(message: str = "Доступ запрещён") -> ApiError:
    return ApiError(403, "FORBIDDEN", message)


def not_found(message: str = "Объект не найден") -> ApiError:
    return ApiError(404, "NOT_FOUND", message)
