"""HTTP routes. Thin: all logic lives in the service layer."""

from __future__ import annotations

import hmac
import json
import threading
from typing import Callable, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response

from api.schemas import (
    ChapterList, CleanupSummary, CloseResponse, CreateJobRequest, ErrorResponse,
    Health, HeartbeatResponse, JobAccepted, JobDetail, JobList,
)
from services.errors import InvalidRequestError, ServiceError
from services.job_service import JobService

router = APIRouter(prefix="/api")

ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
          410: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
          429: {"model": ErrorResponse}}


def _background(action: Callable[[], object]) -> None:
    threading.Thread(target=action, daemon=True).start()


def _raw_session_id(request: Request) -> Optional[str]:
    """Header for fetch calls; query string for plain download links."""
    return (request.headers.get("x-session-id")
            or request.query_params.get("session_id"))


def ctx(request: Request) -> tuple[JobService, str]:
    """The service plus the caller's verified, active session ID."""
    state = request.app.state
    return state.service, state.sessions.require_active(_raw_session_id(request))


# ----------------------------------------------------------------- health

@router.get("/health", response_model=Health)
def health(request: Request):
    return request.app.state.service.health()


# --------------------------------------------------------------- sessions

async def _body_session_id(request: Request) -> Optional[str]:
    """
    The session ID from a JSON body (sent with any content type: the closing
    beacon uses text/plain to avoid a CORS preflight), else header/query.
    """
    try:
        raw = await request.body()
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict) and data.get("session_id"):
            return str(data["session_id"])
    except ValueError:
        pass
    return _raw_session_id(request)


@router.post("/sessions/heartbeat", response_model=HeartbeatResponse,
             responses=ERRORS)
async def heartbeat(request: Request):
    state = request.app.state
    sid = await _body_session_id(request)
    result = await run_in_threadpool(state.sessions.heartbeat, sid)
    _background(state.cleanup.maybe_sweep)           # opportunistic cleanup
    return result


@router.post("/sessions/close", status_code=202, response_model=CloseResponse)
async def close_session(request: Request):
    state = request.app.state
    sid = await _body_session_id(request)
    await run_in_threadpool(state.sessions.close, sid)
    # With a zero grace period this removes the data right away; otherwise the
    # sweeper does it once the grace period has passed.
    _background(lambda: state.cleanup.maybe_sweep(min_interval=0))
    return {"status": "closing"}


@router.post("/internal/cleanup", response_model=CleanupSummary,
             include_in_schema=False)
async def internal_cleanup(request: Request):
    """Scheduler entry point (e.g. Cloud Scheduler) for scale-to-zero hosting."""
    state = request.app.state
    token = state.settings.cleanup_token
    supplied = request.headers.get("x-cleanup-token", "")

    if not token:
        raise _not_found()
    if not hmac.compare_digest(token.encode(), supplied.encode()):
        from services.errors import Forbidden
        raise Forbidden("Invalid cleanup token.")

    return await run_in_threadpool(state.cleanup.cleanup_expired_sessions)


def _not_found() -> ServiceError:
    from services.errors import JobNotFound
    error = JobNotFound("Not found.")
    error.code = "not_found"
    return error


# ------------------------------------------------------------------- jobs

@router.post("/jobs", status_code=202, response_model=JobAccepted,
             responses=ERRORS)
def create_job(body: CreateJobRequest, request: Request):
    service, sid = ctx(request)
    accepted = service.create_job(sid, body.url, body.output_name)
    _background(request.app.state.cleanup.maybe_sweep)
    return accepted


@router.get("/jobs", response_model=JobList, responses=ERRORS)
def list_jobs(request: Request):
    service, sid = ctx(request)
    return {"jobs": service.list_jobs(sid)}


@router.get("/jobs/{job_id}", response_model=JobDetail, responses=ERRORS)
def get_job(job_id: str, request: Request):
    service, sid = ctx(request)
    return service.get_job(sid, job_id)


@router.get("/jobs/{job_id}/chapters", response_model=ChapterList,
            responses=ERRORS)
def get_chapters(job_id: str, request: Request):
    service, sid = ctx(request)
    return {"chapters": service.get_chapters(sid, job_id)}


@router.get("/jobs/{job_id}/chapters/{chapter_number}/content",
            response_class=Response, responses=ERRORS)
def get_chapter_content(job_id: str, chapter_number: str, request: Request):
    """The chapter's text exactly as stored (UTF-8), as plain text, not JSON."""

    service, sid = ctx(request)

    if not chapter_number.isascii() or not chapter_number.isdigit():
        raise InvalidRequestError("The chapter number must be a positive integer.")

    data = service.get_chapter_content(sid, job_id, int(chapter_number))
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.api_route("/jobs/{job_id}/download", methods=["GET", "HEAD"],
                  responses=ERRORS)
def download(job_id: str, request: Request):
    service, sid = ctx(request)
    path, filename = service.get_download(sid, job_id)
    # FileResponse streams the file in chunks and sets Content-Disposition.
    return FileResponse(path, media_type="application/zip", filename=filename)


@router.post("/jobs/{job_id}/cancel", status_code=202, response_model=JobAccepted,
             responses=ERRORS)
def cancel_job(job_id: str, request: Request):
    service, sid = ctx(request)
    return service.cancel(sid, job_id)


@router.post("/jobs/{job_id}/retry", status_code=202, response_model=JobAccepted,
             responses=ERRORS)
def retry_job(job_id: str, request: Request):
    service, sid = ctx(request)
    return service.retry(sid, job_id)
