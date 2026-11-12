"""
JobManager: runs extraction jobs independently of HTTP requests.

* A queue plus a small pool of dedicated worker threads (default: one job at a
  time). Each worker supervises one pipeline *process*, so Playwright never
  runs inside the API's event loop.
* Progress comes from the pipeline manifest via the FileManager.
* Cancellation is graceful (stop file); a forceful fallback is used only after
  a grace period has expired.
* A job is only marked cancelled after its process has really stopped.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import Settings
from services.errors import JobConflict, JobNotFound, QueueFull
from storage.file_manager import (
    ArchiveNotReadyError, FileManager, FileManagerError, JobNotFoundError,
    NoSuccessfulChaptersError, TERMINAL_CHAPTER_STATUSES, now_iso,
    sanitize_error,
)
from worker.pipeline_adapter import PipelineAdapter, process_alive

log = logging.getLogger("worker.manager")

ACTIVE_STATUSES = {"discovering", "processing", "packaging", "cancelling"}
IN_FLIGHT_STATUSES = ACTIVE_STATUSES | {"queued"}


@dataclass
class _Run:
    job_id: str
    process: object = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)


class JobManager:
    def __init__(self, fm: FileManager, adapter: PipelineAdapter,
                 settings: Settings) -> None:
        self.fm = fm
        self.adapter = adapter
        self.settings = settings
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.RLock()
        self._queued: set[str] = set()
        self._active: dict[str, _Run] = {}
        self._threads: list[threading.Thread] = []
        self._shutdown = threading.Event()
        self.running = False
        # Jobs whose files are being deleted right now: they can't be started
        # or retried (guarded by self._lock together with _queued/_active).
        self.deleting: set[str] = set()
        # Called (outside locks) after a job's worker has fully finished.
        self.on_finished: Optional[Callable[[str], None]] = None

    @property
    def lock(self) -> threading.RLock:
        """The lock that makes "is it in flight?" + "start deleting" atomic."""
        return self._lock

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.recover()
        self.running = True

        for index in range(self.settings.max_active_jobs):
            thread = threading.Thread(target=self._worker_loop,
                                      name=f"job-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, wait_seconds: float = 10.0) -> None:
        """Server shutdown: ask running jobs to stop gracefully, then wait."""

        self._shutdown.set()

        with self._lock:
            runs = list(self._active.values())

        for run in runs:
            try:
                run.process.request_stop()                     # type: ignore
            except Exception:
                log.exception("Could not signal job %s", run.job_id)

        deadline = time.monotonic() + wait_seconds
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        self.running = False

    # ------------------------------------------------------------- recovery

    def recover(self) -> None:
        """
        After a backend restart: never assume an old worker is alive.
        Re-queue queued jobs; mark abandoned active jobs as interrupted
        (their manifests and files are preserved and they can be resumed).
        """

        self.fm.cleanup_temp_files()

        for meta in reversed(self.fm.list_jobs()):          # oldest first
            job_id, status = meta.get("job_id"), meta.get("status")

            if not job_id:
                continue

            if status == "queued":
                with self._lock:
                    self._queued.add(job_id)
                    self._queue.put(job_id)
                continue

            if status in ACTIVE_STATUSES:
                if process_alive(meta.get("pid")):
                    # An orphaned pipeline survived the restart: ask it to
                    # stop after its current chapter.
                    try:
                        self.fm.stop_file_path(job_id).write_text("stop")
                    except OSError:
                        pass

                if status == "cancelling":
                    self.fm.update_job(job_id, status="cancelled",
                                       error=None)
                else:
                    self.fm.update_job(
                        job_id, status="interrupted",
                        error="The server restarted while this job was "
                              "running. Retry to resume where it left off.")

    # ---------------------------------------------------------- public API

    def is_in_flight(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._queued or job_id in self._active

    def is_active(self, job_id: str) -> bool:
        """True while a worker is running (or finishing) this job."""
        with self._lock:
            return job_id in self._active

    def remove_queued(self, job_id: str) -> bool:
        """Take a not-yet-started job out of the queue."""
        with self._lock:
            if job_id in self._queued:
                self._queued.discard(job_id)     # the queue entry is skipped
                return True
            return False

    def counts(self) -> tuple[int, int]:
        with self._lock:
            return len(self._active), len(self._queued)

    def in_flight_ids(self) -> set[str]:
        with self._lock:
            return set(self._queued) | set(self._active)

    def can_accept(self) -> bool:
        with self._lock:
            return len(self._queued) < self.settings.max_queued_jobs

    def submit(self, job_id: str) -> None:
        """Queue a job. Raises JobConflict if it is already queued/running."""

        with self._lock:
            if job_id in self.deleting:
                raise JobConflict("This job is being removed.")

            if job_id in self._queued or job_id in self._active:
                raise JobConflict("This job is already queued or running.")

            if len(self._queued) >= self.settings.max_queued_jobs:
                raise QueueFull("The queue is full. Please try again later.")

            self.fm.update_job(job_id, status="queued", error=None,
                               started_at=None, finished_at=None)
            self._queued.add(job_id)
            self._queue.put(job_id)

    def cancel(self, job_id: str) -> str:
        """Returns the resulting status: 'cancelled' or 'cancelling'."""

        with self._lock:
            if job_id in self._queued:
                self._queued.discard(job_id)           # skipped at dequeue
                self.fm.update_job(job_id, status="cancelled",
                                   finished_at=now_iso())
                return "cancelled"

            run = self._active.get(job_id)

            if run is None:
                raise JobNotFound("This job is not queued or running.")

            if run.cancel_requested.is_set():
                return "cancelling"

            meta = self.fm.read_job(job_id)
            if meta.get("status") == "packaging":
                raise JobConflict(
                    "The job is finishing and can no longer be cancelled.")

            run.cancel_requested.set()
            self.fm.update_job(job_id, status="cancelling")
            return "cancelling"

    # --------------------------------------------------------------- worker

    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                job_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            with self._lock:
                if job_id not in self._queued:         # cancelled while queued
                    continue
                self._queued.discard(job_id)
                run = _Run(job_id)
                self._active[job_id] = run

            try:
                self._run_job(run)
            except Exception:
                log.exception("Unexpected error in job %s", job_id)
                self._safe_update(job_id, status="failed", pid=None,
                                  finished_at=now_iso(),
                                  error="An internal error stopped this job.")
            finally:
                with self._lock:
                    self._active.pop(job_id, None)

            self._notify_finished(job_id)

    def _notify_finished(self, job_id: str) -> None:
        """The worker is done with the job: let cleanup act on it."""

        hook = self.on_finished
        if hook is None:
            return
        try:
            hook(job_id)
        except Exception:
            log.exception("Finish hook failed for job %s", job_id)

    def _safe_update(self, job_id: str, **fields) -> None:
        try:
            self.fm.update_job(job_id, **fields)
        except FileManagerError:
            log.exception("Could not update job %s", job_id)

    def _run_job(self, run: _Run) -> None:
        job_id = run.job_id
        meta = self.fm.read_job(job_id)

        if meta.get("status") != "queued":
            return

        self.fm.update_job(job_id, status="discovering", started_at=now_iso(),
                           error=None)

        try:
            process = self.adapter.start(
                job_id, meta["source_url"], self.fm.output_dir(job_id),
                self.fm.stop_file_path(job_id), self.fm.worker_log_path(job_id))
        except Exception as error:
            log.exception("Job %s: could not start the pipeline", job_id)
            self.fm.update_job(
                job_id, status="failed", finished_at=now_iso(),
                error=sanitize_error(str(error)) or "Could not start the extraction.")
            return

        run.process = process
        self.fm.update_job(job_id, pid=getattr(process, "pid", None))

        interval = self.settings.monitor_interval_seconds
        stop_sent_at: Optional[float] = None
        timed_out = False
        started = time.monotonic()
        terminated = False

        while process.poll() is None:
            time.sleep(interval)
            self._sync_progress(job_id)
            now = time.monotonic()

            wants_stop = (run.cancel_requested.is_set() or timed_out
                          or self._shutdown.is_set())

            if (not wants_stop and
                    now - started > self.settings.job_max_runtime_seconds):
                timed_out = True
                wants_stop = True
                log.warning("Job %s hit the maximum runtime", job_id)

            if wants_stop and stop_sent_at is None:
                process.request_stop()
                stop_sent_at = now

            # Forceful fallback only after the grace period has expired.
            if (stop_sent_at is not None and not terminated and
                    now - stop_sent_at > self.settings.cancel_grace_seconds):
                log.warning("Job %s ignored the stop request; terminating",
                            job_id)
                process.terminate()
                terminated = True
                stop_sent_at = now

            if terminated and now - stop_sent_at > 10:
                process.kill()

        exit_code = process.poll()
        self._finalize(run, exit_code, timed_out)

    # ------------------------------------------------------------- progress

    def _sync_progress(self, job_id: str) -> None:
        records = self.fm.chapter_records(job_id)

        if not records:
            return

        counts = self.fm.summarize(records)
        current = next((r.number for r in records
                        if r.status == "processing"), None)
        meta = self.fm.read_job(job_id)
        changes = {
            "total_chapters": counts["total"],
            "completed_chapters": counts["completed"],
            "failed_chapters": counts["failed"],
            "partial_chapters": counts["partial"],
            "current_chapter": current,
        }

        if meta.get("status") == "discovering":
            changes["status"] = "processing"

        if any(meta.get(k) != v for k, v in changes.items()):
            self.fm.update_job(job_id, **changes)

    # ------------------------------------------------------------- finishing

    def _finalize(self, run: _Run, exit_code: Optional[int],
                  timed_out: bool) -> None:
        job_id = run.job_id
        self._sync_progress(job_id)
        records = self.fm.chapter_records(job_id)
        counts = self.fm.summarize(records)
        unfinished = counts["total"] - counts["processed"]
        base = {"pid": None, "finished_at": now_iso(), "current_chapter": None}
        log.info("Job %s: pipeline exited with code %s", job_id, exit_code)

        try:
            cleanup_pending = bool(self.fm.read_job(job_id).get("cleanup_pending"))
        except FileManagerError:
            return                                   # already removed

        if cleanup_pending:
            # The session is gone: don't build a ZIP nobody will download.
            # The finish hook deletes the job right after this returns.
            self.fm.update_job(job_id, **base)
            return

        finished_everything = (exit_code in (0, 2) and bool(records)
                               and unfinished == 0)

        if run.cancel_requested.is_set() and not finished_everything:
            self.fm.update_job(job_id, status="cancelled", error=None, **base)
            return

        if timed_out:
            self.fm.update_job(
                job_id, status="interrupted", **base,
                error="The maximum run time was reached. Retry to resume.")
            return

        if self._shutdown.is_set() or exit_code == 130:
            self.fm.update_job(
                job_id, status="interrupted", **base,
                error="Processing was stopped. Retry to resume.")
            return

        if exit_code in (0, 2) and records and unfinished == 0:
            self._package(job_id, counts, base)
            return

        if exit_code in (0, 2) and records and unfinished > 0:
            reason = sanitize_error(self.adapter.describe_failure(
                self.fm.worker_log_path(job_id), exit_code)) or ""
            self.fm.update_job(
                job_id, status="interrupted", **base,
                error=f"Processing stopped early with {unfinished} chapter(s) "
                      f"unfinished. {reason}")
            return

        reason = sanitize_error(self.adapter.describe_failure(
            self.fm.worker_log_path(job_id), exit_code))
        self.fm.update_job(job_id, status="failed", error=reason, **base)

    def _package(self, job_id: str, counts: dict, base: dict) -> None:
        """All chapters are terminal: build the ZIP and set the final status."""

        self.fm.update_job(job_id, status="packaging")

        try:
            info = self.fm.create_archive(job_id)
        except NoSuccessfulChaptersError:
            self.fm.update_job(
                job_id, status="failed", **base,
                error="No chapters could be extracted from this story.")
            return
        except (ArchiveNotReadyError, FileManagerError, OSError):
            log.exception("Job %s: archive creation failed", job_id)
            self.fm.update_job(
                job_id, status="failed", **base,
                error="The chapters were extracted but the ZIP archive "
                      "could not be created. Retry to try again.")
            return

        clean = (info.failed == 0 and info.partial == 0 and info.skipped == 0
                 and info.successful == info.total)
        self.fm.update_job(
            job_id, status="completed" if clean else "completed_with_errors",
            download_ready=True, error=None, **base)
