"""Server-side validation of user-supplied ReadAWrite URLs."""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from services.errors import InvalidRequestError

MAX_URL_LENGTH = 2048


def validate_readawrite_url(raw: str, allowed_hosts: Iterable[str]) -> str:
    """
    Return a normalised URL, or raise InvalidRequestError.

    Accepts only https URLs on the approved ReadAWrite hostnames. Rejects
    other schemes, credentials, unusual ports, IP addresses, localhost and
    anything that isn't plainly a public web address.
    """

    allowed = {host.lower() for host in allowed_hosts}
    text = (raw or "").strip()

    if not text:
        raise InvalidRequestError("Please enter a ReadAWrite URL.")

    if len(text) > MAX_URL_LENGTH:
        raise InvalidRequestError("The URL is too long.")

    if re.search(r"[\s\x00-\x1f\x7f]", text):
        raise InvalidRequestError("The URL contains invalid characters.")

    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise InvalidRequestError("That doesn't look like a valid URL.")

    if parts.scheme != "https":
        raise InvalidRequestError("Only https:// ReadAWrite URLs are supported.")

    if parts.username or parts.password:
        raise InvalidRequestError("URLs with credentials are not allowed.")

    if port not in (None, 443):
        raise InvalidRequestError("Custom ports are not allowed.")

    if not host:
        raise InvalidRequestError("That doesn't look like a valid URL.")

    try:
        ipaddress.ip_address(host.strip("[]"))
        raise InvalidRequestError("IP addresses are not allowed.")
    except ValueError:
        pass                               # not an IP literal: good

    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise InvalidRequestError("Local addresses are not allowed.")

    if host not in allowed:
        raise InvalidRequestError(
            "Only ReadAWrite URLs are supported "
            f"({', '.join(sorted(allowed))}).")

    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))
