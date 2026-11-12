import hashlib
import json
import time
from pathlib import Path

from storage.file_manager import FileManager


def wait_for(predicate, timeout=10.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not reached in time")


def make_job(fm: FileManager, statuses: dict, name="My Story", write_files=True,
             job_status="processing", session_id=None, texts=None) -> str:
    """Create a job directory with a pipeline manifest like the real pipeline writes."""
    job_id = fm.create_job_dir()
    out = fm.output_dir(job_id)
    chapters = {}
    for number, status in statuses.items():
        entry = {"url": f"https://www.readawrite.com/c/{number:032x}",
                 "status": status, "output": None, "attempts": 1}
        if status == "success":
            raw = (texts or {}).get(number)
            if raw is None:
                raw = f"text of chapter {number}\n".encode()
            if write_files:
                (out / f"{number}.txt").write_bytes(raw)
            entry.update(output=f"{number}.txt", format="Chat", entry_count=1,
                         output_bytes=len(raw),
                         output_sha256=hashlib.sha256(raw).hexdigest())
        elif status in ("failed", "partial"):
            entry.update(attempts=3, error="Timeout at /home/user/app/pipeline.py line 5")
        chapters[str(number)] = entry
    (out / "pipeline_state.json").write_text(json.dumps(
        {"version": 1, "chapter_list_hash": "h", "chapters": chapters}))
    fm.write_job(job_id, {"job_id": job_id, "session_id": session_id, "output_name": name,
                          "source_url": "https://www.readawrite.com/a/x",
                          "status": job_status, "created_at": "2026-01-01T00:00:00+00:00",
                          "updated_at": "2026-01-01T00:00:00+00:00",
                          "download_ready": False, "archive_filename": None})
    return job_id
