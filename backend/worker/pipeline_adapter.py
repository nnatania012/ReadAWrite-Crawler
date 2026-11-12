"""
Adapter between the job manager and the existing extraction pipeline.

The pipeline (pipeline.py) is a command-line program that installs its own
signal handlers and runs Playwright/Chromium, so it is launched as a separate
process (never shell=True). The adapter only builds the command line, starts
the process and offers graceful/forceful stop. Chapter progress is read from
the pipeline's pipeline_state.json by the file manager, not from console text.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional, Protocol

from storage.file_manager import sanitize_error

log = logging.getLogger("worker.adapter")


class RunningPipeline(Protocol):
    pid: Optional[int]

    def poll(self) -> Optional[int]: ...
    def request_stop(self) -> None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


class PipelineAdapter(Protocol):
    def start(self, job_id: str, url: str, output_dir: Path, stop_file: Path,
              log_path: Path) -> RunningPipeline: ...

    def describe_failure(self, log_path: Path, exit_code: Optional[int]) -> str: ...


def process_alive(pid: Optional[int]) -> bool:
    """True if a process with this PID exists (used after a restart)."""

    if not pid or pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32            # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259          # STILL_ACTIVE

    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class SubprocessRunning:
    def __init__(self, popen: subprocess.Popen, stop_file: Path,
                 log_handle) -> None:
        self._popen = popen
        self._stop_file = stop_file
        self._log_handle = log_handle
        self.pid = popen.pid

    def poll(self) -> Optional[int]:
        code = self._popen.poll()
        if code is not None and not self._log_handle.closed:
            self._log_handle.close()
        return code

    def request_stop(self) -> None:
        """Graceful: the pipeline finishes its current chapter, then exits."""
        self._stop_file.write_text("stop", encoding="utf-8")

    def _signal_tree(self, force: bool) -> None:
        if self._popen.poll() is not None:
            return

        if sys.platform == "win32":
            args = ["taskkill", "/PID", str(self.pid), "/T"]
            if force:
                args.append("/F")
            subprocess.run(args, capture_output=True, check=False)
            return

        try:
            os.killpg(os.getpgid(self.pid),
                      signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def terminate(self) -> None:
        self._signal_tree(force=False)

    def kill(self) -> None:
        self._signal_tree(force=True)


class SubprocessPipelineAdapter:
    def __init__(self, python_executable: str, pipeline_script: Path) -> None:
        self.python = python_executable
        self.script = Path(pipeline_script)

    def build_command(self, url: str, output_dir: Path, stop_file: Path
                      ) -> list[str]:
        # An argument list (no shell). The URL has already been validated and
        # can't be mistaken for an option because "--" ends option parsing.
        return [self.python, "-u", str(self.script),
                "-o", str(output_dir), "--stop-file", str(stop_file),
                "--", url]

    def start(self, job_id: str, url: str, output_dir: Path, stop_file: Path,
              log_path: Path) -> SubprocessRunning:
        if not self.script.is_file():
            raise FileNotFoundError("The extraction pipeline script is missing.")

        stop_file.unlink(missing_ok=True)
        command = self.build_command(url, output_dir, stop_file)

        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        log_handle = log_path.open("ab")
        kwargs: dict = {}

        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True      # own group: clean kill

        popen = subprocess.Popen(
            command, cwd=str(self.script.parent), env=env,
            stdin=subprocess.DEVNULL, stdout=log_handle,
            stderr=subprocess.STDOUT, shell=False, **kwargs)

        log.info("Job %s: started pipeline (pid %s)", job_id, popen.pid)
        return SubprocessRunning(popen, stop_file, log_handle)

    def describe_failure(self, log_path: Path, exit_code: Optional[int]) -> str:
        """Short, sanitised reason taken from the pipeline's last error line."""

        try:
            with log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 8192))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            tail = ""

        errors = re.findall(r"^(?:Error|Chapter-list retrieval failed)[:\s].*$",
                            tail, flags=re.M)

        if errors:
            message = sanitize_error(errors[-1])
            if message:
                return message

        return f"The extraction process ended unexpectedly (exit code {exit_code})."
