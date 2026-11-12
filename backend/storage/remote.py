"""
Optional remote (cloud) storage hooks.

Extraction itself needs a normal local working directory (Chromium and the
pipeline write there). When a deployment also keeps job folders in a bucket
(for example Google Cloud Storage mirrored or synced from that directory),
cleanup must remove the job's objects there too. This module provides that
single capability behind a tiny interface so the FileManager stays unchanged
for local use.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Protocol

log = logging.getLogger("storage.remote")

JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")


class RemoteStorage(Protocol):
    def delete_job(self, job_id: str) -> int:
        """Delete every object belonging to the job; return how many."""


class NullRemoteStorage:
    def delete_job(self, job_id: str) -> int:
        return 0


class GCSRemoteStorage:
    """
    Deletes objects under "<prefix>/<job_id>/" in a Google Cloud Storage
    bucket. It never deletes the bucket and refuses anything but a valid
    job ID, so an empty or malformed prefix can't widen the deletion.

    Needs the optional google-cloud-storage package and a service account
    with storage.objects.list and storage.objects.delete on the bucket.
    A client may be injected (tests, custom credentials).
    """

    def __init__(self, bucket: str, prefix: str = "jobs",
                 client: Optional[object] = None) -> None:
        if not bucket:
            raise ValueError("GCS_BUCKET is required for the gcs backend.")
        if client is None:
            from google.cloud import storage          # type: ignore
            client = storage.Client()
        self._bucket = client.bucket(bucket)          # type: ignore[attr-defined]
        self._prefix = prefix.strip("/")

    def _job_prefix(self, job_id: str) -> str:
        if not JOB_ID_RE.match(job_id):
            raise ValueError("Invalid job ID.")
        base = f"{self._prefix}/" if self._prefix else ""
        return f"{base}{job_id}/"

    def delete_job(self, job_id: str) -> int:
        prefix = self._job_prefix(job_id)
        deleted = 0
        for blob in list(self._bucket.list_blobs(prefix=prefix)):
            blob.delete()
            deleted += 1
        log.info("Deleted %d object(s) under %s", deleted, prefix)
        return deleted
