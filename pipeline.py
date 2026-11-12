"""
pipeline.py

Fully automated ReadAWrite chapter extraction pipeline.

    python pipeline.py "https://www.readawrite.com/..."
    python pipeline.py "https://www.readawrite.com/..." -o MyStory
    python pipeline.py "https://www.readawrite.com/..." --stop-file STOP

The only required input is the URL of the online chapter-list page. (A
locally saved chapter-list HTML file is still accepted in place of the URL,
which is handy for offline testing.)

Sequence (strictly one chapter at a time):

    chapter-list URL --Chromium--> rendered chapter-list HTML
    rendered HTML    --chapext---> ordered chapter URLs
    for each chapter N:
        rwfetch  -> rendered HTML  (input.html)
        rwext    -> detect Chat/Lec, extract text
        verify   -> N.txt (atomic write)
        manifest -> pipeline_state.json (atomic write)

Output is written to a folder named after the story page, e.g.
"<ARTICLE_ID>_output/" (or the folder given with -o/--output), created in the
current directory. For a local HTML file input, the folder is created beside
that file as "<file name>_output/".

    <name>_output/
        input.html            working copy of the last fetched chapter
        1.txt, 2.txt, ...     extracted chapters (original chapter numbers)
        pipeline_state.json   progress manifest (used for automatic resume)
        logs/pipeline.log     detailed log

Install:
    pip install beautifulsoup4 requests playwright
    python -m playwright install chromium

Exit codes:
    0    every chapter is complete
    1    input/setup error, or the chapter list could not be retrieved
         (nothing was processed)
    2    finished, but some chapters are failed/partial/not processed
    130  interrupted with Ctrl+C (run again to resume)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
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
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
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

# Chapter-list page retrieval
LIST_NAVIGATION_TIMEOUT_MS = 60_000
LIST_NETWORK_IDLE_TIMEOUT_MS = 10_000
LIST_FIRST_LINKS_TIMEOUT_S = 60.0    # wait for the first chapter links
LIST_POLL_SECONDS = 1.0
LIST_STABLE_ROUNDS = 3               # consecutive unchanged link counts
LIST_MAX_SCROLL_ROUNDS = 60          # scroll-to-bottom rounds (lazy loading)

# Collapsed chapter groups ("#11 - #21" grey bars)
GROUP_LOAD_TIMEOUT_MS = 15_000       # wait for one group's chapters to load
GROUP_CLICK_ATTEMPTS = 3
GROUP_DELAY_SECONDS = 0.5            # politeness delay between groups
GROUP_MAX_ROUNDS = 10                # re-scan in case new groups appear

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
    """
    First Ctrl+C = finish safely and stop; second = abort now.

    A stop file (--stop-file PATH) does the same as the first Ctrl+C: when the
    file appears, the current chapter is finished and the run stops. This is
    how the web backend cancels a job gracefully on every operating system.
    """

    def __init__(self) -> None:
        self._requested = False
        self._count = 0
        self.stop_file: Optional[Path] = None

    @property
    def requested(self) -> bool:
        if not self._requested and self.stop_file is not None:
            try:
                if self.stop_file.exists():
                    self._requested = True
                    log.info("Stop file found; finishing the current "
                             "chapter, then stopping...")
            except OSError:
                pass

        return self._requested

    @requested.setter
    def requested(self, value: bool) -> None:
        self._requested = value

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
    """A problem with the user's input (file or URL)."""


def is_valid_chapter_url(url: str) -> bool:
    parsed = urlparse(url)

    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc.lower() in READAWRITE_HOSTS
        and parsed.path.startswith("/c/")
        and len(parsed.path) > len("/c/")
    )


def is_http_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def validate_list_url(text: str) -> str:
    """Validate the chapter-list URL given on the command line."""

    url = text.strip()

    if re.search(r"\s", url):
        raise InputError(f"The URL contains whitespace: {url!r}")

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InputError(
            f"Not a valid http(s) URL: {url!r}. Include the full address, "
            "for example https://www.readawrite.com/a/ARTICLE_ID"
        )

    return url


def folder_name_for_url(url: str) -> str:
    """Default output-folder name for a chapter-list URL."""

    parsed = urlparse(url)
    segments = [seg for seg in parsed.path.split("/") if seg]

    candidate = segments[-1] if segments else parsed.netloc
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("._")

    return f"{candidate or 'chapters'}_output"


