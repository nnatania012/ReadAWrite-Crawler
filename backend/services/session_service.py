"""
Browser sessions.

A session is one browser tab (its ID lives in sessionStorage). It is kept
alive by heartbeats, may be closed explicitly by the page, and otherwise
expires after an idle timeout.

States, derived from the stored record and the clock:

    active    last heartbeat is recent
    closing   the page said it is closing; it can still be *reopened* by a
              heartbeat during a short grace period. This is what lets a page
              refresh (which also fires "pagehide") keep its session.
    expired   idle timeout passed, or the closing grace period ended

Expired sessions are only *reported* here; deleting their data is the job of
CleanupService.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from config import Settings
from services.errors import SessionExpired, SessionRequired
from storage.session_store import (
    InvalidSessionIdError, SessionStore, normalize_session_id,
)

log = logging.getLogger("sessions")


class SessionService:
    def __init__(self, store: SessionStore, settings: Settings,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock

    # ------------------------------------------------------------ helpers

    @staticmethod
    def parse_id(value: Optional[str]) -> str:
        if not value:
            raise SessionRequired("A session ID is required.")
        try:
            return normalize_session_id(value)
        except InvalidSessionIdError:
            raise SessionRequired("The session ID is not valid.")

    def state_of(self, record: Optional[dict]) -> str:
        """'unknown' | 'active' | 'closing' | 'expired'."""

        if record is None:
            return "unknown"

        now = self.clock()
        status = record.get("status")

        if status == "expired":
            return "expired"

        if status == "closing":
            closing_at = record.get("closing_at") or 0
            if now - closing_at < self.settings.session_close_grace_seconds:
                return "closing"
            return "expired"

        if now - record.get("last_seen", 0) > self.settings.session_idle_timeout_seconds:
            return "expired"

        return "active"

    def is_due_for_cleanup(self, record: Optional[dict]) -> bool:
        return self.state_of(record) == "expired"

    # -------------------------------------------------------- operations

    def heartbeat(self, session_id: str) -> dict:
        """
        Register or refresh a session (also reopens one that is "closing"
        within its grace period). Raises SessionExpired otherwise.
        """

        sid = self.parse_id(session_id)

        with self.store._lock:
            record = self.store.read(sid)
            state = self.state_of(record)

            if state in ("expired",):
                raise SessionExpired(
                    "This session has ended. Start a new session.")

            self.store.create_or_touch(sid)          # unknown, active, closing

        return {
            "session_id": sid,
            "status": "active",
            "heartbeat_seconds": self.settings.session_heartbeat_seconds,
            "idle_timeout_seconds": self.settings.session_idle_timeout_seconds,
        }

    def require_active(self, session_id: Optional[str]) -> str:
        """Gate for every job endpoint. Returns the normalised session ID."""

        sid = self.parse_id(session_id)
        state = self.state_of(self.store.read(sid))

        if state == "active":
            return sid

        raise SessionExpired(
            "This session has ended. Start a new session."
            if state in ("expired", "unknown")
            else "This session is closing.")

    def close(self, session_id: str) -> None:
        """
        The page is closing. Mark the session as closing; its data is removed
        after the grace period unless a heartbeat reopens it first. Never
        touches running jobs.
        """

        sid = self.parse_id(session_id)

        with self.store._lock:
            record = self.store.read(sid)
            if record is None or record.get("status") == "expired":
                return
            self.store.update(sid, status="closing", closing_at=self.clock())
