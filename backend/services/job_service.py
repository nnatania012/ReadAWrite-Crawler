"""Application-level job operations used by the API routes."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from config import Settings
from services.errors import (
    ArchiveNotReady, ChapterNotFound, ChapterUnavailable,
    InternalFailure, InvalidRequestError, JobConflict, JobExpired, JobNotFound,
)
from services.url_validation import validate_readawrite_url
from storage.file_manager import (
    ArchiveNotReadyError, ChapterNotFoundError, ChapterUnavailableError,
    FileManager, FileManagerError, InvalidJobIdError, JobNotFoundError, PathEscapeError,
    now_iso, sanitize_error, sanitize_name,
)
from worker.job_manager import IN_FLIGHT_STATUSES, JobManager
from worker.pipeline_adapter import process_alive

log = logging.getLogger("service")

RETRYABLE_STATUSES = {"failed", "completed_with_errors", "interrupted",
                      "cancelled"}


def default_output_name(url: str, job_id: str) -> str:
    """A sensible folder/archive name when the user didn't supply one."""

    segments = [s for s in urlsplit(url).path.split("/") if s]
    slug = sanitize_name(segments[-1] if segments else "", default="")
    return slug or f"story-{job_id[:6]}"


class JobService:
    def __init__(self, fm: FileManager, manager: JobManager,
                 settings: Settings) -> None:
        self.fm = fm
        self.manager = manager
        self.settings = settings

    # ------------------------------------------------------------- helpers

    def _meta(self, session_id: str, job_id: str) -> dict:
        """A job's metadata, but only for the session that owns it."""

        try:
            meta = self.fm.read_job(job_id)
        except (JobNotFoundError, InvalidJobIdError, PathEscapeError):
            raise JobNotFound("Job not found.")
        except FileManagerError:
            raise InternalFailure("The job information could not be read.")

        if meta.get("session_id") != session_id:
            raise JobNotFound("Job not found.")        # don't reveal other sessions' jobs

        if meta.get("cleanup_pending"):
            raise JobExpired("This job's data has expired.")

        return meta

    @staticmethod
    def _download_url(meta: dict) -> Optional[str]:
        if meta.get("download_ready") and meta.get("archive_filename"):
            return f"/api/jobs/{meta['job_id']}/download"
        return None

    # ---------------------------------------------------------- operations

    def create_job(self, session_id: str, url: str,
                   output_name: Optional[str]) -> dict:
        clean_url = validate_readawrite_url(url, self.settings.allowed_hosts)

        if output_name is not None:
            output_name = output_name.strip()
            if len(output_name) > 100:
                raise InvalidRequestError(
                    "The output name must be 100 characters or fewer.")

        for existing in self.fm.list_jobs():
            if (existing.get("session_id") == session_id
                    and not existing.get("cleanup_pending")
                    and existing.get("source_url") == clean_url
                    and existing.get("status") in IN_FLIGHT_STATUSES):
                raise JobConflict(
                    "An extraction for this URL is already queued or running.",
                    job_id=existing.get("job_id"))

        if not self.manager.can_accept():
            from services.errors import QueueFull
            raise QueueFull("The queue is full. Please try again later.")

        job_id = self.fm.create_job_dir()
        name = (sanitize_name(output_name, default="") if output_name
                else "") or default_output_name(clean_url, job_id)
        stamp = now_iso()

        self.fm.write_job(job_id, {
            "job_id": job_id,
            "session_id": session_id,
            "output_name": name,
            "source_url": clean_url,
            "status": "queued",
            "created_at": stamp,
            "updated_at": stamp,
            "total_chapters": 0,
            "completed_chapters": 0,
            "failed_chapters": 0,
            "partial_chapters": 0,
            "current_chapter": None,
            "download_ready": False,
            "archive_filename": None,
            "error": None,
        })

        self.manager.submit(job_id)
        return {"job_id": job_id, "status": "queued",
                "message": "Extraction job created."}

    def list_jobs(self, session_id: str) -> list[dict]:
        jobs = []

        for meta in self.fm.list_jobs():
            if meta.get("session_id") != session_id or meta.get("cleanup_pending"):
                continue
            jobs.append({
                "job_id": meta["job_id"],
                "output_name": meta.get("output_name", ""),
                "url": meta.get("source_url", ""),
                "status": meta.get("status", "failed"),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "total_chapters": meta.get("total_chapters", 0),
                "completed_chapters": meta.get("completed_chapters", 0),
                "failed_chapters": meta.get("failed_chapters", 0),
                "partial_chapters": meta.get("partial_chapters", 0),
                "download_ready": bool(meta.get("download_ready")),
                "download_url": self._download_url(meta),
            })

        return jobs

    def get_job(self, session_id: str, job_id: str) -> dict:
        meta = self._meta(session_id, job_id)
        records = self.fm.chapter_records(job_id)
        counts = self.fm.summarize(records)

        if not records:                       # pipeline has not written yet
            counts = {**counts,
                      "total": meta.get("total_chapters", 0),
                      "completed": meta.get("completed_chapters", 0),
                      "failed": meta.get("failed_chapters", 0),
                      "partial": meta.get("partial_chapters", 0)}

        current = next((r.number for r in records
                        if r.status == "processing"), None)

        status = meta.get("status", "failed")
        if status == "discovering" and records:   # manifest exists: chapters known
            status = "processing"

        return {
            "job_id": meta["job_id"],
            "output_name": meta.get("output_name", ""),
            "url": meta.get("source_url", ""),
            "status": status,
            "progress": {k: counts[k] for k in (
                "total", "completed", "failed", "partial", "skipped",
                "processing", "pending", "processed", "percentage")},
            "current_chapter": current,
            "download_ready": bool(meta.get("download_ready")),
            "download_url": self._download_url(meta),
            "error": meta.get("error"),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        }

    def get_chapters(self, session_id: str, job_id: str) -> list[dict]:
        self._meta(session_id, job_id)

        return [{
            "number": r.number,
            "status": r.status,
            "attempts": r.attempts or None,
            "error": r.error,
            "format": r.format,
            "filename": r.filename,
            "size_bytes": r.size_bytes if r.status == "success" else None,
            # Copy is offered for every chapter that is extracted and intact,
            # even while the job is still running. (The content endpoint
            # re-checks everything, including the SHA-256.)
            "copyable": bool(r.status == "success" and r.verified),
        } for r in self.fm.chapter_records(job_id)]

    def get_chapter_content(self, session_id: str, job_id: str,
                            chapter_number: int) -> bytes:
        """Raw, verified chapter bytes (UTF-8 text) for the clipboard."""

        self._meta(session_id, job_id)

        try:
            return self.fm.read_chapter_bytes(job_id, chapter_number)
        except ChapterNotFoundError:
            raise ChapterNotFound("Chapter not found.")
        except ChapterUnavailableError as error:
            raise ChapterUnavailable(str(error))
        except JobNotFoundError:
            raise JobNotFound("Job not found.")
        except (FileManagerError, OSError):
            log.exception("Could not read chapter %s of job %s",
                          chapter_number, job_id)
            raise InternalFailure("The chapter could not be read.")

    def get_download(self, session_id: str, job_id: str) -> tuple[Path, str]:
        self._meta(session_id, job_id)

        try:
            return self.fm.get_archive_path(job_id)
        except ArchiveNotReadyError:
            raise ArchiveNotReady("The download is not ready yet.")
        except (PathEscapeError, FileManagerError):
            log.exception("Download refused for job %s", job_id)
            raise ArchiveNotReady("The download is not available.")

    def cancel(self, session_id: str, job_id: str) -> dict:
        meta = self._meta(session_id, job_id)

        if meta.get("status") not in IN_FLIGHT_STATUSES:
            raise JobConflict("Only queued or running jobs can be cancelled.")

        status = self.manager.cancel(job_id)
        return {"job_id": job_id, "status": status,
                "message": "Job cancelled." if status == "cancelled"
                else "Cancelling: the current chapter will finish first."}

    def retry(self, session_id: str, job_id: str) -> dict:
        meta = self._meta(session_id, job_id)

        if self.manager.is_in_flight(job_id) or \
                meta.get("status") not in RETRYABLE_STATUSES:
            raise JobConflict("This job can't be retried right now.")

        if process_alive(meta.get("pid")):
            raise JobConflict(
                "The previous worker is still shutting down. "
                "Please try again in a moment.")

        self.manager.submit(job_id)               # raises if full/duplicate
        self.fm.invalidate_archive(job_id)        # results may change
        return {"job_id": job_id, "status": "queued",
                "message": "Job queued to resume."}

    def health(self) -> dict:
        active, queued = self.manager.counts()
        return {"status": "ok",
                "worker": {"running": self.manager.running,
                           "active_jobs": active, "queued_jobs": queued}}
