"""Backend settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    pipeline_script: Path
    python_executable: str
    max_active_jobs: int
    max_queued_jobs: int
    cors_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    data_retention_days: int
    cancel_grace_seconds: float
    job_max_runtime_seconds: float
    monitor_interval_seconds: float
    # --- sessions & cleanup ---
    session_heartbeat_seconds: float = 60.0
    session_idle_timeout_seconds: float = 900.0
    session_close_grace_seconds: float = 30.0
    session_sweep_seconds: float = 30.0       # 0 disables the sweeper thread
    cleanup_token: str = ""                   # enables POST /api/internal/cleanup
    storage_backend: str = "local"            # "local" or "gcs"
    gcs_bucket: str = ""
    gcs_prefix: str = "jobs"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "./data/jobs")).resolve(),
            pipeline_script=Path(
                os.environ.get(
                    "PIPELINE_SCRIPT", str(BACKEND_DIR.parent / "pipeline.py")
                )
            ).resolve(),
            python_executable=os.environ.get("PYTHON_EXECUTABLE", sys.executable),
            max_active_jobs=max(1, _int("MAX_ACTIVE_JOBS", 1)),
            max_queued_jobs=max(1, _int("MAX_QUEUED_JOBS", 20)),
            cors_origins=_list("CORS_ORIGINS", "http://localhost:5173"),
            allowed_hosts=tuple(
                host.lower()
                for host in _list("ALLOWED_HOSTS", "readawrite.com,www.readawrite.com")
            ),
            data_retention_days=max(0, _int("DATA_RETENTION_DAYS", 0)),
            cancel_grace_seconds=_float("CANCEL_GRACE_SECONDS", 900.0),
            job_max_runtime_seconds=_float("JOB_MAX_RUNTIME_SECONDS", 6 * 3600.0),
            monitor_interval_seconds=_float("MONITOR_INTERVAL_SECONDS", 1.0),
            session_heartbeat_seconds=_float("SESSION_HEARTBEAT_SECONDS", 60.0),
            session_idle_timeout_seconds=_float("SESSION_IDLE_TIMEOUT_SECONDS", 900.0),
            session_close_grace_seconds=_float("SESSION_CLOSE_GRACE_SECONDS", 30.0),
            session_sweep_seconds=_float("SESSION_SWEEP_SECONDS", 30.0),
            cleanup_token=os.environ.get("CLEANUP_TOKEN", ""),
            storage_backend=os.environ.get("STORAGE_BACKEND", "local").lower(),
            gcs_bucket=os.environ.get("GCS_BUCKET", ""),
            gcs_prefix=os.environ.get("GCS_PREFIX", "jobs").strip("/"),
        )
