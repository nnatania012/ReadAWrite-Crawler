"""
pipeline.py

Fully automated ReadAWrite chapter extraction pipeline.

    python pipeline.py chapters.html
    python pipeline.py chapters.html -o MyStory      (optional folder name)

Sequence (strictly one chapter at a time):

    chapters.html --chapext--> ordered chapter URLs
    for each chapter N:
        rwfetch  -> rendered HTML  (input.html)
        rwext    -> detect Chat/Lec, extract text
        verify   -> N.txt (atomic write)
        manifest -> pipeline_state.json (atomic write)

Output is written beside the input file, in "<input name>_output/" (or in
the folder given with -o/--output):

    chapters_output/
        input.html            working copy of the last fetched chapter
        1.txt, 2.txt, ...     extracted chapters (original chapter numbers)
        pipeline_state.json   progress manifest (used for automatic resume)
        logs/pipeline.log     detailed log

Install:
    pip install beautifulsoup4 requests playwright
    python -m playwright install chromium

Exit codes:
    0    every chapter is complete
    1    input/setup error (nothing was processed)
    2    finished, but some chapters are failed/partial/not processed
    130  interrupted with Ctrl+C (run again to resume)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import chapext
    import rwext
    import rwfetch
    from rwfetch import FetchResult, FetchStatus
except ImportError as import_error:
    sys.exit(
        f"Import error: {import_error}\n\n"
        "Make sure chapext.py, rwfetch.py, rwext.py and pipeline.py are in "
        "the same folder, and that the dependencies are installed:\n"
        "    pip install beautifulsoup4 requests playwright\n"
        "    python -m playwright install chromium"
    )


# ============================================================
# SETTINGS (fixed defaults; nothing here needs to be configured)
# ============================================================

MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (2, 5)        # before attempt 2, before attempt 3
INTER_CHAPTER_DELAY_SECONDS = 1.5    # politeness delay between chapters
MAX_CONSECUTIVE_ACCESS_DENIALS = 3   # stop if the site keeps refusing us
BROWSER_RESTART_ATTEMPTS = 2

STATE_FILE_NAME = "pipeline_state.json"
STATE_VERSION = 1
WORK_HTML_NAME = "input.html"
PARTIAL_HTML_NAME = "last_partial.html"
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "pipeline.log"

READAWRITE_HOSTS = {"www.readawrite.com", "readawrite.com"}

BANNER = "=" * 52

log = logging.getLogger("pipeline")


def say(message: str = "") -> None:
    """Terminal + log line (INFO)."""
    log.info(message)


# ============================================================
# SMALL UTILITIES
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)

    return digest.hexdigest()


def replace_with_retry(source: Path, destination: Path) -> None:
    """os.replace with a few retries (Windows can briefly lock files)."""

    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 5:
                raise

            time.sleep(0.2 * (attempt + 1))


def write_text_atomic(
    path: Path,
    text: str,
    newline: Optional[str] = None,
) -> None:
    """
    Write text to "<path>.tmp", flush it to disk, then atomically rename it
    over the final path. A crash can never leave a half-written final file.

    newline=None keeps Python's normal text-mode behaviour (identical to
    rwext.py's Path.write_text); newline="" writes the text unchanged.
    """

    temp = path.with_name(path.name + ".tmp")

    with temp.open("w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())

    replace_with_retry(temp, path)


def fingerprint(urls: list[str]) -> str:
    return sha256_bytes("\n".join(urls).encode("utf-8"))


# ============================================================
# STOP FLAG (Ctrl+C)
# ============================================================

class StopFlag:
    """First Ctrl+C = finish safely and stop; second = abort now."""

    def __init__(self) -> None:
        self.requested = False
        self._count = 0

    def install(self) -> None:
        def handler(signum, frame):
            self._count += 1

            if self._count == 1:
                self.requested = True
                print(
                    "\nInterrupt received. Finishing the current chapter "
                    "safely, then stopping...\n"
                    "(Press Ctrl+C again to abort immediately.)",
                    flush=True,
                )
            else:
                raise KeyboardInterrupt

        signal.signal(signal.SIGINT, handler)


# ============================================================
# INPUT: CHAPTER URL EXTRACTION AND VALIDATION
# ============================================================

class InputError(Exception):
    """A problem with the user's input file (reported before Chromium)."""


