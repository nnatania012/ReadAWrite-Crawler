import threading
import time

import pytest

from config import Settings
from storage.file_manager import FileManager
from tests.fakes import FakeAdapter, FakePipeline
from tests.helpers import make_job, wait_for
from worker.job_manager import JobManager


def settings_for(tmp_path, **overrides):
    values = dict(
        data_dir=tmp_path / "jobs", pipeline_script=tmp_path / "pipeline.py",
        python_executable="python", max_active_jobs=1, max_queued_jobs=5,
        cors_origins=("http://localhost:5173",),
        allowed_hosts=("www.readawrite.com",), data_retention_days=0,
        cancel_grace_seconds=30, job_max_runtime_seconds=600,
        monitor_interval_seconds=0.02)
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def env(tmp_path):
    settings = settings_for(tmp_path)
    fm = FileManager(settings.data_dir)
    adapter = FakeAdapter()
    manager = JobManager(fm, adapter, settings)
    yield fm, adapter, manager, settings
    manager.stop(wait_seconds=2)


def new_job(fm, status, url="https://www.readawrite.com/a/x", **kw):
    """chapters=None -> a brand-new job with no pipeline manifest yet."""
    chapters = kw.pop("chapters", {1: "success", 2: "pending"})
    if chapters is None:
        job_id = fm.create_job_dir()
        fm.write_job(job_id, {"job_id": job_id, "output_name": "Story",
                              "source_url": url, "status": status,
                              "created_at": "2026-01-01T00:00:00+00:00",
                              "updated_at": "2026-01-01T00:00:00+00:00",
                              "download_ready": False, "archive_filename": None})
    else:
        job_id = make_job(fm, chapters, job_status=status)
    fm.update_job(job_id, source_url=url, **kw)
    return job_id


# -------------------------------------------------------------- recovery

def test_recovery_marks_abandoned_active_jobs_interrupted_and_keeps_files(env):
    fm, adapter, manager, _ = env
    ids = {s: new_job(fm, s, pid=999_999_999)
           for s in ("discovering", "processing", "packaging")}
    cancelling = new_job(fm, "cancelling", pid=999_999_999)
    done = new_job(fm, "completed")
    manager.start()
    for status, job_id in ids.items():
        meta = fm.read_job(job_id)
        assert meta["status"] == "interrupted" and "Retry" in meta["error"], status
        assert (fm.output_dir(job_id) / "1.txt").is_file()          # progress preserved
        assert (fm.output_dir(job_id) / "pipeline_state.json").is_file()
    assert fm.read_job(cancelling)["status"] == "cancelled"
    assert fm.read_job(done)["status"] == "completed"
    assert adapter.started == []                                    # nothing was relaunched


def test_recovery_requeues_queued_jobs_and_runs_them(env):
    fm, adapter, manager, _ = env
    url = "https://www.readawrite.com/a/Q"
    queued = new_job(fm, "queued", url=url, chapters=None)
    adapter.plan(url, statuses=["success", "success"])
    manager.start()
    wait_for(lambda: fm.read_job(queued)["status"] == "completed")


def test_recovery_asks_a_surviving_orphan_to_stop(env):
    import os
    fm, _, manager, _ = env
    job_id = new_job(fm, "processing", pid=os.getpid())             # "alive" PID
    manager.start()
    assert fm.stop_file_path(job_id).is_file()
    assert fm.read_job(job_id)["status"] == "interrupted"


def test_recovery_removes_leftover_temp_files(env):
    fm, _, manager, _ = env
    job_id = new_job(fm, "completed")
    (fm.downloads_dir(job_id) / "x.zip.tmp").write_text("partial")
    manager.start()
    assert not list(fm.downloads_dir(job_id).glob("*.tmp"))


# ------------------------------------------------------- queue behaviour

def test_submit_twice_is_a_conflict(env):
    from services.errors import JobConflict
    fm, adapter, manager, _ = env
    hold = threading.Event()
    url = "https://www.readawrite.com/a/D"
    adapter.plan(url, hold=hold, statuses=["success"] * 3)
    job_id = new_job(fm, "failed", url=url, chapters=None)
    manager.start()
    manager.submit(job_id)
    with pytest.raises(JobConflict):
        manager.submit(job_id)
    hold.set()


