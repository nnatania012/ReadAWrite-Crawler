"""Errors raised by the service layer; the API turns them into JSON."""

from __future__ import annotations


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, **extra) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra


class InvalidRequestError(ServiceError):
    status_code = 422
    code = "invalid_request"


class JobNotFound(ServiceError):
    status_code = 404
    code = "job_not_found"


class JobConflict(ServiceError):
    status_code = 409
    code = "job_conflict"


class ArchiveNotReady(ServiceError):
    status_code = 409
    code = "archive_not_ready"


class QueueFull(ServiceError):
    status_code = 429
    code = "queue_full"


class InternalFailure(ServiceError):
    status_code = 500
    code = "internal_error"


class SessionRequired(ServiceError):
    status_code = 400
    code = "session_required"


class SessionExpired(ServiceError):
    status_code = 410
    code = "session_expired"


class JobExpired(ServiceError):
    status_code = 410
    code = "job_expired"


class ChapterNotFound(ServiceError):
    status_code = 404
    code = "chapter_not_found"


class ChapterUnavailable(ServiceError):
    status_code = 409
    code = "chapter_unavailable"


class Forbidden(ServiceError):
    status_code = 403
    code = "forbidden"
