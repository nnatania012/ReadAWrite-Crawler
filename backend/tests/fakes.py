"""A fake pipeline: writes a real-looking pipeline_state.json and N.txt files."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional


class FakePipeline:
    def __init__(self, output_dir: Path, stop_file: Path, statuses: list[str],
                 delay: float, hold: Optional[threading.Event],
                 exit_code: Optional[int], log_path: Path, log_text: str,
                 texts: Optional[dict] = None):
        self.output_dir, self.stop_file = output_dir, stop_file
        self.statuses, self.delay, self.hold = statuses, delay, hold
        self.forced_exit, self.log_path, self.log_text = exit_code, log_path, log_text
        self.texts = texts or {}
        self.pid = 4_000_000 + id(self) % 1000
        self.exit_code: Optional[int] = None
        self.stopped_gracefully = False
        self.killed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- RunningPipeline protocol ------------------------------------
    def poll(self):
        return self.exit_code

    def request_stop(self):
        self.stop_file.write_text("stop")

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True

    # -- simulation ----------------------------------------------------
    def _manifest(self) -> dict:
        path = self.output_dir / "pipeline_state.json"
        if path.exists():
            return json.loads(path.read_text())
        return {"version": 1, "chapter_list_hash": "x",
                "total_chapters": len(self.statuses),
                "chapters": {str(i): {"url": f"https://www.readawrite.com/c/{i:032x}",
                                      "status": "pending", "output": None, "attempts": 0}
                             for i in range(1, len(self.statuses) + 1)}}

    def _save(self, data: dict):
        temp = self.output_dir / "pipeline_state.json.tmp"
        temp.write_text(json.dumps(data))
        os.replace(temp, self.output_dir / "pipeline_state.json")

    def _sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(0.005)

    def _run(self):
        try:
            if self.log_text:
                self.log_path.write_text(self.log_text)
            if self.forced_exit is not None and not self.statuses:
                self.exit_code = self.forced_exit
                return
            data = self._manifest()
            self._save(data)
            for index, wanted in enumerate(self.statuses, start=1):
                entry = data["chapters"][str(index)]
                if entry["status"] == "success":          # resume: skip done work
                    continue
                if self.stop_file.exists():
                    self.stopped_gracefully = True
                    self.exit_code = 130
                    return
                entry.update(status="in_progress", attempts=0)
                self._save(data)
                if self.hold is not None and index == 2:
                    self.hold.wait(10)           # chapter 2 stays "in progress"
                self._sleep(self.delay)
                if wanted == "success":
                    raw = self.texts.get(index)
                    if raw is None:
                        raw = (f"chapter {index} text\n" * 3).encode()
                    path = self.output_dir / f"{index}.txt"
                    path.write_bytes(raw)
                    entry.update(status="success", output=f"{index}.txt", attempts=1,
                                 format="Chat", entry_count=3,
                                 output_bytes=path.stat().st_size,
                                 output_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                else:
                    entry.update(status=wanted, output=None, attempts=3,
                                 error="Timeout: page.goto at /home/user/x/pipeline.py")
                self._save(data)
            ok = all(e["status"] == "success" for e in data["chapters"].values())
            self.exit_code = self.forced_exit if self.forced_exit is not None else (0 if ok else 2)
        except Exception:                                     # pragma: no cover
            self.exit_code = 99


class FakeAdapter:
    """Configure per-URL behaviour with .plan(url, statuses=[...])."""

    def __init__(self):
        self.plans: dict[str, dict] = {}
        self.default = {"statuses": ["success"] * 3, "delay": 0.0, "hold": None,
                        "exit_code": None, "log_text": "", "texts": None}
        self.fail_start = False
        self.started: list[FakePipeline] = []
        self._lock = threading.Lock()
        self.concurrent = 0
        self.max_concurrent = 0
        self.start_order: list[str] = []

    def plan(self, url: str, **kwargs):
        self.plans[url] = {**self.default, **kwargs}

    def start(self, job_id, url, output_dir, stop_file, log_path):
        if self.fail_start:
            raise FileNotFoundError("The extraction pipeline script is missing.")
        stop_file.unlink(missing_ok=True)
        plan = self.plans.get(url, self.default)
        proc = FakePipeline(output_dir, stop_file, plan["statuses"], plan["delay"],
                            plan["hold"], plan["exit_code"], log_path, plan["log_text"],
                            plan["texts"])
        with self._lock:
            self.started.append(proc)
            self.start_order.append(url)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        original_poll = proc.poll

        def poll():
            code = original_poll()
            if code is not None and not getattr(proc, "_counted", False):
                proc._counted = True
                with self._lock:
                    self.concurrent -= 1
            return code
        proc.poll = poll
        return proc

    def describe_failure(self, log_path, exit_code):
        try:
            tail = Path(log_path).read_text()
        except OSError:
            tail = ""
        lines = [l for l in tail.splitlines() if l.startswith("Error")]
        return lines[-1] if lines else f"The extraction process ended unexpectedly (exit code {exit_code})."
