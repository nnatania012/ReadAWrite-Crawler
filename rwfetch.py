"""
rwfetch.py

Open ReadAWrite chapters in Chromium, allow the site's own JavaScript to
load and decrypt each chapter, expand every chat bubble (Chat chapters) or
wait for the chapter text (Lec chapters), and return / save the rendered DOM
as HTML suitable for rwext.py.

This module works in two ways:

1. As a library (used by pipeline.py):

       playwright, browser = await open_browser()
       result = await fetch_chapter(browser, url)
       if result.status is FetchStatus.SUCCESS:
           html = result.html
       await close_browser(playwright, browser)

2. As the original stand-alone command-line tool:

       python rwfetch.py
       python rwfetch.py "https://www.readawrite.com/c/CHAPTER_GUID"
       python rwfetch.py "https://www.readawrite.com/c/CHAPTER_GUID" -o chapter.html
       python rwfetch.py "https://www.readawrite.com/c/CHAPTER_GUID" --headed

Install:
    pip install playwright
    python -m playwright install chromium

The fetcher never tries to log in, solve a CAPTCHA, or otherwise get around
access restrictions. If the site answers with HTTP 401/403 or redirects to a
login page, the result is FAILED with auth_required=True.
"""

import argparse
import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


log = logging.getLogger("rwfetch")


TEST_CHAPTER_URL = (
    "https://www.readawrite.com/c/"
    "35efe0f861c5dd138392fda8cd2faa94"
)

# Timeouts (milliseconds). Same values as the original script.
NAVIGATION_TIMEOUT_MS = 60_000
FIRST_LAYOUT_WAIT_MS = 15_000
LOADER_WAIT_MS = 90_000
EXPAND_WAIT_MS = 60_000

# Lec chapters: how long the highlighted-text count must stay unchanged
# before the chapter is considered fully rendered.
LEC_POLL_SECONDS = 1.0
LEC_STABLE_POLLS = 2
LEC_MAX_WAIT_SECONDS = 45.0

# Substrings of Playwright errors that mean the browser/page itself died.
CRASH_MARKERS = (
    "crash",
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser closed",
    "has been closed",
    "connection closed",
    "browser disconnected",
)


# ============================================================
# RESULT TYPES
# ============================================================

class FetchStatus(str, Enum):
    """Outcome of fetching one chapter."""

    SUCCESS = "SUCCESS"   # all expected content was rendered
    PARTIAL = "PARTIAL"   # HTML captured, but completeness unconfirmed
    FAILED = "FAILED"     # nothing usable was captured


@dataclass
class FetchResult:
    status: FetchStatus
    html: Optional[str] = None
    detail: str = ""
    layout: Optional[str] = None        # "Chat", "Lec" or None (unknown)
    http_status: Optional[int] = None
    expected_items: Optional[int] = None
    rendered_items: Optional[int] = None
    error_code: Optional[int] = None
    retryable: bool = True
    browser_crashed: bool = False
    auth_required: bool = False
    stats: dict = field(default_factory=dict)


# ============================================================
# BROWSER LIFECYCLE
# ============================================================

async def open_browser(headed: bool = False) -> tuple[Playwright, Browser]:
    """Start Playwright and launch one Chromium instance."""

    playwright = await async_playwright().start()

    try:
        browser = await playwright.chromium.launch(headless=not headed)
    except Exception:
        await playwright.stop()
        raise

    return playwright, browser


async def close_browser(
    playwright: Optional[Playwright],
    browser: Optional[Browser],
) -> None:
    """Close Chromium and stop Playwright, ignoring errors."""

    if browser is not None:
        try:
            await asyncio.wait_for(browser.close(), timeout=15)
        except Exception as error:
            log.debug("Ignoring error while closing browser: %r", error)

    if playwright is not None:
        try:
            await asyncio.wait_for(playwright.stop(), timeout=15)
        except Exception as error:
            log.debug("Ignoring error while stopping Playwright: %r", error)


# ============================================================
# PAGE HELPERS
# ============================================================

# Returns "Chat" once the chapter data has been decrypted (window.allnum is
# set), "Lec" once highlighted chapter text exists, otherwise false.
DETECT_LAYOUT_JS = """() => {
    if (typeof window.allnum !== 'undefined' &&
        Number(window.allnum) > 0) {
        return 'Chat';
    }
    if (document.querySelectorAll(
            '#chapterContents .highlight.cursorPointer').length > 0) {
        return 'Lec';
    }
    return false;
}"""

# Same readiness condition as the original script.
CHAT_READY_JS = """() =>
    typeof window.allnum !== 'undefined' &&
    Number(window.allnum) > 0 &&
    typeof window.openAllBubble === 'function' &&
    (typeof window.errorcode === 'undefined' ||
     Number(window.errorcode) === 0)
"""

CHAT_COUNT_JS = """expected =>
    document.querySelectorAll(
        '#chatStoryArea .chatStoryContainer'
    ).length >= expected
"""

LEC_COUNT_JS = (
    "() => document.querySelectorAll("
    "'#chapterContents .highlight.cursorPointer').length"
)

