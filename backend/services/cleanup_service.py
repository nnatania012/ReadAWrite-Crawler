"""
Automatic removal of expired sessions' data, without ever interrupting a
running extraction.

Rules enforced here:
* a job is only deleted when its session has ended (closed past the grace
  period, or idle past the timeout) or it was already marked cleanup_pending;
* a job that is queued is taken out of the queue first; a job that is running
  (or whose process might still be alive) is only *marked* cleanup_pending
  and deleted by the finish hook once its worker has really stopped;
* deletion is atomic with respect to retry/start (manager lock + "deleting"
  set) and idempotent, so concurrent or repeated calls are harmless.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from config import Settings
from services.session_service import SessionService
from storage.file_manager import FileManager, FileManagerError, now_iso
from storage.session_store import SessionStore
from worker.job_manager import JobManager
from worker.pipeline_adapter import process_alive

log = logging.getLogger("cleanup")

# How long an "ended" session marker is kept so a stale tab gets a clear
# "session ended" answer instead of silently starting a new empty session.
TOMBSTONE_SECONDS = 24 * 3600


class CleanupNotAuthorized(Exception):
    """cleanup_job() was asked to delete a job of a live session."""


class CleanupService:
    def __init__(self, fm: FileManager, manager: JobManager,
                 store: SessionStore, sessions: SessionService,
                 settings: Settings,
                 clock: Callable[[], float] = time.time) -> None:
        self.fm = fm
        self.manager = manager
        self.store = store
        self.sessions = sessions
        self.settings = settings
        self.clock = clock
        self._sweep_lock = threading.Lock()
        self._last_sweep = 0.0

    # ------------------------------------------------------------ one job

    def _authorized(self, meta: dict) -> bool:
        if meta.get("cleanup_pending"):
            return True

        sid = meta.get("session_id")
        if not sid:
            return False                      # legacy job: never auto-deleted

        record = self.store.read(sid)

        if record is None:                    # session record lost
            idle = self.settings.session_idle_timeout_seconds
            return self.clock() - self._job_time(meta) > idle

        return self.sessions.is_due_for_cleanup(record)

    def _job_time(self, meta: dict) -> float:
        from datetime import datetime
        try:
            return datetime.fromisoformat(meta["updated_at"]).timestamp()
        except (KeyError, ValueError):
            return 0.0

    def cleanup_job(self, job_id: str, authorized: bool = False) -> str:
        """
        Returns "deleted", "deferred" (still in use; marked cleanup_pending)
        or "missing". Raises CleanupNotAuthorized for a live session's job.
        """

        try:
            meta = self.fm.read_job(job_id)
        except FileManagerError:
            # No readable metadata. Remove a leftover folder, if any.
            return self._delete_if_idle(job_id)

        if not (authorized or self._authorized(meta)):
            raise CleanupNotAuthorized(job_id)

        return self._delete_if_idle(job_id, mark_pending=True)

    def _delete_if_idle(self, job_id: str, mark_pending: bool = False) -> str:
        with self.manager.lock:
            if job_id in self.manager.deleting:
                return "deferred"                       # someone else is on it

            self.manager.remove_queued(job_id)          # not started: just drop it

            busy = self.manager.is_active(job_id)

            if not busy:
                try:
                    pid = self.fm.read_job(job_id).get("pid")
                except FileManagerError:
                    pid = None
                busy = process_alive(pid)               # orphaned pipeline?

            if busy:
                if mark_pending:
                    self._mark_pending(job_id)
                return "deferred"

            self.manager.deleting.add(job_id)

        try:
            existed = self.fm.delete_job(job_id)
            log.info("Cleanup removed job %s", job_id)
            return "deleted" if existed else "missing"
        finally:
            with self.manager.lock:
                self.manager.deleting.discard(job_id)

    def _mark_pending(self, job_id: str) -> None:
        try:
            meta = self.fm.read_job(job_id)
            if not meta.get("cleanup_pending"):
                self.fm.update_job(job_id, cleanup_pending=True,
                                   cleanup_requested_at=now_iso())
        except FileManagerError:
            pass

    # ------------------------------------------------------- one session

    def cleanup_session(self, session_id: str) -> dict:
        """Remove (or defer) every job of a session that has ended."""

        result = {"deleted": 0, "deferred": 0}

        for meta in self.fm.list_jobs():
            if meta.get("session_id") != session_id:
                continue                                 # never another session's

            outcome = self.cleanup_job(meta["job_id"], authorized=True)
            if outcome == "deleted":
                result["deleted"] += 1
            elif outcome == "deferred":
                result["deferred"] += 1

        if self.store.read(session_id) is not None:
            self.store.update(session_id, status="expired",
                              expired_at=self.clock())

        return result

    # ---------------------------------------------------------- the sweep

    def cleanup_expired_sessions(self) -> dict:
        """
        Find and remove everything that has expired. Safe to call at any time
        and from any trigger (startup, job creation/completion, heartbeat,
        a timer, or an external scheduler).
        """

        summary = {"sessions": 0, "deleted": 0, "deferred": 0}

        with self._sweep_lock:
            for sid in self.store.list_ids():
                record = self.store.read(sid)
                if record and self.sessions.is_due_for_cleanup(record) and not (
                        record.get("status") == "expired"
                        and not any(m.get("session_id") == sid
                                    for m in self.fm.list_jobs())):
                    outcome = self.cleanup_session(sid)
                    summary["sessions"] += 1
                    summary["deleted"] += outcome["deleted"]
                    summary["deferred"] += outcome["deferred"]

            for meta in self.fm.list_jobs():
                job_id = meta.get("job_id")
                if not job_id:
                    continue
                try:
                    if meta.get("cleanup_pending") or (
                            meta.get("session_id") and self._authorized(meta)):
                        outcome = self.cleanup_job(job_id, authorized=True)
                        if outcome == "deleted":
                            summary["deleted"] += 1
                        elif outcome == "deferred":
                            summary["deferred"] += 1
                except Exception:
                    log.exception("Cleanup failed for job %s", job_id)

            jobs_left = {m.get("session_id") for m in self.fm.list_jobs()}
            for sid in self.store.list_ids():             # old "ended" markers
                record = self.store.read(sid)
                if (record and record.get("status") == "expired"
                        and sid not in jobs_left
                        and self.clock() - record.get("expired_at", 0) > TOMBSTONE_SECONDS):
                    self.store.delete(sid)

            self._last_sweep = time.monotonic()

        if any(summary.values()):
            log.info("Cleanup sweep: %s", summary)

        return summary

    def maybe_sweep(self, min_interval: float = 10.0) -> None:
        """Opportunistic sweep that won't run more often than min_interval."""

        if time.monotonic() - self._last_sweep < min_interval:
            return
        try:
            self.cleanup_expired_sessions()
        except Exception:
            log.exception("Opportunistic cleanup failed")

    # ---------------------------------------------------------- finish hook

    def job_finished(self, job_id: str) -> None:
        """Called by the JobManager when a worker has completely stopped."""

        try:
            meta = self.fm.read_job(job_id)
        except FileManagerError:
            return

        if meta.get("cleanup_pending") or self._authorized(meta):
            self.cleanup_job(job_id, authorized=True)

        self.maybe_sweep(min_interval=0)
