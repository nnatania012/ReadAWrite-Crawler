
"""
rwchapters.py

Extract every chapter URL from a ReadAWrite chapter-list HTML page.

Supported chapter-row structures:
1. .chatChapterTableRow.notChapterStatus3
2. .articleTableContent .table-content-row

The script extracts URLs from onclick attributes containing:
    window.location.href = 'https://www.readawrite.com/c/...'

Usage:
    python rwchapters.py chapters.html

    python rwchapters.py chapters.html -o chapter_links.txt

    python rwchapters.py "https://www.readawrite.com/a/ARTICLE_ID"

Dependencies:
    pip install beautifulsoup4 requests
"""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


CHAPTER_ROW_SELECTORS = [
    ".chatChapterTableRow.notChapterStatus3",
    ".articleTableContent .table-content-row",
]

CHAPTER_URL_PATTERN = re.compile(
    r"""window\s*\.\s*location\s*\.\s*href\s*=\s*(['"])(.*?)\1""",
    re.IGNORECASE,
)


def load_html(source):
    """Load HTML from a local file or URL."""

    if source.lower().startswith(("http://", "https://")):
        response = requests.get(
            source,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        return response.text

    path = Path(source)

    if not path.is_file():
        raise FileNotFoundError(f"Input file not found: {source}")

    raw = path.read_bytes()

    # Try common encodings used by ReadAWrite HTML files.
    for encoding in ("utf-8-sig", "utf-8", "cp874", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise UnicodeError(f"Unable to decode HTML file: {source}")


def extract_url_from_onclick(onclick):
    """Extract the URL assigned to window.location.href."""

    if not onclick:
        return None

    match = CHAPTER_URL_PATTERN.search(onclick)

    if not match:
        return None

    url = match.group(2).strip()

    # Only accept ReadAWrite chapter URLs.
    parsed = urlparse(url)

    if parsed.netloc.lower() not in {
        "www.readawrite.com",
        "readawrite.com",
    }:
        return None

    if not parsed.path.startswith("/c/"):
        return None

    return url


def find_chapter_rows(soup):
    """
    Find chapter rows using the supported DOM structures.

    The first selector is the requested structure.
    The second selector supports the uploaded test1.html.
    """

    rows = []
    seen_rows = set()

    for selector in CHAPTER_ROW_SELECTORS:
        for element in soup.select(selector):
            # Avoid processing the same element twice.
            identity = id(element)

            if identity in seen_rows:
                continue

            seen_rows.add(identity)
            rows.append(element)

    return rows


def extract_chapter_links(html):
    """Extract all chapter links in document order, without duplicates."""

    soup = BeautifulSoup(html, "html.parser")

    rows = find_chapter_rows(soup)

    links = []
    seen_links = set()

    for row in rows:
        onclick = row.get("onclick", "")
        url = extract_url_from_onclick(onclick)

        if not url or url in seen_links:
            continue

        seen_links.add(url)
        links.append(url)

    return links


def save_links(links, output_path):
    """Save one chapter URL per line."""

    Path(output_path).write_text(
        "\n".join(links) + ("\n" if links else ""),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Extract ReadAWrite chapter URLs from HTML."
    )

    parser.add_argument(
        "source",
        help="Local HTML file or ReadAWrite URL.",
    )

    parser.add_argument(
        "-o",
        "--output",
        default="chapter_links.txt",
        help="Output text file (default: chapter_links.txt).",
    )

    args = parser.parse_args()

    try:
        html = load_html(args.source)
        links = extract_chapter_links(html)

        if not links:
            print("No chapter links were found.", file=sys.stderr)
            print(
                "Check whether the HTML contains a supported chapter-list "
                "structure.",
                file=sys.stderr,
            )
            sys.exit(1)

        save_links(links, args.output)

        print(f"Found {len(links)} chapter link(s).")
        print(f"Saved to: {args.output}")

        for index, link in enumerate(links, start=1):
            print(f"{index}. {link}")

    except (requests.RequestException, OSError, UnicodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
