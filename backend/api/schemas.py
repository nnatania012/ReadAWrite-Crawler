"""Pydantic request/response models for the public API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

JobStatus = Literal[
    "queued", "discovering", "processing", "packaging", "completed",
    "completed_with_errors", "failed", "cancelling", "cancelled",
    "interrupted",
]
ChapterStatus = Literal["success", "failed", "partial", "processing",
                        "pending", "skipped"]


class CreateJobRequest(BaseModel):
    url: str = Field(..., max_length=2048)
    output_name: Optional[str] = Field(default=None, max_length=100)

    @field_validator("output_name")
    @classmethod
    def _blank_to_none(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() or None if value is not None else None


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus
    message: str


class Progress(BaseModel):
    total: int
    completed: int
    failed: int
    partial: int
    skipped: int
    processing: int
    pending: int
    processed: int
    percentage: float


class JobDetail(BaseModel):
    job_id: str
    output_name: str
    url: str
    status: JobStatus
    progress: Progress
    current_chapter: Optional[int] = None
    download_ready: bool
    download_url: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str


class JobSummary(BaseModel):
    job_id: str
    output_name: str
    url: str
    status: JobStatus
    created_at: str
    updated_at: str
    total_chapters: int
    completed_chapters: int
    failed_chapters: int
    partial_chapters: int
    download_ready: bool
    download_url: Optional[str] = None


class JobList(BaseModel):
    jobs: list[JobSummary]


class ChapterInfo(BaseModel):
    number: int
    status: ChapterStatus
    attempts: Optional[int] = None
    error: Optional[str] = None
    format: Optional[str] = None
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    copyable: bool = False


class ChapterList(BaseModel):
    chapters: list[ChapterInfo]


class WorkerInfo(BaseModel):
    running: bool
    active_jobs: int
    queued_jobs: int


class Health(BaseModel):
    status: str
    worker: WorkerInfo


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class HeartbeatResponse(BaseModel):
    session_id: str
    status: str
    heartbeat_seconds: float
    idle_timeout_seconds: float


class CloseResponse(BaseModel):
    status: str


class CleanupSummary(BaseModel):
    sessions: int
    deleted: int
    deferred: int