def is_valid_chapter_url(url: str) -> bool:
    parsed = urlparse(url)

    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc.lower() in READAWRITE_HOSTS
        and parsed.path.startswith("/c/")
        and len(parsed.path) > len("/c/")
    )


def load_chapter_urls(input_path: Path) -> list[str]:
    """Reuse chapext to read the chapter list, then validate it."""

    if not input_path.exists():
        raise InputError(f"Input file not found: {input_path}")

    if not input_path.is_file():
        raise InputError(f"Input path is not a file: {input_path}")

    try:
        html = chapext.load_html(str(input_path))
    except (OSError, UnicodeError) as error:
        raise InputError(f"Could not read {input_path}: {error}")

    links = chapext.extract_chapter_links(html)

    # chapext already removes duplicates; do it again defensively while
    # keeping the original order.
    links = list(dict.fromkeys(links))

    if not links:
        raise InputError(
            "No chapter links were found. Check that the HTML is a "
            "ReadAWrite chapter-list page with a supported structure."
        )

    invalid = [link for link in links if not is_valid_chapter_url(link)]

    if invalid:
        raise InputError(
            f"{len(invalid)} extracted URL(s) are not valid ReadAWrite "
            f"chapter URLs, for example: {invalid[0]}"
        )

    return links


# ============================================================
# MANIFEST (pipeline_state.json)
# ============================================================

class State:
    """Progress manifest with atomic saves."""

    def __init__(self, path: Path, data: dict) -> None:
        self.path = path
        self.data = data

    @classmethod
    def new(cls, path: Path, source_file: str, list_hash: str,
            urls: list[str]) -> "State":
        stamp = now_iso()

        data = {
            "version": STATE_VERSION,
            "source_file": source_file,
            "chapter_list_hash": list_hash,
            "total_chapters": len(urls),
            "created_at": stamp,
            "updated_at": stamp,
            "chapters": {
                str(number): {"url": url, "status": "pending",
                              "output": None, "attempts": 0}
                for number, url in enumerate(urls, start=1)
            },
        }

        return cls(path, data)

    @property
    def chapters(self) -> dict:
        return self.data["chapters"]

    def entry(self, number: int) -> dict:
        return self.chapters[str(number)]

    def set_entry(self, number: int, entry: dict) -> None:
        self.chapters[str(number)] = entry

    def save(self) -> None:
        self.data["updated_at"] = now_iso()

        write_text_atomic(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False),
            newline="\n",
        )

    def count(self, status: str) -> int:
        return sum(
            1 for entry in self.chapters.values()
            if entry.get("status") == status
        )


def read_state_file(path: Path) -> tuple[str, Optional[dict]]:
    """Return ('missing'|'ok'|'corrupt', data)."""

    if not path.exists():
        return "missing", None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if (
            not isinstance(data, dict)
            or not isinstance(data.get("chapters"), dict)
            or "chapter_list_hash" not in data
        ):
            return "corrupt", None

        return "ok", data

    except (OSError, ValueError):
        return "corrupt", None


NUMBERED_TXT = re.compile(r"^\d+\.txt$")


def has_numbered_outputs(directory: Path) -> bool:
    return directory.is_dir() and any(
        NUMBERED_TXT.match(item.name) for item in directory.iterdir()
    )


