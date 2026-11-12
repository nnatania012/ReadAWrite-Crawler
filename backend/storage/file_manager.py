"""
File management for the ReadAWrite Extractor.

Everything that touches the filesystem for a job lives here: job directories,
job.json metadata, discovering chapter files, building the extraction report,
creating/verifying ZIP archives, and safe retrieval for downloads.

This module never crawls or extracts anything. The pipeline's own
pipeline_state.json manifest stays the authority for chapter status; job.json
only holds application-level data about the job.

Layout (one physical directory per job, named by the generated job ID):

    <DATA_DIR>/<job_id>/
        job.json              application job metadata
        output/               passed to the pipeline (-o); holds N.txt,
                              pipeline_state.json, logs/, input.html, ...
        downloads/            <safe name>.zip and extraction_report.json
        worker.log            raw pipeline console output (internal)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from storage.remote import NullRemoteStorage, RemoteStorage

log = logging.getLogger("storage")

MAX_CHAPTER_BYTES = 50 * 1024 * 1024      # sanity bound for one chapter file
UTF8_BOM = b"\xef\xbb\xbf"
COPYABLE_JOB_STATUSES = {"completed", "completed_with_errors"}

JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")
CHAPTER_FILE_RE = re.compile(r"^(\d+)\.txt$")
STATE_FILE_NAME = "pipeline_state.json"
JOB_FILE_NAME = "job.json"

TERMINAL_CHAPTER_STATUSES = {"success", "failed", "partial", "skipped"}
FINISHED_JOB_STATUSES = {
    "completed", "completed_with_errors", "failed", "cancelled", "interrupted",
}
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


# ----------------------------------------------------------------- errors

class FileManagerError(Exception):
    """Base class for file-management problems."""


class InvalidJobIdError(FileManagerError):
    pass


class JobNotFoundError(FileManagerError):
    pass


class PathEscapeError(FileManagerError):
    pass


class ArchiveNotReadyError(FileManagerError):
    pass


class NoSuccessfulChaptersError(FileManagerError):
    pass


class ChapterNotFoundError(FileManagerError):
    """The chapter number isn't part of this job."""


class ExtractionIncompleteError(FileManagerError):
    """The job hasn't finished (or didn't finish normally)."""


class ChapterUnavailableError(FileManagerError):
    """The chapter isn't a verified, successfully extracted chapter."""


# ------------------------------------------------------------ small helpers

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sanitize_name(name: Optional[str], default: str = "story",
                  max_len: int = 80) -> str:
    """
    Turn user text into a safe file/folder name: no separators, control
    characters, reserved characters or Windows device names; bounded length.
    """

    text = unicodedata.normalize("NFKC", name or "")
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    text = re.sub(r"_{2,}", "_", text)
    text = text[:max_len].strip(" ._")

    if not text or text.lower() in WINDOWS_RESERVED or set(text) <= {"_"}:
        return default

    return text


_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s'\"<>|]+")
_POSIX_PATH = re.compile(
    r"(?<![\w:/.])/(?:home|Users|tmp|var|usr|opt|mnt|root|etc|srv|data|app|"
    r"private|proc|sys)(?:/[^\s'\"<>|]*)*"
)


def sanitize_error(text: Optional[str], max_len: int = 300) -> Optional[str]:
    """Remove filesystem paths, stack-trace noise and excess length."""

    if not text:
        return None

    cleaned = str(text)
    cleaned = _WIN_PATH.sub("<path>", cleaned)
    cleaned = _POSIX_PATH.sub("<path>", cleaned)
    cleaned = re.sub(r"Traceback \(most recent call last\):.*", "", cleaned,
                     flags=re.S)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"

    return cleaned or None


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _fsync_replace(temp: Path, final: Path) -> None:
    for attempt in range(6):
        try:
            os.replace(temp, final)
            return
        except PermissionError:       # Windows can briefly lock files
            if attempt == 5:
                raise
            time.sleep(0.1 * (attempt + 1))