def urls_from_html(html: str, base_url: Optional[str] = None) -> list[str]:
    """
    Reuse chapext to extract the chapter URLs from chapter-list HTML
    (a saved file's content or the rendered page), then validate them.
    """

    unloaded = chapext.find_unloaded_groups(html)

    if unloaded:
        raise InputError(
            f"{len(unloaded)} collapsed chapter group(s) were never "
            f"expanded ({chapext.describe_unloaded_groups(unloaded)}), so "
            "this HTML only contains the chapters that were visible. "
            "Run the pipeline with the chapter-list page URL (it expands "
            "the groups automatically), or expand every grey bar in your "
            "browser and save the page again."
        )

    links = chapext.extract_chapter_links(html, base_url=base_url)

    # chapext already removes duplicates; do it again defensively while
    # keeping the original order.
    links = list(dict.fromkeys(links))

    if not links:
        raise InputError(
            "No chapter links were found. Check that this is a ReadAWrite "
            "chapter-list page with a supported structure."
        )

    invalid = [link for link in links if not is_valid_chapter_url(link)]

    if invalid:
        raise InputError(
            f"{len(invalid)} extracted URL(s) are not valid ReadAWrite "
            f"chapter URLs, for example: {invalid[0]}"
        )

    return links


def load_chapter_urls(input_path: Path) -> list[str]:
    """Read a locally saved chapter-list HTML file."""

    if not input_path.exists():
        raise InputError(f"Input file not found: {input_path}")

    if not input_path.is_file():
        raise InputError(f"Input path is not a file: {input_path}")

    try:
        html = chapext.load_html(str(input_path))
    except (OSError, UnicodeError) as error:
        raise InputError(f"Could not read {input_path}: {error}")

    return urls_from_html(html)


# ============================================================
# CHAPTER-LIST PAGE RETRIEVAL (online URL -> rendered HTML)
# ============================================================

def _http_problem(
    status: Optional[int],
    requested_url: str,
    final_url: str,
) -> Optional[FetchResult]:
    """Map an HTTP status / login redirect to a FAILED result, or None."""

    if status in (401, 403):
        return FetchResult(
            status=FetchStatus.FAILED,
            detail=(
                f"HTTP {status}: the site is restricting access "
                "(login or permission required)"
            ),
            http_status=status, retryable=False, auth_required=True,
        )

    if rwfetch._looks_like_login(requested_url, final_url):
        return FetchResult(
            status=FetchStatus.FAILED,
            detail="Redirected to a login page: this page requires a "
                   "signed-in account",
            http_status=status, retryable=False, auth_required=True,
        )

    if status in (404, 410):
        return FetchResult(
            status=FetchStatus.FAILED,
            detail=f"HTTP {status}: page not found",
            http_status=status, retryable=False,
        )

    if status is not None and (status == 429 or status >= 500):
        return FetchResult(
            status=FetchStatus.FAILED,
            detail=f"HTTP {status} from the server",
            http_status=status, retryable=True,
        )

    return None


UNLOADED_GROUP_IDS_JS = """(selector) =>
    Array.from(document.querySelectorAll(selector))
        .filter(el => el.getAttribute('is_loaded') !== 'true')
        .map(el => el.id)
"""

GROUP_LOADED_JS = """(id) => {
    const el = document.getElementById(id);
    return !!el && el.getAttribute('is_loaded') === 'true';
}"""

GROUP_ROW_COUNTS_JS = """([selector, rowSelector]) =>
    Array.from(document.querySelectorAll(selector)).map(el => ({
        id: el.id,
        rows: el.querySelectorAll(rowSelector).length
    }))
"""