def test_two_workers_never_process_the_same_job(tmp_path):
    settings = settings_for(tmp_path, max_active_jobs=3)
    fm = FileManager(settings.data_dir)
    adapter = FakeAdapter()
    manager = JobManager(fm, adapter, settings)
    manager.start()
    try:
        url = "https://www.readawrite.com/a/S"
        adapter.plan(url, statuses=["success"] * 3, delay=0.05)
        job_id = new_job(fm, "queued", url=url, chapters=None)
        manager._queued.add(job_id)
        for _ in range(3):
            manager._queue.put(job_id)           # duplicate queue entries
        wait_for(lambda: fm.read_job(job_id)["status"] == "completed")
        assert len(adapter.started) == 1
    finally:
        manager.stop(wait_seconds=2)


# ----------------------------------------------------- cancellation paths

class StubbornPipeline(FakePipeline):
    """Ignores the stop file (to test the forced-stop fallback)."""

    def request_stop(self):
        pass

    def terminate(self):
        self.terminated = True
        self.exit_code = 130


def test_forced_stop_only_after_grace_period(tmp_path):
    settings = settings_for(tmp_path, cancel_grace_seconds=0.3)
    fm = FileManager(settings.data_dir)
    adapter = FakeAdapter()
    manager = JobManager(fm, adapter, settings)
    manager.start()
    try:
        url = "https://www.readawrite.com/a/H"
        hold = threading.Event()
        adapter.plan(url, statuses=["success"] * 3, hold=hold)
        job_id = new_job(fm, "queued", url=url, chapters=None)
        original = adapter.start

        def start(*a, **k):
            proc = original(*a, **k)
            proc.__class__ = StubbornPipeline
            return proc
        adapter.start = start
        manager.submit(job_id)
        wait_for(lambda: fm.read_job(job_id)["status"] == "processing")
        started = time.monotonic()
        manager.cancel(job_id)
        assert fm.read_job(job_id)["status"] == "cancelling"
        wait_for(lambda: fm.read_job(job_id)["status"] == "cancelled", timeout=5)
        assert time.monotonic() - started >= 0.3                    # waited the grace period
        assert getattr(adapter.started[0], "terminated", False)
        hold.set()
    finally:
        manager.stop(wait_seconds=2)


def test_cancelled_only_after_worker_actually_stops(env):
    fm, adapter, manager, _ = env
    manager.start()
    url = "https://www.readawrite.com/a/C"
    hold = threading.Event()
    adapter.plan(url, statuses=["success"] * 4, hold=hold)
    job_id = new_job(fm, "queued", url=url, chapters=None)
    manager.submit(job_id)
    wait_for(lambda: fm.read_job(job_id)["status"] == "processing")
    assert manager.cancel(job_id) == "cancelling"
    time.sleep(0.2)
    assert fm.read_job(job_id)["status"] == "cancelling"            # still running chapter 2
    hold.set()
    wait_for(lambda: fm.read_job(job_id)["status"] == "cancelled")


def test_max_runtime_stops_job_and_marks_interrupted(tmp_path):
    settings = settings_for(tmp_path, job_max_runtime_seconds=0.2)
    fm = FileManager(settings.data_dir)
    adapter = FakeAdapter()
    manager = JobManager(fm, adapter, settings)
    manager.start()
    try:
        url = "https://www.readawrite.com/a/T"
        adapter.plan(url, statuses=["success"] * 50, delay=0.05)
        job_id = new_job(fm, "queued", url=url, chapters=None)
        manager.submit(job_id)
        wait_for(lambda: fm.read_job(job_id)["status"] == "interrupted", timeout=10)
        assert "maximum run time" in fm.read_job(job_id)["error"]
        assert fm.read_job(job_id)["completed_chapters"] >= 1       # progress kept
    finally:
        manager.stop(wait_seconds=2)


def test_server_shutdown_stops_running_job_gracefully(tmp_path):
    settings = settings_for(tmp_path)
    fm = FileManager(settings.data_dir)
    adapter = FakeAdapter()
    manager = JobManager(fm, adapter, settings)
    manager.start()
    url = "https://www.readawrite.com/a/Z"
    adapter.plan(url, statuses=["success"] * 40, delay=0.03)
    job_id = new_job(fm, "queued", url=url, chapters=None)
    manager.submit(job_id)
    wait_for(lambda: fm.read_job(job_id)["status"] == "processing")
    manager.stop(wait_seconds=5)
    meta = fm.read_job(job_id)
    assert meta["status"] == "interrupted" and adapter.started[0].stopped_gracefully
