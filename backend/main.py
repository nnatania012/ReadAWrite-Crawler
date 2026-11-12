"""
ReadAWrite Extractor backend.

Run from the backend/ folder:

    uvicorn main:app --reload --port 8000

Settings come from environment variables (see .env.example).
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import router
from config import Settings
from services.cleanup_service import CleanupService
from services.errors import ServiceError
from services.job_service import JobService
from services.session_service import SessionService
from storage.file_manager import FileManager
from storage.remote import GCSRemoteStorage, NullRemoteStorage, RemoteStorage
from storage.session_store import SessionStore
from worker.job_manager import JobManager
from worker.pipeline_adapter import PipelineAdapter, SubprocessPipelineAdapter

log = logging.getLogger("api")


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    body["error"].update(extra)
    return JSONResponse(status_code=status, content=body)


def _remote_storage(settings: Settings) -> RemoteStorage:
    if settings.storage_backend == "gcs":
        return GCSRemoteStorage(settings.gcs_bucket, settings.gcs_prefix)
    return NullRemoteStorage()


class _Sweeper(threading.Thread):
    """Periodic expired-session cleanup (one of several triggers)."""

    def __init__(self, cleanup: CleanupService, interval: float) -> None:
        super().__init__(name="cleanup-sweeper", daemon=True)
        self.cleanup, self.interval = cleanup, interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.cleanup.cleanup_expired_sessions()
            except Exception:
                log.exception("Sweeper failed")


def create_app(settings: Optional[Settings] = None,
               adapter: Optional[PipelineAdapter] = None,
               remote: Optional[RemoteStorage] = None,
               clock: Callable[[], float] = time.time) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        fm = FileManager(settings.data_dir, remote or _remote_storage(settings))
        pipeline_adapter = adapter or SubprocessPipelineAdapter(
            settings.python_executable, settings.pipeline_script)
        manager = JobManager(fm, pipeline_adapter, settings)
        store = SessionStore(settings.data_dir, clock)
        sessions = SessionService(store, settings, clock)
        cleanup = CleanupService(fm, manager, store, sessions, settings, clock)
        manager.on_finished = cleanup.job_finished

        app.state.settings = settings
        app.state.fm = fm
        app.state.manager = manager
        app.state.sessions = sessions
        app.state.cleanup = cleanup
        app.state.service = JobService(fm, manager, settings)

        manager.start()
        fm.cleanup_old_jobs(settings.data_retention_days,
                            protected_ids=manager.in_flight_ids())
        cleanup.cleanup_expired_sessions()            # startup trigger

        sweeper = None
        if settings.session_sweep_seconds > 0:
            sweeper = _Sweeper(cleanup, settings.session_sweep_seconds)
            sweeper.start()

        log.info("Backend ready. Data directory: %s", settings.data_dir)
        try:
            yield
        finally:
            if sweeper:
                sweeper.stop_event.set()
            manager.stop()

    app = FastAPI(title="ReadAWrite Extractor API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Session-ID"],
        expose_headers=["Content-Disposition"],
    )

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return _error(exc.status_code, exc.code, exc.message, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        fields = ", ".join(
            ".".join(str(p) for p in e.get("loc", ())[1:]) or "request"
            for e in exc.errors())
        return _error(422, "invalid_request",
                      f"The request was not valid ({fields}).")

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.exception("Unhandled error", exc_info=exc)   # details stay in logs
        return _error(500, "internal_error", "Something went wrong.")

    app.include_router(router)
    return app


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