async def expand_collapsed_groups(page) -> tuple[int, list[str]]:
    """
    Click every collapsed chapter group ("#11 - #21" grey bar) so the site's
    own JavaScript loads its chapters, exactly like a reader clicking it.

    A group is only clicked while its is_loaded attribute is not "true"
    (clicking a loaded group would merely collapse it again). Each click is
    verified by waiting for is_loaded to become "true"; if it doesn't, the
    click is retried (the page's handlers may not be attached yet).

    Returns (groups_expanded, ids_that_could_not_be_expanded).
    """

    expanded = 0
    failed: list[str] = []

    for _ in range(GROUP_MAX_ROUNDS):
        pending = [
            group_id
            for group_id in await page.evaluate(
                UNLOADED_GROUP_IDS_JS, chapext.GROUP_ROW_SELECTOR
            )
            if group_id and group_id not in failed
        ]

        if not pending:
            break

        for group_id in pending:
            loaded = False

            for click in range(1, GROUP_CLICK_ATTEMPTS + 1):
                header = page.locator(
                    f'[id="{group_id}"] {chapext.GROUP_HEADER_SELECTOR}'
                ).first

                try:
                    await header.scroll_into_view_if_needed()
                    await header.click()

                    await page.wait_for_function(
                        GROUP_LOADED_JS,
                        arg=group_id,
                        timeout=GROUP_LOAD_TIMEOUT_MS,
                    )

                    loaded = True
                    break

                except PlaywrightTimeoutError:
                    log.debug(
                        "Group %s not loaded after click %d/%d",
                        group_id, click, GROUP_CLICK_ATTEMPTS,
                    )

            if loaded:
                expanded += 1
                log.debug("Expanded chapter group %s", group_id)
                await asyncio.sleep(GROUP_DELAY_SECONDS)
            else:
                failed.append(group_id)

    return expanded, failed


async def short_groups(page) -> list[str]:
    """
    Loaded groups that hold fewer chapter rows than their id promises
    (group_10_20 -> 11 rows). Empty list means every group is complete.
    """

    problems = []

    counts = await page.evaluate(
        GROUP_ROW_COUNTS_JS,
        [chapext.GROUP_ROW_SELECTOR, chapext.GROUP_CONTENT_ROW_SELECTOR],
    )

    for item in counts:
        match = chapext.GROUP_ID_PATTERN.match(item["id"])

        if not match:
            continue

        expected = int(match.group(2)) - int(match.group(1)) + 1

        if item["rows"] < expected:
            problems.append(
                f"{item['id']} has {item['rows']} of {expected} chapters"
            )

    return problems