def resolve_output_dir(
    input_path: Path,
    list_hash: str,
    output_name: Optional[str] = None,
) -> tuple[Path, Optional[dict]]:
    """
    Choose the output directory for this exact chapter list.

    output_name is the optional -o/--output value. A plain name is created
    beside the input file; an absolute path is used as given. When it is not
    supplied, the folder is "<input name>_output".

    1. base folder                - used when new/empty, or when its manifest
                                    has the same chapter-list fingerprint.
    2. "<base>_<hash8>"           - used when the base folder belongs to
                                    a different chapter list.

    The same chapter list therefore always maps to the same directory, and
    two different lists never share one.
    """

    base_name = output_name or f"{input_path.stem}_output"
    base = input_path.parent / base_name
    separate = base.with_name(f"{base.name}_{list_hash[:8]}")

    for candidate, is_last in ((base, False), (separate, True)):
        if not candidate.exists():
            return candidate, None

        status, data = read_state_file(candidate / STATE_FILE_NAME)

        if status == "ok":
            if data["chapter_list_hash"] == list_hash:
                return candidate, data

            continue

        if status == "missing":
            if has_numbered_outputs(candidate) and not is_last:
                continue

            return candidate, None

        # corrupt manifest
        if is_last:
            backup = candidate / f"pipeline_state.corrupt.{int(time.time())}.json"
            replace_with_retry(candidate / STATE_FILE_NAME, backup)
            return candidate, None

    raise InputError(
        f"Could not choose a safe output directory for {input_path.name}."
    )


def is_chapter_complete(entry: dict, url: str, output_dir: Path,
                        number: int) -> bool:
    """A chapter is skipped only if its record AND its file both check out."""

    if entry.get("status") != "success":
        return False

    if entry.get("url") != url:
        return False

    if entry.get("output") != f"{number}.txt":
        return False

    path = output_dir / entry["output"]

    try:
        size = path.stat().st_size
    except OSError:
        return False

    if size <= 0 or size != entry.get("output_bytes"):
        return False

    try:
        return sha256_file(path) == entry.get("output_sha256")
    except OSError:
        return False


# ============================================================
# BROWSER SESSION
# ============================================================

class BrowserUnavailable(RuntimeError):
    """Chromium could not be (re)started."""


class BrowserSession:
    """One Chromium for the whole run, with a safe restart."""

    def __init__(self) -> None:
        self.playwright = None
        self.browser = None

    @property
    def alive(self) -> bool:
        return self.browser is not None and self.browser.is_connected()

    async def start(self) -> None:
        self.playwright, self.browser = await rwfetch.open_browser()

    async def close(self) -> None:
        playwright, browser = self.playwright, self.browser
        self.playwright = None
        self.browser = None

        await rwfetch.close_browser(playwright, browser)

    async def restart(self) -> None:
        await self.close()

        last_error: Optional[BaseException] = None

        for attempt in range(1, BROWSER_RESTART_ATTEMPTS + 1):
            try:
                await self.start()
                log.debug("Chromium restarted (attempt %d).", attempt)
                return
            except Exception as error:
                last_error = error
                log.debug("Chromium restart failed: %r", error)
                await asyncio.sleep(2)

        raise BrowserUnavailable(
            f"Could not restart Chromium: {last_error}"
        )


# ============================================================
# THE PIPELINE
# ============================================================

