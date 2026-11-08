"""
ReadAWrite HTML extractor with automatic Chat/Lec detection.

Usage:
    python rwext.py input.html
    python rwext.py input.html --by-paragraph
    python rwext.py input.html --by-paragraph --blank-between
    python rwext.py input.html --pair
    python rwext.py input.html --keep-blank
    python rwext.py input.html --pair --keep-blank

    # Optional manual mode overrides:
    python rwext.py input.html --Lec
    python rwext.py input.html --Chat

    # Custom output:
    python rwext.py input.html -o result.txt
    python rwext.py https://www.readawrite.com/c/... -o result.txt

Options:
    --Lec             Force Lec extraction (automatic detection is default)
    --Chat            Force Chat extraction (automatic detection is default)

    Lec-only:
    --by-paragraph    Join spans from the same <p> into one line
    --blank-between   Add an empty line between paragraphs
                      (requires --by-paragraph)

    Chat-only:
    --pair            Join each name with the following ordinary text message
    --keep-blank      Keep blank/whitespace-only ordinary chat bubbles

    Shared:
    -o, --output      Choose output file

Install dependencies:
    pip install beautifulsoup4 requests
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup


# ============================================================
# SHARED FUNCTIONS
# ============================================================

def decode_bytes(raw: bytes) -> str:
    """Decode HTML using common encodings."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")

    for enc in ("utf-8-sig", "utf-8", "cp874"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue

    return raw.decode("utf-8", errors="replace")


def load_html(source: str) -> str:
    """Load HTML from a URL or a local file."""
    if source.lower().startswith(("http://", "https://")):
        import requests

        response = requests.get(
            source,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        response.raise_for_status()
        return decode_bytes(response.content)

    return decode_bytes(Path(source).read_bytes())


def get_soup(html_or_soup):
    """Reuse an existing BeautifulSoup document when one is supplied."""
    if isinstance(html_or_soup, BeautifulSoup):
        return html_or_soup

    return BeautifulSoup(html_or_soup, "html.parser")


def write_output(lines: list[str], output: str) -> None:
    """Write extracted entries to a UTF-8 text file."""
    Path(output).write_text("\n".join(lines), encoding="utf-8")
    print(f"Extracted {len(lines)} entries -> {output}")


# ============================================================
# LEC FUNCTIONS
# Extraction logic preserved from the supplied script.
# ============================================================

def is_highlight(tag) -> bool:
    """Check for elements with both highlight and cursorPointer classes."""
    classes = tag.get("class") or []
    return "highlight" in classes and "cursorPointer" in classes


def clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def span_text(tag) -> str:
    # separator="" preserves Thai words split across HTML tags.
    return clean(tag.get_text(""))


def extract_lec(
    html_or_soup,
    by_paragraph: bool = False,
    blank_between: bool = False,
) -> list[str]:

    soup = get_soup(html_or_soup)
    tags = soup.find_all(is_highlight)

    if not by_paragraph:
        return [
            text for text in (span_text(tag) for tag in tags) if text
        ]

    lines = []
    current_parent = None
    buffer = []

    for tag in tags:
        text = span_text(tag)

        if not text:
            continue

        parent = tag.find_parent("p") or tag.parent

        if parent is not current_parent and buffer:
            lines.append(" ".join(buffer))

            if blank_between:
                lines.append("")

            buffer = []

        current_parent = parent
        buffer.append(text)

    if buffer:
        lines.append(" ".join(buffer))

    return lines


# ============================================================
# MODE DETECTION
# ============================================================

class ModeDetectionError(ValueError):
    """Raised when the HTML does not clearly match Chat or Lec structure."""


def detect_mode(html_or_soup) -> str:
    """
    Detect the actual content layout and return either 'Chat' or 'Lec'.

    Detection uses parsed DOM elements rather than searching raw HTML
    strings inside JavaScript, CSS, or unrelated page content.
    """

    soup = get_soup(html_or_soup)

    # --------------------------------------------------------
    # CHAT DETECTION
    # --------------------------------------------------------

    # Prefer the actual chat-story content area.
    chat_area = soup.find(id="chatStoryArea")
    chat_scope = chat_area if chat_area is not None else soup

    chat_bubbles = chat_scope.select(
        ".chatStoryContainer .chatStoryContent .chatStoryBubble, "
        ".chatStoryContent .chatStoryBubble"
    )

    has_chat = False

    for bubble in chat_bubbles:
        if bubble.select_one(
            ".chatStoryName, "
            ".chatStoryRounded.chatStoryText, "
            ".chatStorySocial, "
            "img.chatStoryImage"
        ):
            has_chat = True
            break

    # --------------------------------------------------------
    # LEC DETECTION
    # --------------------------------------------------------

    chapter_content = soup.find(id="chapterContents")

    if chapter_content is not None:
        lec_markers = chapter_content.find_all(is_highlight)
    else:
        lec_markers = soup.select(
            "p.par .highlight.cursorPointer"
        )

    has_lec = bool(lec_markers)

    # --------------------------------------------------------
    # DETECTION RESULT
    # --------------------------------------------------------

    if has_chat and has_lec:
        raise ModeDetectionError(
            "The HTML contains both Chat bubble structures and Lec "
            "highlighted chapter text. Automatic detection is ambiguous. "
            "Use --Chat or --Lec to select the intended extractor manually."
        )

    if has_chat:
        return "Chat"

    if has_lec:
        return "Lec"

    raise ModeDetectionError(
        "Could not recognize this ReadAWrite HTML as Chat or Lec. "
        f"Chat bubble containers found: {len(chat_bubbles)}; "
        f"Lec highlight spans found: {len(lec_markers)}. "
        "The saved HTML may be incomplete or use a different page layout. "
        "You can try --Chat or --Lec as a manual override."
    )


# ============================================================
# CHAT DETECTION HELPERS
# ============================================================

def has_classes(tag, required: set[str]) -> bool:
    """Return True when a tag contains every class in required."""
    classes = set(tag.get("class") or [])
    return required.issubset(classes)


def is_name(tag) -> bool:
    return "chatStoryName" in (tag.get("class") or [])


def is_text(tag) -> bool:
    return has_classes(
        tag,
        {"chatStoryRounded", "chatStoryText"}
    )


def is_chat_image(tag) -> bool:
    """A user-shared chat image, not an icon or social-post image."""

    return (
        getattr(tag, "name", None) == "img"
        and has_classes(
            tag,
            {
                "chatStoryRounded",
                "chatStoryImage",
                "cursorPointer",
            }
        )
    )


def is_icon_image(tag) -> bool:
    """An icon image used with a following padded text div."""

    return (
        getattr(tag, "name", None) == "img"
        and "chatStoryImage" in (tag.get("class") or [])
        and not is_chat_image(tag)
    )


def is_social_post(tag) -> bool:
    """Match readgram/readtwit social cards."""

    return has_classes(
        tag,
        {
            "chatStoryRounded",
            "chatStorySocial",
            "showInlineBlock",
            "cursorPointer",
        }
    )


def has_padding_top(tag) -> bool:
    """Check for a padding-top CSS declaration, regardless of its value."""

    style = tag.get("style") or ""

    for declaration in style.split(";"):
        property_name, separator, _value = declaration.partition(":")

        if separator and property_name.strip().lower() == "padding-top":
            return True

    return False


def find_icon_text_container(img):
    """
    Find the immediately following padded div associated with an icon.

    Uses DOM relationships rather than a fixed padding-top value.
    """

    image_container = img.find_parent("div")

    if image_container is None:
        return None

    following = image_container.find_next_sibling()

    if (
        following is not None
        and following.name == "div"
        and has_padding_top(following)
    ):
        return following

    return None


# ============================================================
# SOCIAL-POST EXTRACTION HELPERS
# ============================================================

def inline_text(tag) -> str:
    """Extract a compact, single-line text value."""

    text = tag.get_text(" ", strip=True)

    return re.sub(r"\s+", " ", text).strip()


def caption_text(tag) -> str:
    """Extract caption text while retaining meaningful HTML line breaks."""

    # Work on a copy so extracting a caption does not mutate the source DOM.
    from copy import copy

    tag_copy = copy(tag)

    for br in tag_copy.find_all("br"):
        br.replace_with("\n")

    raw = tag_copy.get_text("", strip=False).replace("\xa0", " ")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    cleaned_lines = []

    for line in raw.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()

        if line:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def first_descendant_with_classes(container, required: set[str]):
    """Return the first descendant containing all required classes."""

    return container.find(
        lambda tag: (
            getattr(tag, "name", None) is not None
            and has_classes(tag, required)
        )
    )


def extract_social_post(container) -> dict[str, str]:
    """
    Extract the profile, engagement line, and caption from one social card.
    """

    result = {
        "profile": "",
        "engagement": "",
        "caption": "",
    }

    # --------------------------------------------------------
    # A. PROFILE / DISPLAY NAME
    # --------------------------------------------------------

    profile_tag = first_descendant_with_classes(
        container,
        {
            "showInlineBlock",
            "width100Percent",
            "height1Line",
        }
    )

    if profile_tag is not None:
        result["profile"] = inline_text(profile_tag)

    # --------------------------------------------------------
    # B. LEFT ENGAGEMENT COUNT
    # --------------------------------------------------------

    left_tag = first_descendant_with_classes(
        container,
        {
            "showInlineBlock",
            "width50Percent",
            "pull-left",
            "text-left",
            "txtSize12",
        }
    )

    # --------------------------------------------------------
    # C. RIGHT ENGAGEMENT COUNT
    # --------------------------------------------------------

    # Permit txtSize11, txtSize12, or another numeric txtSize class.

    right_tag = container.find(
        lambda tag: (
            getattr(tag, "name", None) is not None
            and has_classes(
                tag,
                {
                    "showInlineBlock",
                    "width50Percent",
                    "pull-left",
                    "text-right",
                }
            )
            and any(
                re.fullmatch(r"txtSize\d+", class_name)
                for class_name in (tag.get("class") or [])
            )
        )
    )

    left_text = inline_text(left_tag) if left_tag is not None else ""
    right_text = inline_text(right_tag) if right_tag is not None else ""

    # Both engagement counts are placed on the same output line.

    result["engagement"] = " | ".join(
        value
        for value in (left_text, right_text)
        if value
    )

    # --------------------------------------------------------
    # D. POST CAPTION
    # --------------------------------------------------------

    caption_candidates = container.find_all(
        lambda tag: (
            getattr(tag, "name", None) is not None
            and has_classes(
                tag,
                {
                    "showInlineBlock",
                    "width100Percent",
                    "text-left",
                }
            )
        )
    )

    if caption_candidates:

        # Prefer the caption inside the padded content area.
        padded_candidates = [
            tag
            for tag in caption_candidates
            if "paddingAll10" in (tag.parent.get("class") or [])
        ]

        caption_tag = (
            padded_candidates[-1]
            if padded_candidates
            else caption_candidates[-1]
        )

        result["caption"] = caption_text(caption_tag)

    return result


def format_social_post(
    data: dict[str, str],
    omit_profile: bool = False
) -> str:
    """Format one social card as a single grouped output block."""

    fields = []

    if data.get("profile") and not omit_profile:
        fields.append(data["profile"])

    if data.get("engagement"):
        fields.append(data["engagement"])

    if data.get("caption"):
        fields.append(data["caption"])

    return "\n".join(fields)


# ============================================================
# CHAT EXTRACTION
# ============================================================

def collect_chat_items(
    soup,
    keep_blank: bool = False
) -> list[tuple[str, object]]:
    """
    Collect logical chat items in HTML document order.

    Each item is:
        (kind, value)

    Supported kinds:
        name
        text
        image
        icon_text
        social

    Social cards and attachments are atomic items. Their descendants
    are not separately collected as chat messages.
    """

    social_containers = soup.find_all(is_social_post)

    # --------------------------------------------------------
    # IDENTIFY OUTERMOST SOCIAL CONTAINERS
    # --------------------------------------------------------

    social_ids = {
        id(container)
        for container in social_containers
    }

    outer_social_containers = [
        container
        for container in social_containers
        if not any(
            id(parent) in social_ids
            for parent in container.parents
        )
    ]

    # --------------------------------------------------------
    # REGISTER SOCIAL EVENTS
    # --------------------------------------------------------

    special_events = {
        id(container): (
            "social",
            extract_social_post(container)
        )
        for container in outer_social_containers
    }

    # Suppress all descendants of social cards.
    # This prevents profile pictures, post images, captions, and
    # engagement fields from being extracted independently.

    social_descendant_ids = set()

    for container in outer_social_containers:
        social_descendant_ids.update(
            id(descendant)
            for descendant in container.find_all(True)
        )

    special_roots = list(outer_social_containers)

    # --------------------------------------------------------
    # IDENTIFY CHAT IMAGES AND ICON-ASSOCIATED TEXT
    # --------------------------------------------------------

    for img in soup.find_all("img"):

        if id(img) in social_descendant_ids:
            continue

        # Chat image attachments.

        if is_chat_image(img):

            src = (img.get("src") or "").strip()

            if src:
                special_events[id(img)] = (
                    "image",
                    f"[IMAGE] {src}"
                )

                special_roots.append(img)

            continue

        # Icon-associated text.

        if is_icon_image(img):

            text_container = find_icon_text_container(img)

            if (
                text_container is not None
                and id(text_container) not in social_descendant_ids
            ):

                text = text_container.get_text(" ", strip=True)

                text = re.sub(r"[ \t]+", " ", text)

                if text:

                    special_events.setdefault(
                        id(text_container),
                        ("icon_text", text)
                    )

                    special_roots.append(text_container)

    # --------------------------------------------------------
    # SUPPRESS DESCENDANTS OF ATOMIC ITEMS
    # --------------------------------------------------------

    # Keep the roots themselves because they represent the actual
    # position of each logical item in the document.

    special_descendant_ids = set(social_descendant_ids)

    for root in special_roots:

        special_descendant_ids.update(
            id(descendant)
            for descendant in root.find_all(True)
        )

    # --------------------------------------------------------
    # COLLECT ALL ITEMS IN DOCUMENT ORDER
    # --------------------------------------------------------

    items = []

    for tag in soup.find_all(True):

        tag_id = id(tag)

        if tag_id in special_descendant_ids:
            continue

        if tag_id in special_events:

            kind, value = special_events[tag_id]

            items.append((kind, value))

            continue

        # Existing ordinary chat extraction.

        if is_name(tag) or is_text(tag):

            text = tag.get_text(" ", strip=True)

            text = re.sub(r"[ \t]+", " ", text)

            if not text and not keep_blank:
                continue

            items.append(
                ("name" if is_name(tag) else "text", text)
            )

    return items


# ============================================================
# CHAT OUTPUT FORMATTING
# ============================================================

def render_chat_items(
    items: list[tuple[str, object]],
    pair: bool = False
) -> list[str]:
    """Render collected items while preserving ordinary name/message pairing."""

    lines = []

    # --------------------------------------------------------
    # NORMAL OUTPUT
    # --------------------------------------------------------

    if not pair:

        previous_name = None

        for kind, value in items:

            if kind == "name":

                lines.append(value)
                previous_name = value

            elif kind == "social":

                # Avoid printing an identical author name twice
                # when the chat bubble name matches the social profile.

                omit_profile = bool(
                    previous_name
                    and value.get("profile") == previous_name
                )

                block = format_social_post(
                    value,
                    omit_profile=omit_profile
                )

                if block:
                    lines.append(block)

                previous_name = None

            else:

                lines.append(value)
                previous_name = None

        return lines

    # --------------------------------------------------------
    # PAIRED OUTPUT
    # --------------------------------------------------------

    pending_name = None

    for kind, value in items:

        if kind == "name":

            # A later name replaces an earlier pending name,
            # matching the original implementation.

            pending_name = value

            continue

        if kind == "text":

            lines.append(
                f"{pending_name}: {value}"
                if pending_name
                else value
            )

            pending_name = None

            continue

        # Images, icon-associated text, and social posts are
        # standalone logical items.
        #
        # A pending name must not accidentally attach to them.

        omit_profile = False

        if pending_name is not None:

            lines.append(pending_name)

            if (
                kind == "social"
                and value.get("profile") == pending_name
            ):
                omit_profile = True

            pending_name = None

        if kind == "social":

            block = format_social_post(
                value,
                omit_profile=omit_profile
            )

            if block:
                lines.append(block)

        else:

            lines.append(value)

    # Preserve original --pair behavior:
    # a trailing name without a following ordinary text bubble
    # is not emitted in paired mode.

    return lines


def extract_chat(
    html_or_soup,
    keep_blank: bool = False,
    pair: bool = False
) -> list[str]:

    soup = get_soup(html_or_soup)

    items = collect_chat_items(
        soup,
        keep_blank=keep_blank
    )

    return render_chat_items(
        items,
        pair=pair
    )


# ============================================================
# ARGUMENT PARSING AND PROGRAM ENTRY POINT
# ============================================================

def validate_mode_options(parser, mode: str, args) -> None:
    """Reject options that do not belong to the selected extraction mode."""

    if mode == "Lec":

        if args.pair:
            parser.error(
                "--pair can only be used with Chat content"
            )

        if args.keep_blank:
            parser.error(
                "--keep-blank can only be used with Chat content"
            )

    elif mode == "Chat":

        if args.by_paragraph:
            parser.error(
                "--by-paragraph can only be used with Lec content"
            )

        if args.blank_between:
            parser.error(
                "--blank-between can only be used with Lec content"
            )

    if args.blank_between and not args.by_paragraph:

        parser.error(
            "--blank-between requires --by-paragraph"
        )


def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Extract text from ReadAWrite HTML. Chat/Lec format is "
            "detected automatically unless a manual override is supplied."
        )
    )

    parser.add_argument(
        "source",
        nargs="?",
        default=None,
        help="HTML file path or URL (default depends on selected mode)",
    )

    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output text file",
    )

    # --------------------------------------------------------
    # OPTIONAL MANUAL MODE OVERRIDES
    # --------------------------------------------------------

    mode = parser.add_mutually_exclusive_group(required=False)

    mode.add_argument(
        "--Lec",
        action="store_true",
        help="Force Lec extraction instead of automatic detection",
    )

    mode.add_argument(
        "--Chat",
        action="store_true",
        help="Force Chat extraction instead of automatic detection",
    )

    # --------------------------------------------------------
    # LEC-ONLY ARGUMENTS
    # --------------------------------------------------------

    parser.add_argument(
        "--by-paragraph",
        action="store_true",
        help="Join spans from the same paragraph into one line (Lec only)",
    )

    parser.add_argument(
        "--blank-between",
        action="store_true",
        help=(
            "Add blank lines between paragraphs "
            "(Lec only; requires --by-paragraph)"
        ),
    )

    # --------------------------------------------------------
    # CHAT-ONLY ARGUMENTS
    # --------------------------------------------------------

    parser.add_argument(
        "--pair",
        action="store_true",
        help='Join names and ordinary text as "name: text" (Chat only)',
    )

    parser.add_argument(
        "--keep-blank",
        action="store_true",
        help="Keep blank ordinary chat bubbles (Chat only)",
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # DETERMINE WHETHER A MANUAL MODE WAS SELECTED
    # --------------------------------------------------------

    explicit_mode: Optional[str] = (
        "Lec" if args.Lec
        else "Chat" if args.Chat
        else None
    )

    if args.blank_between and not args.by_paragraph:

        parser.error(
            "--blank-between requires --by-paragraph"
        )

    # Validate immediately if the user supplied a manual override.

    if explicit_mode is not None:

        validate_mode_options(
            parser,
            explicit_mode,
            args
        )

    # --------------------------------------------------------
    # SELECT INPUT FILE
    # --------------------------------------------------------

    if args.source:

        source = args.source

    elif explicit_mode == "Lec":

        source = "text.html"

    else:

        source = "input.html"

    # --------------------------------------------------------
    # LOAD HTML
    # --------------------------------------------------------

    try:

        html = load_html(source)

    except Exception as exc:

        sys.exit(
            f"Could not load '{source}': {exc}"
        )

    # --------------------------------------------------------
    # PARSE HTML ONCE
    # --------------------------------------------------------

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # --------------------------------------------------------
    # AUTOMATIC MODE DETECTION
    # --------------------------------------------------------

    detected_mode = None

    try:

        detected_mode = detect_mode(soup)

    except ModeDetectionError as exc:

        if explicit_mode is None:

            sys.exit(
                "Automatic mode detection failed:\n"
                f"  {exc}"
            )

        print(
            f"Warning: {exc}",
            file=sys.stderr
        )

        print(
            f"Continuing with manually selected mode: {explicit_mode}",
            file=sys.stderr
        )

    # --------------------------------------------------------
    # SELECT FINAL EXTRACTION MODE
    # --------------------------------------------------------

    if explicit_mode is not None:

        selected_mode = explicit_mode

        if (
            detected_mode is not None
            and detected_mode != explicit_mode
        ):

            print(
                f"Warning: HTML appears to be {detected_mode}, "
                f"but --{explicit_mode} was supplied. "
                "Using the manual override.",
                file=sys.stderr
            )

    else:

        selected_mode = detected_mode

    # Validate mode-specific arguments against the detected format.

    validate_mode_options(
        parser,
        selected_mode,
        args
    )

    print(
        f"Detected format: {detected_mode or 'unknown'}"
    )

    if explicit_mode and detected_mode != explicit_mode:

        print(
            f"Using manual override: {selected_mode}"
        )

    # --------------------------------------------------------
    # SELECT OUTPUT FILE
    # --------------------------------------------------------

    if args.output:

        output = args.output

    elif selected_mode == "Lec":

        output = "highlight_output.txt"

    else:

        output = "output.txt"

    # --------------------------------------------------------
    # EXTRACT CONTENT
    # --------------------------------------------------------

    if selected_mode == "Lec":

        lines = extract_lec(
            soup,
            by_paragraph=args.by_paragraph,
            blank_between=args.blank_between,
        )

        if not lines:

            sys.exit(
                "Nothing extracted.\n"
                f"  File size: {len(html):,} characters\n"
                f"  'highlight cursorPointer' appears "
                f"{len(soup.find_all(is_highlight))} times in the parsed HTML.\n"
                "  The saved HTML may not contain the chapter text. "
                "Try copying the chapter's Outer HTML after it has fully loaded."
            )

    else:

        lines = extract_chat(
            soup,
            keep_blank=args.keep_blank,
            pair=args.pair,
        )

        if not lines:

            names = len(soup.find_all(is_name))
            texts = len(soup.find_all(is_text))
            images = len(soup.find_all(is_chat_image))
            socials = len(soup.find_all(is_social_post))

            sys.exit(
                "Nothing extracted.\n"
                f"  File size: {len(html):,} characters\n"
                f"  chatStoryName elements: {names}; "
                f"chatStoryText elements: {texts}; "
                f"chat image elements: {images}; "
                f"social-post elements: {socials}.\n"
                "  Check that the saved HTML includes the chat bubbles."
            )

    # --------------------------------------------------------
    # WRITE OUTPUT
    # --------------------------------------------------------

    write_output(
        lines,
        output
    )


if __name__ == "__main__":
    main()