STATS_JS = """() => ({
    title: document.title,

    chat_items_expected:
        typeof window.allnum === 'undefined'
            ? null : Number(window.allnum),

    chat_containers: document.querySelectorAll(
        '#chatStoryArea .chatStoryContainer'
    ).length,

    chat_names: document.querySelectorAll(
        '#chatStoryArea .chatStoryName'
    ).length,

    chat_texts: document.querySelectorAll(
        '#chatStoryArea .chatStoryRounded.chatStoryText'
    ).length,

    chat_images: document.querySelectorAll(
        '#chatStoryArea img.chatStoryRounded.chatStoryImage'
    ).length,

    social_posts: document.querySelectorAll(
        '#chatStoryArea .chatStoryRounded.chatStorySocial'
    ).length,

    lec_highlights: document.querySelectorAll(
        '#chapterContents .highlight.cursorPointer'
    ).length,

    error_code:
        typeof window.errorcode === 'undefined'
            ? null : Number(window.errorcode)
})"""


async def _detect_layout(page: Page, timeout_ms: int) -> Optional[str]:
    """Wait until Chat or Lec content appears. Returns None on timeout."""

    try:
        handle = await page.wait_for_function(
            DETECT_LAYOUT_JS,
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        return None

    value = await handle.json_value()
    return value if value in ("Chat", "Lec") else None


async def _wait_for_stable_lec(page: Page) -> tuple[bool, int]:
    """
    Wait until the number of highlighted Lec spans stops changing.

    Returns (stable, final_count).
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + LEC_MAX_WAIT_SECONDS

    previous = -1
    stable_polls = 0
    count = 0

    while loop.time() < deadline:
        count = int(await page.evaluate(LEC_COUNT_JS))

        if count > 0 and count == previous:
            stable_polls += 1

            if stable_polls >= LEC_STABLE_POLLS:
                return True, count
        else:
            stable_polls = 0

        previous = count
        await asyncio.sleep(LEC_POLL_SECONDS)

    return False, count


def _looks_like_login(requested_url: str, final_url: str) -> bool:
    """True when the browser ended up on a login page."""

    pattern = re.compile(r"/(log-?in|sign-?in)(/|$)", re.IGNORECASE)

    requested_path = urlparse(requested_url).path
    final_path = urlparse(final_url).path

    return bool(
        pattern.search(final_path)
        and not pattern.search(requested_path)
    )


def _is_crash_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in CRASH_MARKERS)


# ============================================================
# MAIN FETCH FUNCTION
# ============================================================

async def fetch_chapter(browser: Browser, url: str) -> FetchResult:
    """
    Load one chapter in a fresh browser context and return its rendered DOM.

    A new context (and page) is created for every call and always closed
    before returning, so no page state leaks into the next chapter.
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
            lambda error: log.debug(
                "[Browser JavaScript error] %s", error
            ),
        )

        def on_crash(_page) -> None:
            crashed["flag"] = True

        page.on("crash", on_crash)

        log.debug("Opening: %s", url)

        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=NAVIGATION_TIMEOUT_MS,
        )

        http_status = response.status if response is not None else None

        if http_status is not None:
            log.debug("HTTP status: %s", http_status)

        # ---- Access / availability checks (never bypassed) --------

        if http_status in (401, 403):
            return FetchResult(
                status=FetchStatus.FAILED,
                detail=(
                    f"HTTP {http_status}: the site is restricting access "
                    "(login or permission required)"
                ),
                http_status=http_status,
                retryable=False,
                auth_required=True,
            )

        if _looks_like_login(url, page.url):
            return FetchResult(
                status=FetchStatus.FAILED,
                detail=(
                    "Redirected to a login page: this chapter requires "
                    "a signed-in account"
                ),
                http_status=http_status,
                retryable=False,
                auth_required=True,
            )

        if http_status in (404, 410):
            return FetchResult(
                status=FetchStatus.FAILED,
                detail=f"HTTP {http_status}: chapter not found",
                http_status=http_status,
                retryable=False,
            )

        if http_status is not None and (
            http_status == 429 or http_status >= 500
        ):
            return FetchResult(
                status=FetchStatus.FAILED,
                detail=f"HTTP {http_status} from the server",
                http_status=http_status,
                retryable=True,
            )

        if http_status is not None and http_status >= 400:
            log.warning(
                "The chapter page returned HTTP %s; continuing.",
                http_status,
            )

        # ---- Wait for ReadAWrite's normal chapter loader ----------

        layout = await _detect_layout(page, FIRST_LAYOUT_WAIT_MS)

        if layout is None:
            has_loader = await page.evaluate(
                "() => typeof window.reloadContent === 'function'"
            )

            if has_loader:
                log.debug(
                    "Starting ReadAWrite's normal chapter loader..."
                )

                await page.evaluate("() => window.reloadContent()")

            layout = await _detect_layout(page, LOADER_WAIT_MS)

        status = FetchStatus.PARTIAL
        detail = ""
        expected_items: Optional[int] = None
        rendered_items: Optional[int] = None

        if layout is None:
            detail = (
                "Neither Chat bubbles nor Lec chapter text appeared "
                "before the timeout"
            )

        elif layout == "Chat":
            try:
                await page.wait_for_function(
                    CHAT_READY_JS,
                    timeout=LOADER_WAIT_MS,
                )

                expected_items = int(
                    await page.evaluate("() => Number(window.allnum)")
                )

                rendered_items = await page.locator(
                    "#chatStoryArea .chatStoryContainer"
                ).count()

                log.debug(
                    "Chapter data loaded: %s chat item(s); "
                    "currently rendered: %s.",
                    expected_items,
                    rendered_items,
                )

                if rendered_items < expected_items:
                    log.debug("Expanding all chat bubbles...")

                    await page.evaluate("() => window.openAllBubble()")

                await page.wait_for_function(
                    CHAT_COUNT_JS,
                    arg=expected_items,
                    timeout=EXPAND_WAIT_MS,
                )

                rendered_items = await page.locator(
                    "#chatStoryArea .chatStoryContainer"
                ).count()

                status = FetchStatus.SUCCESS

            except PlaywrightTimeoutError:
                detail = (
                    "Could not confirm that every chat bubble was "
                    "rendered"
                )

                try:
                    rendered_items = await page.locator(
                        "#chatStoryArea .chatStoryContainer"
                    ).count()
                except Exception:
                    pass

        else:  # layout == "Lec"
            stable, count = await _wait_for_stable_lec(page)

            rendered_items = count
            expected_items = count if stable else None

            if stable:
                status = FetchStatus.SUCCESS
            else:
                detail = (
                    "Lec chapter text was still changing when the "
                    "wait ended"
                )

        # ---- Capture whatever was rendered ------------------------

        rendered_html = await page.content()
        stats = await page.evaluate(STATS_JS)

        return FetchResult(
            status=status,
            html=rendered_html,
            detail=detail,
            layout=layout,
            http_status=http_status,
            expected_items=expected_items,
            rendered_items=rendered_items,
            error_code=stats.get("error_code"),
            retryable=True,
            stats=stats,
        )

    except PlaywrightTimeoutError as error:
        return FetchResult(
            status=FetchStatus.FAILED,
            detail=f"Timeout: {str(error).splitlines()[0]}",
            browser_crashed=crashed["flag"],
        )

    except Exception as error:
        is_crash = crashed["flag"] or _is_crash_error(error)

        first_line = (str(error).splitlines() or [""])[0]

        return FetchResult(
            status=FetchStatus.FAILED,
            detail=f"{type(error).__name__}: {first_line}",
            browser_crashed=is_crash,
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
# STAND-ALONE COMMAND-LINE TOOL
# ============================================================

async def save_rendered_chapter(
    url: str,
    output: str,
    headed: bool = False,
) -> FetchResult:
    """Fetch one chapter and save its rendered DOM (CLI helper)."""

    playwright, browser = await open_browser(headed=headed)

    try:
        print(f"Opening: {url}")

        result = await fetch_chapter(browser, url)

    finally:
        await close_browser(playwright, browser)

    if result.html is not None:
        output_path = Path(output)
        output_path.write_text(result.html, encoding="utf-8")

        stats = result.stats

        print(f"Saved rendered HTML: {output_path.resolve()}")
        print(f"Page title: {stats.get('title')}")
        print(f"Layout: {result.layout or 'unknown'}")
        print(f"Chat items expected: {stats.get('chat_items_expected')}")
        print(f"Chat containers saved: {stats.get('chat_containers')}")
        print(f"Chat names: {stats.get('chat_names')}")
        print(f"Chat text bubbles: {stats.get('chat_texts')}")
        print(f"Chat images: {stats.get('chat_images')}")
        print(f"Social posts: {stats.get('social_posts')}")
        print(f"Lec highlighted spans: {stats.get('lec_highlights')}")
        print(
            f"ReadAWrite content error code: "
            f"{stats.get('error_code')}"
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Load a ReadAWrite chapter in Chromium "
            "and save its rendered HTML."
        )
    )

    parser.add_argument(
        "url",
        nargs="?",
        default=TEST_CHAPTER_URL,
        help=(
            "ReadAWrite chapter URL "
            "(defaults to the supplied test chapter)."
        ),
    )

    parser.add_argument(
        "-o",
        "--output",
        default="chapter1.html",
        help="Output HTML filename (default: chapter1.html).",
    )

    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the Chromium window instead of running headlessly.",
    )

    args = parser.parse_args()

    if not args.url.startswith("https://www.readawrite.com/c/"):
        parser.error(
            "Please provide a ReadAWrite chapter URL under /c/."
        )

    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    try:
        result = asyncio.run(
            save_rendered_chapter(args.url, args.output, args.headed)
        )

    except Exception as error:
        print(f"Error: {type(error).__name__}: {error}")
        return 1

    if result.status is FetchStatus.SUCCESS:
        print("Success: all chapter content was rendered and saved.")
        return 0

    if result.status is FetchStatus.PARTIAL:
        print(
            "Partial result: an HTML file was saved, but the script "
            f"could not verify the chapter was fully loaded "
            f"({result.detail})."
        )
        return 2

    print(f"Failed: {result.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