class Pipeline:
    def __init__(self, input_path: Path,
                 output_name: Optional[str] = None) -> None:
        self.input_path = input_path
        self.output_name = output_name
        self.urls: list[str] = []
        self.list_hash = ""
        self.output_dir: Path = input_path.parent
        self.state: Optional[State] = None
        self.session = BrowserSession()
        self.stop = StopFlag()

        self.skipped = 0
        self.succeeded_now = 0
        self.attempted = 0
        self.interrupted = False
        self.aborted_reason: Optional[str] = None
        self.access_denials_in_a_row = 0

    # ---------------- setup (before Chromium) --------------------

    def prepare(self) -> None:
        self.urls = load_chapter_urls(self.input_path)
        self.list_hash = fingerprint(self.urls)

        self.output_dir, existing = resolve_output_dir(
            self.input_path, self.list_hash, self.output_name
        )

        (self.output_dir / LOG_DIR_NAME).mkdir(parents=True, exist_ok=True)

        setup_logging(self.output_dir / LOG_DIR_NAME / LOG_FILE_NAME)

        # Remove scratch files left behind by an interrupted run.
        for leftover in self.output_dir.glob("*.tmp"):
            try:
                leftover.unlink()
            except OSError:
                pass

        state_path = self.output_dir / STATE_FILE_NAME

        if existing is not None:
            self.state = State(state_path, existing)
            self.state.data["source_file"] = self.input_path.name
            self.state.data["total_chapters"] = len(self.urls)

            for number, url in enumerate(self.urls, start=1):
                self.state.chapters.setdefault(
                    str(number),
                    {"url": url, "status": "pending",
                     "output": None, "attempts": 0},
                )
        else:
            self.state = State.new(
                state_path, self.input_path.name, self.list_hash, self.urls
            )

        self.state.save()

    # ---------------- helpers ------------------------------------

    async def interruptible_sleep(self, seconds: float) -> bool:
        """Sleep, waking early on Ctrl+C. Returns False if interrupted."""

        end = time.monotonic() + seconds

        while time.monotonic() < end:
            if self.stop.requested:
                return False

            await asyncio.sleep(min(0.25, end - time.monotonic()))

        return not self.stop.requested

    async def fetch(self, url: str) -> FetchResult:
        """Fetch one chapter, restarting Chromium first if it is dead."""

        if not self.session.alive:
            say("Chromium is not running; restarting it...")
            await self.session.restart()

        try:
            result = await rwfetch.fetch_chapter(self.session.browser, url)
        except Exception as error:
            log.debug("Unexpected fetch exception", exc_info=True)

            result = FetchResult(
                status=FetchStatus.FAILED,
                detail=f"{type(error).__name__}: {error}",
                browser_crashed=True,
            )

        if result.browser_crashed or not self.session.alive:
            say("Chromium crashed or disconnected; restarting it...")
            await self.session.restart()

        return result

    @staticmethod
    def extract(html: str) -> tuple[str, list[str]]:
        """Reuse rwext: detect Chat/Lec, run the default extractor."""

        soup = rwext.get_soup(html)
        layout = rwext.detect_mode(soup)

        if layout == "Chat":
            lines = rwext.extract_chat(soup, keep_blank=False, pair=False)
        else:
            lines = rwext.extract_lec(
                soup, by_paragraph=False, blank_between=False
            )

        return layout, lines

    def write_chapter_file(self, number: int, lines: list[str]) -> dict:
        """
        Write N.txt atomically and verify it. Raises ValueError if the
        result is not a valid, non-empty chapter.
        """

        if not lines or not any(line.strip() for line in lines):
            raise ValueError("The extractor produced no content")

        final = self.output_dir / f"{number}.txt"
        temp = final.with_name(final.name + ".tmp")

        # Same text and same text-mode newline behaviour as rwext.py.
        text = "\n".join(lines)

        with temp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        # Verify the temporary file before it becomes the final file.
        size = temp.stat().st_size

        if size <= 0:
            temp.unlink(missing_ok=True)
            raise ValueError("The output file is empty")

        try:
            readback = temp.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            temp.unlink(missing_ok=True)
            raise ValueError(f"Output verification failed: {error}")

        if not readback.strip():
            temp.unlink(missing_ok=True)
            raise ValueError("The output file contains only whitespace")

        replace_with_retry(temp, final)

        return {
            "output": final.name,
            "output_bytes": final.stat().st_size,
            "output_sha256": sha256_file(final),
        }

    # ---------------- one chapter --------------------------------

    async def process_chapter(self, number: int, url: str) -> str:
        """
        Returns 'success', 'partial', 'failed' or 'interrupted'.
        The manifest is updated before returning.
        """

        state = self.state
        assert state is not None

        state.set_entry(number, {
            "url": url,
            "status": "in_progress",
            "output": None,
            "attempts": 0,
            "started_at": now_iso(),
        })
        state.save()

        final_status = "failed"
        final_error = "Unknown error"
        auth_denied = False
        attempts_used = 0

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                delay = RETRY_DELAYS_SECONDS[
                    min(attempt - 2, len(RETRY_DELAYS_SECONDS) - 1)
                ]

                say(
                    f"Retrying in {delay}s "
                    f"(attempt {attempt}/{MAX_ATTEMPTS})..."
                )

                if not await self.interruptible_sleep(delay):
                    return self._finish_interrupted(number, url, attempts_used)

            attempts_used = attempt
            ctx = f"[chapter {number}] [attempt {attempt}/{MAX_ATTEMPTS}] {url}"

            say("Fetching chapter...")
            log.debug("%s fetch started", ctx)

            try:
                result = await self.fetch(url)
            except BrowserUnavailable:
                state.set_entry(number, {
                    "url": url, "status": "failed", "output": None,
                    "attempts": attempt,
                    "error": "Chromium could not be restarted",
                    "last_attempt_at": now_iso(),
                })
                state.save()
                raise

            log.debug(
                "%s fetch status=%s layout=%s expected=%s rendered=%s "
                "http=%s detail=%s",
                ctx, result.status.value, result.layout,
                result.expected_items, result.rendered_items,
                result.http_status, result.detail,
            )

            # ---- FAILED -----------------------------------------

            if result.status is FetchStatus.FAILED:
                final_status = "failed"
                final_error = result.detail or "Fetch failed"
                auth_denied = result.auth_required

                say(f"Fetch failed: {final_error}")

                if not result.retryable:
                    break

                continue

            # ---- PARTIAL (never extracted) ----------------------

            if result.status is FetchStatus.PARTIAL:
                final_status = "partial"
                final_error = result.detail or "Rendering not confirmed"
                auth_denied = False

                say(f"Partial render: {final_error}")

                if result.html:
                    try:
                        write_text_atomic(
                            self.output_dir / PARTIAL_HTML_NAME,
                            result.html,
                            newline="",
                        )
                    except OSError:
                        log.debug("Could not save partial HTML",
                                  exc_info=True)

                continue

            # ---- SUCCESS: save HTML, extract, verify ------------

            auth_denied = False
            say("Content fully rendered.")

            try:
                write_text_atomic(
                    self.output_dir / WORK_HTML_NAME,
                    result.html or "",
                    newline="",
                )

                say("Detecting format...")
                layout, lines = self.extract(result.html or "")
                say(f"Detected: {layout}")

                say("Extracting text...")
                info = self.write_chapter_file(number, lines)

            except Exception as error:
                final_status = "failed"
                final_error = f"Extraction error: {error}"

                say(f"Extraction failed: {error}")
                log.debug("%s extraction traceback:\n%s",
                          ctx, traceback.format_exc())
                continue

            say(f"{len(lines)} entries extracted.")
            say(f"Saved: {info['output']}")

            state.set_entry(number, {
                "url": url,
                "status": "success",
                "output": info["output"],
                "output_bytes": info["output_bytes"],
                "output_sha256": info["output_sha256"],
                "attempts": attempt,
                "format": layout,
                "entry_count": len(lines),
                "completed_at": now_iso(),
            })
            state.save()

            log.debug(
                "%s SUCCESS format=%s entries=%d output=%s bytes=%d",
                ctx, layout, len(lines),
                info["output"], info["output_bytes"],
            )

            self.access_denials_in_a_row = 0
            say("Status: SUCCESS")
            return "success"

        # ---- every attempt used up (or a non-retryable error) -----

        state.set_entry(number, {
            "url": url,
            "status": final_status,
            "output": None,
            "attempts": attempts_used,
            "error": final_error,
            "last_attempt_at": now_iso(),
        })
        state.save()

        log.debug("[chapter %d] %s after %d attempt(s): %s",
                  number, final_status.upper(), attempts_used, final_error)

        if auth_denied:
            self.access_denials_in_a_row += 1
        else:
            self.access_denials_in_a_row = 0

        say(f"Status: {final_status.upper()} - {final_error}")
        return final_status

    def _finish_interrupted(self, number: int, url: str,
                            attempts_used: int) -> str:
        """Record an interrupted chapter as unfinished (never as success)."""

        assert self.state is not None

        self.state.set_entry(number, {
            "url": url,
            "status": "pending",
            "output": None,
            "attempts": attempts_used,
            "error": "Interrupted before completion",
            "last_attempt_at": now_iso(),
        })
        self.state.save()

        return "interrupted"

    # ---------------- main loop ----------------------------------

    async def run(self) -> None:
        state = self.state
        assert state is not None

        total = len(self.urls)

        say("Checking previous progress...")

        complete: set[int] = set()

        for number, url in enumerate(self.urls, start=1):
            if is_chapter_complete(
                state.entry(number), url, self.output_dir, number
            ):
                complete.add(number)

        previously_failed = sum(
            1 for number, url in enumerate(self.urls, start=1)
            if number not in complete
            and state.entry(number).get("status") in ("failed", "partial")
        )

        remaining = total - len(complete)

        say(f"Completed chapters: {len(complete)}")

        if previously_failed:
            say(f"Failed/partial chapters to retry: {previously_failed}")

        say(f"Remaining chapters: {remaining}")

        if complete:
            say("")
            say("Skipping verified completed chapters...")

            if remaining:
                say("Resuming from unfinished chapters...")

        self.skipped = len(complete)

        if remaining == 0:
            say("")
            say("All chapters are already complete. Nothing to do.")
            return

        say("")
        say("Starting Chromium...")

        try:
            await self.session.start()
        except Exception as error:
            raise BrowserUnavailable(
                f"Could not launch Chromium: {error}\n"
                "If Chromium is not installed, run:\n"
                "    python -m playwright install chromium"
            )

        try:
            todo = [
                (number, url)
                for number, url in enumerate(self.urls, start=1)
                if number not in complete
            ]

            for position, (number, url) in enumerate(todo):
                if self.stop.requested:
                    self.interrupted = True
                    break

                if (
                    self.access_denials_in_a_row
                    >= MAX_CONSECUTIVE_ACCESS_DENIALS
                ):
                    self.aborted_reason = (
                        "The site refused access to "
                        f"{MAX_CONSECUTIVE_ACCESS_DENIALS} chapters in a "
                        "row (login or permission required). Stopped "
                        "instead of repeating blocked requests. Progress "
                        "is saved."
                    )
                    break

                say("")
                say(f"[{number}/{total}] Chapter {number}")

                self.attempted += 1
                outcome = await self.process_chapter(number, url)

                if outcome == "success":
                    self.succeeded_now += 1

                if outcome == "interrupted":
                    self.interrupted = True
                    break

                if self.stop.requested:
                    self.interrupted = True
                    break

                if position < len(todo) - 1:
                    if not await self.interruptible_sleep(
                        INTER_CHAPTER_DELAY_SECONDS
                    ):
                        self.interrupted = True
                        break

        finally:
            await self.session.close()

    # ---------------- report -------------------------------------

    def print_summary(self) -> None:
        if self.state is None:
            return

        state = self.state
        total = len(self.urls)

        success_total = 0
        failures: list[tuple[int, str, str]] = []
        partials = 0
        failed = 0
        unprocessed = 0

        for number, url in enumerate(self.urls, start=1):
            entry = state.entry(number)
            status = entry.get("status")

            if is_chapter_complete(entry, url, self.output_dir, number):
                success_total += 1
            elif status == "failed":
                failed += 1
                failures.append((number, "failed", entry.get("error", "")))
            elif status == "partial":
                partials += 1
                failures.append((number, "partial", entry.get("error", "")))
            else:
                unprocessed += 1

        say("")
        say(BANNER)

        if self.interrupted:
            say("PIPELINE INTERRUPTED")
        elif self.aborted_reason:
            say("PIPELINE STOPPED")
        else:
            say("PIPELINE FINISHED")

        say(BANNER)
        say("")
        say(f"Total chapters: {total}")
        say(
            f"Complete: {success_total} "
            f"({self.succeeded_now} this run, "
            f"{self.skipped} previously completed)"
        )
        say(f"Partial: {partials}")
        say(f"Failed: {failed}")
        say(f"Not yet processed: {unprocessed}")

        if failures:
            say("")
            say("Chapters needing attention:")

            for number, kind, error in failures[:25]:
                say(f"  {number}: {kind} - {error}")

            if len(failures) > 25:
                say(f"  ... and {len(failures) - 25} more (see the manifest)")

        if self.aborted_reason:
            say("")
            say(self.aborted_reason)

        say("")
        say("Output directory:")
        say(str(self.output_dir))
        say("")
        say("Log:")
        say(str(self.output_dir / LOG_DIR_NAME / LOG_FILE_NAME))

        if self.interrupted or self.aborted_reason or (
            failed or partials or unprocessed
        ):
            say("")
            say(
                "Run the same command again to resume: completed chapters "
                "are skipped and the rest are retried."
            )

    def exit_code(self) -> int:
        if self.interrupted:
            return 130

        if self.state is None:
            return 1

        for number, url in enumerate(self.urls, start=1):
            if not is_chapter_complete(
                self.state.entry(number), url, self.output_dir, number
            ):
                return 2

        return 0