async def fetch_chapter_list_html(browser, url: str) -> FetchResult:
    """
    Open the chapter-list page in a fresh Chromium context, wait for its
    JavaScript-generated chapter links, and return the rendered HTML.

    Completion is decided by chapext's own extraction rules, not by sleeping:
    the page is re-read until chapter links (or collapsed chapter groups)
    exist, every collapsed group is clicked open so its chapters load, then
    the page is scrolled to the bottom (in case the list loads lazily) until
    the number of extracted links stops changing. A list with unexpanded or
    incomplete groups is never reported as SUCCESS.

    SUCCESS  - links found and the count was stable.
    PARTIAL  - links found, but the count was still changing at the end.
    FAILED   - navigation/HTTP problem, or no chapter links ever appeared.
    """

    context = None
    page = None
    crashed = {"flag": False}

    try:
        context = await browser.new_context(
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
        )

        page = await context.new_page()
        page.set_default_timeout(20_000)

        page.on(
            "pageerror",
            lambda error: log.debug("[Browser JavaScript error] %s", error),
        )

        def on_crash(_page) -> None:
            crashed["flag"] = True

        page.on("crash", on_crash)

        log.debug("Opening chapter-list page: %s", url)

        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=LIST_NAVIGATION_TIMEOUT_MS,
        )

        status = response.status if response is not None else None

        problem = _http_problem(status, url, page.url)

        if problem is not None:
            return problem

        final_host = (urlparse(page.url).hostname or "").lower()

        if final_host not in READAWRITE_HOSTS:
            return FetchResult(
                status=FetchStatus.FAILED,
                detail=(
                    "The page redirected to a different website "
                    f"({final_host or 'unknown'}); only ReadAWrite pages "
                    "are supported"
                ),
                http_status=status,
                retryable=False,
            )

        if status is not None and status >= 400:
            log.warning("The chapter-list page returned HTTP %s; "
                        "continuing.", status)

        # Best effort: let the page's own requests settle. Not required.
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=LIST_NETWORK_IDLE_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            log.debug("Network did not go idle; continuing.")

        loop = asyncio.get_running_loop()

        def count_links(html: str) -> int:
            return len(
                chapext.extract_chapter_links(html, base_url=page.url)
            )

        def has_content(html: str) -> bool:
            # Visible chapter rows, or collapsed groups still to be opened.
            return (
                count_links(html) > 0
                or bool(chapext.find_unloaded_groups(html))
            )

        # ---- 1. wait until chapter links / groups exist ------------

        deadline = loop.time() + LIST_FIRST_LINKS_TIMEOUT_S
        html = await page.content()

        while not has_content(html) and loop.time() < deadline:
            await asyncio.sleep(LIST_POLL_SECONDS)
            html = await page.content()

        # ---- 2. open every collapsed chapter group -----------------

        groups_expanded, groups_failed = await expand_collapsed_groups(page)

        html = await page.content()
        count = count_links(html)

        if count == 0 and groups_failed:
            return FetchResult(
                status=FetchStatus.FAILED,
                detail=(
                    f"Could not expand {len(groups_failed)} collapsed "
                    f"chapter group(s) ({', '.join(groups_failed)}) and no "
                    "chapters were visible"
                ),
                html=html,
                http_status=status,
                retryable=True,
            )

        if count == 0:
            problem = _http_problem(None, url, page.url)

            if problem is not None:
                return problem

            return FetchResult(
                status=FetchStatus.FAILED,
                detail=(
                    "No chapter links appeared on the page. The page may "
                    "need a login, its structure may not be supported, or "
                    "its content did not finish loading"
                ),
                html=html,
                http_status=status,
                retryable=True,
            )

        # ---- 3. scroll until the link count stops changing ---------

        stable = 0
        rounds = 0

        while stable < LIST_STABLE_ROUNDS and rounds < LIST_MAX_SCROLL_ROUNDS:
            await page.evaluate(
                "() => window.scrollTo(0, "
                "document.documentElement.scrollHeight)"
            )
            await asyncio.sleep(LIST_POLL_SECONDS)

            html = await page.content()
            new_count = count_links(html)

            if new_count == count:
                stable += 1
            else:
                stable = 0
                count = new_count

            rounds += 1

        log.debug("Chapter list: %d link(s) after %d scroll round(s), "
                  "stable=%s", count, rounds, stable >= LIST_STABLE_ROUNDS)

        # ---- 4. verify the list is complete ------------------------

        late_groups, late_failed = await expand_collapsed_groups(page)
        groups_expanded += late_groups
        groups_failed = list(dict.fromkeys(groups_failed + late_failed))

        if late_groups:
            html = await page.content()
            count = count_links(html)

        leftover = chapext.find_unloaded_groups(html)
        short = await short_groups(page)

        stats = {"groups_expanded": groups_expanded}

        problem_text = None

        if groups_failed or leftover:
            names = chapext.describe_unloaded_groups(leftover) or ", ".join(
                groups_failed
            )
            problem_text = (
                f"Could not expand {max(len(leftover), len(groups_failed))} "
                f"collapsed chapter group(s) ({names}); the list would be "
                "missing chapters"
            )
        elif short:
            problem_text = (
                "A chapter group loaded fewer chapters than expected ("
                + "; ".join(short) + ")"
            )
        elif stable < LIST_STABLE_ROUNDS:
            problem_text = (
                f"The chapter list was still growing after "
                f"{rounds} scroll rounds ({count} links so far)"
            )

        if problem_text:
            return FetchResult(
                status=FetchStatus.PARTIAL,
                detail=problem_text,
                html=html,
                layout="ChapterList",
                http_status=status,
                expected_items=count,
                rendered_items=count,
                stats=stats,
            )

        return FetchResult(
            status=FetchStatus.SUCCESS,
            html=html,
            layout="ChapterList",
            http_status=status,
            expected_items=count,
            rendered_items=count,
            stats=stats,
        )

    except PlaywrightTimeoutError as error:
        return FetchResult(
            status=FetchStatus.FAILED,
            detail=f"Timeout: {str(error).splitlines()[0]}",
            browser_crashed=crashed["flag"],
        )

    except Exception as error:
        first_line = (str(error).splitlines() or [""])[0]

        return FetchResult(
            status=FetchStatus.FAILED,
            detail=f"{type(error).__name__}: {first_line}",
            browser_crashed=(
                crashed["flag"] or rwfetch._is_crash_error(error)
            ),
        )

    finally:
        for closable in (page, context):
            if closable is None:
                continue

            try:
                await asyncio.wait_for(closable.close(), timeout=10)
            except Exception as error:
                log.debug("Ignoring close error: %r", error)


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
    base_dir: Path,
    default_name: str,
    list_hash: str,
    output_name: Optional[str] = None,
) -> tuple[Path, Optional[dict]]:
    """
    Choose the output directory for this exact chapter list.

    output_name is the optional -o/--output value. A plain name is created
    inside base_dir; an absolute path is used as given. When it is not
    supplied, default_name is used.

    1. base folder                - used when new/empty, or when its manifest
                                    has the same chapter-list fingerprint.
    2. "<base>_<hash8>"           - used when the base folder belongs to
                                    a different chapter list.

    The same chapter list therefore always maps to the same directory, and
    two different lists never share one.
    """

    base_name = output_name or default_name
    base = base_dir / base_name
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
        f"Could not choose a safe output directory under {base_dir}."
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
    def __init__(self, source: str,
                 output_name: Optional[str] = None) -> None:
        self.output_name = output_name
        self.list_url: Optional[str] = None
        self.input_path: Optional[Path] = None

        if is_http_url(source):
            self.list_url = validate_list_url(source)
            self.source_label = self.list_url
            self.base_dir = Path.cwd()
            self.default_folder = folder_name_for_url(self.list_url)
        else:
            path = Path(source).expanduser()

            if not path.exists():
                raise InputError(
                    f"{source!r} is neither an http(s) URL nor an existing "
                    "file. Pass the chapter-list page URL, e.g. "
                    "https://www.readawrite.com/a/ARTICLE_ID"
                )

            self.input_path = path.resolve()
            self.source_label = self.input_path.name
            self.base_dir = self.input_path.parent
            self.default_folder = f"{self.input_path.stem}_output"

        self.urls: list[str] = []
        self.list_hash = ""
        self.output_dir: Path = self.base_dir
        self.state: Optional[State] = None
        self.session = BrowserSession()
        self.stop = StopFlag()

        self.skipped = 0
        self.succeeded_now = 0
        self.attempted = 0
        self.interrupted = False
        self.aborted_reason: Optional[str] = None
        self.access_denials_in_a_row = 0

    # ---------------- setup --------------------------------------

    def load_from_file(self) -> None:
        """Local-file input: read and validate the chapter URLs."""

        assert self.input_path is not None
        self.urls = load_chapter_urls(self.input_path)

    async def ensure_browser(self) -> None:
        if self.session.alive:
            return

        say("Starting Chromium...")

        try:
            await self.session.start()
        except Exception as error:
            raise BrowserUnavailable(
                f"Could not launch Chromium: {error}\n"
                "If Chromium is not installed, run:\n"
                "    python -m playwright install chromium"
            )

    async def load_from_url(self) -> None:
        """
        URL input: open the chapter-list page in the shared Chromium,
        wait for its chapter links, and extract them with chapext.
        Retries temporary problems; raises InputError when it cannot.
        """

        assert self.list_url is not None

        await self.ensure_browser()

        say(f"Retrieving chapter list: {self.list_url}")

        last_error = "Unknown error"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                delay = RETRY_DELAYS_SECONDS[
                    min(attempt - 2, len(RETRY_DELAYS_SECONDS) - 1)
                ]

                say(f"Retrying in {delay}s "
                    f"(attempt {attempt}/{MAX_ATTEMPTS})...")

                if not await self.interruptible_sleep(delay):
                    self.interrupted = True
                    return

            if not self.session.alive:
                await self.session.restart()

            try:
                result = await fetch_chapter_list_html(
                    self.session.browser, self.list_url
                )
            except Exception as error:
                log.debug("Unexpected chapter-list exception",
                          exc_info=True)
                result = FetchResult(
                    status=FetchStatus.FAILED,
                    detail=f"{type(error).__name__}: {error}",
                    browser_crashed=True,
                )

            log.debug(
                "[chapter list] [attempt %d/%d] %s status=%s links=%s "
                "http=%s detail=%s",
                attempt, MAX_ATTEMPTS, self.list_url,
                result.status.value, result.rendered_items,
                result.http_status, result.detail,
            )

            if result.browser_crashed or not self.session.alive:
                say("Chromium crashed or disconnected; restarting it...")
                await self.session.restart()

            if result.status is FetchStatus.SUCCESS:
                try:
                    self.urls = urls_from_html(
                        result.html or "", base_url=self.list_url
                    )

                    opened = result.stats.get("groups_expanded", 0)

                    if opened:
                        say(f"Expanded {opened} collapsed chapter "
                            "group(s).")

                    return
                except InputError as error:
                    last_error = str(error)
            else:
                last_error = result.detail or "Chapter list not loaded"

                if not result.retryable:
                    say(f"Chapter-list retrieval failed: {last_error}")
                    break

            say(f"Chapter-list retrieval attempt {attempt} failed: "
                f"{last_error}")

        raise InputError(
            f"Could not retrieve the chapter list from {self.list_url}: "
            f"{last_error}"
        )

    def announce(self) -> None:
        say(f"Output: {self.output_dir.name}/")
        say(f"Found {len(self.urls)} chapters.")
        say("")

        log.debug("Source: %s", self.source_label)
        log.debug("Chapter list SHA-256: %s", self.list_hash)

    def prepare(self) -> None:
        """Once self.urls is known: choose the output folder, set up the
        log file and load/create the manifest."""

        self.list_hash = fingerprint(self.urls)

        self.output_dir, existing = resolve_output_dir(
            self.base_dir, self.default_folder,
            self.list_hash, self.output_name,
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
            self.state.data["source_file"] = self.source_label
            self.state.data["total_chapters"] = len(self.urls)

            for number, url in enumerate(self.urls, start=1):
                self.state.chapters.setdefault(
                    str(number),
                    {"url": url, "status": "pending",
                     "output": None, "attempts": 0},
                )
        else:
            self.state = State.new(
                state_path, self.source_label, self.list_hash, self.urls
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
        """Entry point: (URL mode) retrieve the chapter list, then process
        every chapter. Chromium is always closed on the way out."""

        try:
            if self.list_url is not None:
                await self.load_from_url()

                if self.interrupted:
                    return

                self.prepare()
                self.announce()

            await self.process()

        finally:
            await self.session.close()

    async def process(self) -> None:
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
        await self.ensure_browser()

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
            if self.interrupted:
                say("")
                say("Interrupted before the chapter list was retrieved. "
                    "Nothing was processed.")

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

def bootstrap_logging() -> None:
    """
    Console output plus an in-memory buffer. In URL mode the chapter list is
    retrieved before the output folder (and so the log file) exists; the
    buffer is written to the log file as soon as it does.
    """

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))

    buffer = logging.handlers.MemoryHandler(
        capacity=100_000, flushLevel=logging.CRITICAL + 10
    )

    root.addHandler(console)
    root.addHandler(buffer)

    logging.getLogger("asyncio").setLevel(logging.WARNING)


