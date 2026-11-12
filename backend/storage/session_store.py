"""
Persistent browser-session records.

One small JSON file per session in <DATA_DIR>/_sessions/<uuid>.json. The
leading underscore keeps the folder from ever being mistaken for a job
directory. Times are epoch seconds from an injectable clock so expiry logic
can be tested without waiting.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class InvalidSessionIdError(ValueError):
    pass


def normalize_session_id(value: object) -> str:
    """Return the lower-case UUID, or raise InvalidSessionIdError."""

    text = str(value or "").strip().lower()

    if not SESSION_ID_RE.match(text):
        raise InvalidSessionIdError("Invalid session ID.")

    return text


class SessionStore:
    def __init__(self, root: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(root).resolve() / "_sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()

    def _path(self, session_id: str) -> Path:
        session_id = normalize_session_id(session_id)
        path = (self.dir / f"{session_id}.json").resolve()
        if path.parent != self.dir:
            raise InvalidSessionIdError("Invalid session ID.")
        return path

    def read(self, session_id: str) -> Optional[dict]:
        path = self._path(session_id)
        for attempt in range(3):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except ValueError:
                time.sleep(0.02 * (attempt + 1))
        return None

    def write(self, record: dict) -> None:
        with self._lock:
            path = self._path(record["session_id"])
            temp = path.with_name(path.name + ".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(record))
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(6):
                try:
                    os.replace(temp, path)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))

    def create_or_touch(self, session_id: str) -> dict:
        """Register the session if new; otherwise just refresh last_seen."""

        session_id = normalize_session_id(session_id)
        with self._lock:
            now = self.clock()
            record = self.read(session_id) or {
                "session_id": session_id, "created_at": now,
                "status": "active", "closing_at": None,
            }
            record.update(last_seen=now, status="active", closing_at=None)
            self.write(record)
            return record

    def update(self, session_id: str, **fields) -> Optional[dict]:
        with self._lock:
            record = self.read(session_id)
            if record is None:
                return None
            record.update(fields)
            self.write(record)
            return record

    def delete(self, session_id: str) -> None:
        with self._lock:
            try:
                self._path(session_id).unlink()
            except FileNotFoundError:
                pass

    def list_ids(self) -> list[str]:
        ids = []
        for item in self.dir.glob("*.json"):
            if SESSION_ID_RE.match(item.stem):
                ids.append(item.stem)
        return ids