# ============================================================
# LOGGING
# ============================================================

def setup_logging(log_path: Path) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))

    root.addHandler(file_handler)
    root.addHandler(console)

    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ============================================================
# ENTRY POINT
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch and extract every chapter listed in a ReadAWrite "
            "chapter-list HTML file."
        )
    )

    parser.add_argument(
        "chapters_html",
        help="Chapter-list HTML file (e.g. chapters.html).",
    )

    parser.add_argument(
        "-o",
        "--output",
        metavar="OUTPUT_FOLDER_NAME",
        default=None,
        help=(
            "Optional name for the output folder (created beside the "
            "input file). Default: <input name>_output."
        ),
    )

    args = parser.parse_args()

    if args.output is not None and not args.output.strip():
        parser.error("-o/--output must not be empty")

    input_path = Path(args.chapters_html).expanduser().resolve()

    # Until the output directory exists, log to the console only.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print(BANNER)
    print("ReadAWrite Automated Chapter Pipeline")
    print(BANNER)
    print()
    print(f"Input: {input_path.name}")

    pipeline = Pipeline(input_path, args.output.strip() if args.output else None)

    try:
        print("Extracting chapter URLs...")
        pipeline.prepare()
    except InputError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    say(f"Output: {pipeline.output_dir.name}/")
    say(f"Found {len(pipeline.urls)} chapters.")
    say("")

    log.debug("Input file: %s", input_path)
    log.debug("Chapter list SHA-256: %s", pipeline.list_hash)

    pipeline.stop.install()

    try:
        asyncio.run(pipeline.run())

    except KeyboardInterrupt:
        pipeline.interrupted = True

    except BrowserUnavailable as error:
        say("")
        say(f"Error: {error}")
        pipeline.aborted_reason = str(error)

    except Exception as error:
        log.error("Unexpected pipeline error: %s", error)
        log.debug("Traceback:\n%s", traceback.format_exc())
        pipeline.aborted_reason = f"Unexpected error: {error}"

    pipeline.print_summary()

    return pipeline.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