def setup_logging(log_path: Path) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    buffered = [
        handler for handler in root.handlers
        if isinstance(handler, logging.handlers.MemoryHandler)
    ]

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

    # Write everything logged before the log file existed.
    for handler in buffered:
        handler.setTarget(file_handler)
        handler.flush()
        handler.close()

    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ============================================================
# ENTRY POINT
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch and extract every chapter listed on a ReadAWrite "
            "chapter-list page."
        )
    )

    parser.add_argument(
        "source",
        metavar="CHAPTER_LIST_URL",
        help=(
            "URL of the online chapter-list page, e.g. "
            "https://www.readawrite.com/a/ARTICLE_ID "
            "(a locally saved chapter-list HTML file also works)."
        ),
    )

    parser.add_argument(
        "-o",
        "--output",
        metavar="OUTPUT_FOLDER_NAME",
        default=None,
        help=(
            "Optional name for the output folder (created in the "
            "current directory; beside the file for a local HTML input). "
            "Default: <page name>_output."
        ),
    )

    parser.add_argument(
        "--stop-file",
        metavar="PATH",
        default=None,
        help=(
            "Optional: stop gracefully (after the current chapter) as soon "
            "as this file exists. Used by the web backend; not needed for "
            "normal command-line use."
        ),
    )

    args = parser.parse_args()

    if args.output is not None and not args.output.strip():
        parser.error("-o/--output must not be empty")

    # Until the output directory exists, log to the console and a buffer.
    bootstrap_logging()

    print(BANNER)
    print("ReadAWrite Automated Chapter Pipeline")
    print(BANNER)
    print()

    try:
        pipeline = Pipeline(
            args.source, args.output.strip() if args.output else None
        )

        if pipeline.list_url is not None:
            print(f"Chapter-list URL: {pipeline.list_url}")
            print()
        else:
            # Local-file input: everything can be validated before Chromium.
            print(f"Input: {pipeline.source_label}")
            print("Extracting chapter URLs...")
            pipeline.load_from_file()
            pipeline.prepare()
            pipeline.announce()

    except (InputError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if args.stop_file:
        pipeline.stop.stop_file = Path(args.stop_file)

    pipeline.stop.install()

    try:
        asyncio.run(pipeline.run())

    except KeyboardInterrupt:
        pipeline.interrupted = True

    except InputError as error:
        # URL mode: the chapter list could not be retrieved or parsed.
        print(f"Error: {error}", file=sys.stderr)
        return 1

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