def _atomic_write_text(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")

    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())

    _fsync_replace(temp, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return digest.hexdigest()


# ------------------------------------------------------------- data classes

@dataclass
class ChapterRecord:
    number: int
    status: str                     # success|failed|partial|processing|pending
    attempts: int = 0
    error: Optional[str] = None
    format: Optional[str] = None
    entry_count: Optional[int] = None
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    verified: bool = False
    sha256: Optional[str] = None
    _path: Optional[Path] = field(default=None, repr=False)


@dataclass
class Manifest:
    data: dict
    output_dir: Path


@dataclass
class ArchiveInfo:
    filename: str
    size_bytes: int
    total: int
    successful: int
    failed: int
    partial: int
    skipped: int
    fingerprint: str
    reused: bool = False


# ------------------------------------------------------------- the manager

class FileManager:
    def __init__(self, data_dir: Path,
                 remote: Optional[RemoteStorage] = None) -> None:
        self.remote: RemoteStorage = remote or NullRemoteStorage()
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # ---- identity & paths ------------------------------------------------

    def _lock(self, job_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(job_id, threading.RLock())

    def validate_job_id(self, job_id: str) -> str:
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise InvalidJobIdError("Invalid job ID.")
        return job_id

    def job_dir(self, job_id: str) -> Path:
        self.validate_job_id(job_id)
        path = (self.root / job_id).resolve()

        if not _inside(self.root, path) or path.parent != self.root:
            raise PathEscapeError("Job path escapes the data directory.")

        return path

    def output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output"

    def downloads_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "downloads"

    def job_exists(self, job_id: str) -> bool:
        try:
            return (self.job_dir(job_id) / JOB_FILE_NAME).is_file()
        except FileManagerError:
            return False

    def worker_log_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "worker.log"

    def stop_file_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "STOP"

    # ---- job directories & metadata --------------------------------------

    def new_job_id(self) -> str:
        while True:
            job_id = secrets.token_hex(6)
            if not (self.root / job_id).exists():
                return job_id

    def create_job_dir(self, job_id: Optional[str] = None) -> str:
        """Create a brand-new, unique job directory (never reuses one)."""

        job_id = job_id or self.new_job_id()
        path = self.job_dir(job_id)
        path.mkdir(parents=True, exist_ok=False)      # raises if it exists
        (path / "output").mkdir()
        (path / "downloads").mkdir()
        return job_id

    def write_job(self, job_id: str, meta: dict) -> None:
        with self._lock(job_id):
            path = self.job_dir(job_id) / JOB_FILE_NAME
            _atomic_write_text(
                path, json.dumps(meta, indent=2, ensure_ascii=False)
            )

    def read_job(self, job_id: str) -> dict:
        path = self.job_dir(job_id) / JOB_FILE_NAME

        for attempt in range(3):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise JobNotFoundError("Job not found.")
            except ValueError:
                time.sleep(0.05 * (attempt + 1))

        raise FileManagerError("Job metadata is unreadable.")

    def update_job(self, job_id: str, **fields: Any) -> dict:
        """Read-modify-write job.json atomically under the job's lock."""

        with self._lock(job_id):
            meta = self.read_job(job_id)
            meta.update(fields)
            meta["updated_at"] = now_iso()
            self.write_job(job_id, meta)
            return meta

    def list_jobs(self) -> list[dict]:
        """All jobs, newest first. Unreadable jobs are skipped and logged."""

        jobs = []

        for entry in self.root.iterdir():
            if not entry.is_dir() or not JOB_ID_RE.match(entry.name):
                continue
            try:
                jobs.append(self.read_job(entry.name))
            except FileManagerError:
                log.warning("Skipping unreadable job %s", entry.name)

        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return jobs

    # ---- pipeline manifest & chapters ------------------------------------

    def find_manifest(self, job_id: str) -> Optional[Manifest]:
        """
        Locate and read the pipeline's pipeline_state.json for this job.

        Normally it is output/pipeline_state.json. If the story's chapter list
        changed between runs the pipeline writes to a sibling "output_<hash>"
        folder instead; the most recently updated one wins.
        """

        job_path = self.job_dir(job_id)
        candidates = []

        for folder in job_path.glob("output*"):
            state = folder / STATE_FILE_NAME
            if folder.is_dir() and state.is_file():
                try:
                    candidates.append((state.stat().st_mtime, state, folder))
                except OSError:
                    continue

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        _, state, folder = candidates[0]

        for attempt in range(3):
            try:
                data = json.loads(state.read_text(encoding="utf-8"))
                if isinstance(data.get("chapters"), dict):
                    return Manifest(data=data, output_dir=folder)
                return None
            except (ValueError, OSError):
                time.sleep(0.05 * (attempt + 1))

        return None

    def chapter_records(self, job_id: str, deep: bool = False
                        ) -> list[ChapterRecord]:
        """
        Chapter-level information in original order, derived from the
        pipeline manifest. A "success" is only reported as success when its
        TXT file exists with the recorded size (and, if deep, checksum).
        """

        manifest = self.find_manifest(job_id)

        if manifest is None:
            return []

        records = []

        for key, entry in manifest.data["chapters"].items():
            try:
                number = int(key)
            except ValueError:
                continue

            raw = entry.get("status")
            record = ChapterRecord(
                number=number,
                status="pending",
                attempts=int(entry.get("attempts") or 0),
                error=sanitize_error(entry.get("error")),
                format=entry.get("format"),
                entry_count=entry.get("entry_count"),
            )

            if raw == "success":
                self._verify_success(record, entry, manifest.output_dir, deep)
            elif raw in ("failed", "partial"):
                record.status = raw
            elif raw == "in_progress":
                record.status = "processing"
            else:
                record.status = "pending"

            records.append(record)

        records.sort(key=lambda r: r.number)
        return records

    def _verify_success(self, record: ChapterRecord, entry: dict,
                        output_dir: Path, deep: bool) -> None:
        name = entry.get("output") or f"{record.number}.txt"
        path = (output_dir / name).resolve()

        problem = None

        if name != f"{record.number}.txt" or not _inside(output_dir, path):
            problem = "Unexpected output file name."
        elif not path.is_file():
            problem = "Output file is missing."
        else:
            size = path.stat().st_size
            expected = entry.get("output_bytes")

            if size <= 0:
                problem = "Output file is empty."
            elif expected is not None and size != expected:
                problem = "Output file does not match the recorded size."
            elif deep and entry.get("output_sha256"):
                if _sha256_file(path) != entry["output_sha256"]:
                    problem = "Output file failed the checksum check."

            record.size_bytes = size
            record.sha256 = entry.get("output_sha256")

        if problem:
            record.status = "failed"
            record.error = problem
            record.verified = False
        else:
            record.status = "success"
            record.filename = name
            record.verified = True
            record._path = path

    @staticmethod
    def summarize(records: Iterable[ChapterRecord]) -> dict:
        counts = {"total": 0, "completed": 0, "failed": 0, "partial": 0,
                  "skipped": 0, "processing": 0, "pending": 0}

        for record in records:
            counts["total"] += 1
            key = {"success": "completed", "skipped": "skipped"}.get(
                record.status, record.status)
            counts[key] += 1

        counts["processed"] = (counts["completed"] + counts["failed"]
                               + counts["partial"] + counts["skipped"])
        counts["percentage"] = (
            round(counts["processed"] / counts["total"] * 100, 2)
            if counts["total"] else 0.0
        )
        return counts

    def list_chapter_files(self, job_id: str) -> list[dict]:
        """TXT files on disk (numeric order) with size and chapter status."""

        manifest = self.find_manifest(job_id)
        folder = manifest.output_dir if manifest else self.output_dir(job_id)
        status_by_number = {r.number: r.status
                            for r in self.chapter_records(job_id)}
        files = []

        if folder.is_dir():
            for item in folder.iterdir():
                match = CHAPTER_FILE_RE.match(item.name)
                if match and item.is_file():
                    number = int(match.group(1))
                    files.append({
                        "number": number,
                        "filename": item.name,
                        "size_bytes": item.stat().st_size,
                        "status": status_by_number.get(number, "unknown"),
                    })

        files.sort(key=lambda f: f["number"])
        return files

    # ---- report & archive --------------------------------------------------

    def build_report(self, job_meta: dict, records: list[ChapterRecord],
                     status: Optional[str] = None) -> dict:
        counts = self.summarize(records)

        return {
            "job_id": job_meta.get("job_id"),
            "output_name": job_meta.get("output_name"),
            "source_url": job_meta.get("source_url"),
            "status": status or job_meta.get("status"),
            "generated_at": now_iso(),
            "total_chapters": counts["total"],
            "successful": counts["completed"],
            "failed": counts["failed"],
            "partial": counts["partial"],
            "skipped": counts["skipped"],
            "chapters": [
                {k: v for k, v in {
                    "number": r.number,
                    "status": r.status if r.status != "processing" else "pending",
                    "attempts": r.attempts or None,
                    "error": r.error,
                    "format": r.format,
                }.items() if v is not None}
                for r in records
            ],
        }

    @staticmethod
    def _fingerprint(records: list[ChapterRecord], top: str) -> str:
        material = json.dumps(
            [top] + [[r.number, r.status, r.sha256] for r in records]
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def invalidate_archive(self, job_id: str) -> None:
        """Remove any existing archive (called when a retry starts)."""

        with self._lock(job_id):
            folder = self.downloads_dir(job_id)

            if folder.is_dir():
                for item in folder.iterdir():
                    if item.suffix in (".zip", ".tmp") or \
                            item.name == "extraction_report.json":
                        try:
                            item.unlink()
                        except OSError:
                            log.warning("Could not remove %s", item.name)

            if self.job_exists(job_id):
                self.update_job(job_id, download_ready=False,
                                archive_filename=None, archive_fingerprint=None)

    def create_archive(self, job_id: str, output_name: Optional[str] = None
                       ) -> ArchiveInfo:
        """
        Build <safe name>.zip once every chapter has reached a terminal state.

        Raises ArchiveNotReadyError while chapters are pending/processing,
        and NoSuccessfulChaptersError when nothing was extracted (no empty
        "success" archive is ever produced).
        """

        with self._lock(job_id):
            meta = self.read_job(job_id)
            records = self.chapter_records(job_id, deep=True)

            if not records:
                raise ArchiveNotReadyError("No chapters were discovered.")

            if any(r.status not in TERMINAL_CHAPTER_STATUSES for r in records):
                raise ArchiveNotReadyError(
                    "Some chapters have not finished processing.")

            good = [r for r in records if r.status == "success"]

            if not good:
                raise NoSuccessfulChaptersError(
                    "No chapters were extracted successfully.")

            top = sanitize_name(output_name or meta.get("output_name"))
            fingerprint = self._fingerprint(records, top)
            counts = self.summarize(records)
            archive_name = f"{top}.zip"
            downloads = self.downloads_dir(job_id)
            downloads.mkdir(parents=True, exist_ok=True)
            final = downloads / archive_name

            info = ArchiveInfo(
                filename=archive_name, size_bytes=0, total=counts["total"],
                successful=counts["completed"], failed=counts["failed"],
                partial=counts["partial"], skipped=counts["skipped"],
                fingerprint=fingerprint,
            )

            if (meta.get("archive_fingerprint") == fingerprint
                    and meta.get("archive_filename") == archive_name
                    and final.is_file() and self._zip_is_valid(final)):
                info.size_bytes = final.stat().st_size
                info.reused = True            # avoid needless regeneration
                return info

            has_problems = any(r.status != "success" for r in records)
            report = self.build_report(meta, records)
            report_text = json.dumps(report, indent=2, ensure_ascii=False)
            temp = downloads / (archive_name + ".tmp")

            try:
                with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as zf:
                    for record in good:                 # numeric order
                        assert record._path is not None
                        zf.write(record._path, f"{top}/{record.filename}")
                    if has_problems:
                        zf.writestr(f"{top}/extraction_report.json",
                                    report_text)

                expected = {f"{top}/{r.filename}" for r in good}
                if has_problems:
                    expected.add(f"{top}/extraction_report.json")

                with zipfile.ZipFile(temp) as zf:
                    if zf.testzip() is not None or set(zf.namelist()) != expected:
                        raise FileManagerError("Archive verification failed.")

                _fsync_replace(temp, final)
            finally:
                if temp.exists():
                    temp.unlink(missing_ok=True)

            for other in downloads.glob("*.zip"):        # stale names
                if other.name != archive_name:
                    other.unlink(missing_ok=True)

            _atomic_write_text(downloads / "extraction_report.json",
                               report_text)

            info.size_bytes = final.stat().st_size
            self.update_job(job_id, archive_filename=archive_name,
                            archive_fingerprint=fingerprint,
                            download_ready=True)
            return info

    @staticmethod
    def _zip_is_valid(path: Path) -> bool:
        try:
            with zipfile.ZipFile(path) as zf:
                return zf.testzip() is None
        except (zipfile.BadZipFile, OSError):
            return False

    def get_archive_path(self, job_id: str) -> tuple[Path, str]:
        """(path, download filename) for a ready, valid archive of THIS job."""

        meta = self.read_job(job_id)
        name = meta.get("archive_filename")

        if not meta.get("download_ready") or not name:
            raise ArchiveNotReadyError("The archive is not ready.")

        if name != sanitize_name(Path(name).stem) + ".zip":
            raise PathEscapeError("Unsafe archive name.")

        path = (self.downloads_dir(job_id) / name).resolve()

        if not _inside(self.downloads_dir(job_id), path):
            raise PathEscapeError("Archive path escapes the job directory.")

        if not path.is_file() or not self._zip_is_valid(path):
            raise ArchiveNotReadyError("The archive is not available.")

        return path, name

    # ---- chapter content (copy to clipboard) -----------------------------

    def read_chapter_bytes(self, job_id: str, chapter_number: int) -> bytes:
        """
        The verified raw bytes of one chapter's TXT file, for the content API.

        Every check runs against the very bytes that are returned (read once),
        so the file can't change between verification and delivery:
        job finished normally, chapter successful in the pipeline manifest,
        file inside the job's output folder, recorded size and SHA-256 match,
        and the bytes are valid UTF-8.

        The text is not altered. Like PowerShell's
        `Get-Content -Encoding UTF8 -Raw`, a leading UTF-8 byte-order mark
        (which the pipeline never writes) is not part of the content.
        """

        meta = self.read_job(job_id)

        if meta.get("status") not in COPYABLE_JOB_STATUSES:
            raise ExtractionIncompleteError(
                "The extraction has not finished successfully yet.")

        if (not isinstance(chapter_number, int) or isinstance(chapter_number, bool)
                or chapter_number < 1):
            raise ChapterNotFoundError("Chapter not found.")

        manifest = self.find_manifest(job_id)
        entry = (manifest.data["chapters"].get(str(chapter_number))
                 if manifest else None)

        if entry is None:
            raise ChapterNotFoundError("Chapter not found.")

        if entry.get("status") != "success":
            raise ChapterUnavailableError("This chapter was not extracted.")

        name = f"{chapter_number}.txt"
        path = (manifest.output_dir / name).resolve()

        if entry.get("output") != name or not _inside(manifest.output_dir, path):
            raise ChapterUnavailableError("This chapter has no valid output.")

        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_CHAPTER_BYTES + 1)
        except OSError:
            raise ChapterUnavailableError("This chapter's file is missing.")

        if not data or len(data) > MAX_CHAPTER_BYTES:
            raise ChapterUnavailableError("This chapter's file is not valid.")

        expected_size = entry.get("output_bytes")
        expected_hash = entry.get("output_sha256")

        if expected_size is not None and len(data) != expected_size:
            raise ChapterUnavailableError(
                "This chapter's file failed its integrity check.")

        if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ChapterUnavailableError(
                "This chapter's file failed its integrity check.")

        try:
            data.decode("utf-8")                     # strict: no replacements
        except UnicodeDecodeError:
            raise ChapterUnavailableError("This chapter's file is not valid UTF-8.")

        return data[len(UTF8_BOM):] if data.startswith(UTF8_BOM) else data

    def get_chapter_content(self, job_id: str, chapter_number: int) -> str:
        """The chapter text, decoded as UTF-8 and otherwise untouched."""

        return self.read_chapter_bytes(job_id, chapter_number).decode("utf-8")

    # ---- deletion ----------------------------------------------------------

    def delete_job(self, job_id: str) -> bool:
        """
        Remove a job completely: numbered TXT files, ZIP, report, manifest,
        HTML, logs and metadata, plus its objects in remote storage. Safe to
        call repeatedly. Callers must first make sure no worker is using the
        job (see CleanupService).
        """

        path = self.job_dir(job_id)
        existed = path.exists()

        if existed:
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():                     # e.g. a file briefly locked
                time.sleep(0.2)
                shutil.rmtree(path, ignore_errors=True)

        self.remote.delete_job(job_id)

        with self._locks_guard:
            self._locks.pop(job_id, None)

        return existed

    # ---- housekeeping -------------------------------------------------------

    def cleanup_temp_files(self, job_id: Optional[str] = None) -> int:
        """Remove leftover *.tmp files (e.g. from an interrupted ZIP)."""

        ids = [job_id] if job_id else [
            p.name for p in self.root.iterdir()
            if p.is_dir() and JOB_ID_RE.match(p.name)]
        removed = 0

        for jid in ids:
            base = self.job_dir(jid)
            for pattern in ("*.tmp", "downloads/*.tmp"):
                for item in base.glob(pattern):
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        pass

        return removed

    def cleanup_old_jobs(self, retention_days: int,
                         protected_ids: Iterable[str] = ()) -> list[str]:
        """
        Delete whole job directories older than the retention period.
        retention_days <= 0 disables deletion. Active/queued jobs (and any
        in protected_ids) are never deleted.
        """

        if retention_days <= 0:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        protected = set(protected_ids)
        deleted = []

        for meta in self.list_jobs():
            job_id = meta.get("job_id")

            if (not job_id or job_id in protected
                    or meta.get("status") not in FINISHED_JOB_STATUSES):
                continue

            try:
                created = datetime.fromisoformat(meta["created_at"])
            except (KeyError, ValueError):
                continue

            if created < cutoff:
                shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
                deleted.append(job_id)
                log.info("Retention cleanup removed job %s", job_id)

        return deleted
