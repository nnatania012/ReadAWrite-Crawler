import pytest

from services.errors import InvalidRequestError
from services.url_validation import validate_readawrite_url

HOSTS = ("readawrite.com", "www.readawrite.com")


def ok(url):
    return validate_readawrite_url(url, HOSTS)


def test_accepts_official_urls_and_normalises():
    assert ok("https://www.readawrite.com/a/abc123") == "https://www.readawrite.com/a/abc123"
    assert ok("  https://READAWRITE.com/a/x#frag ") == "https://readawrite.com/a/x"
    assert ok("https://www.readawrite.com:443/a/x?y=1").endswith("/a/x?y=1")


@pytest.mark.parametrize("url", [
    "", "   ", "not a url", "www.readawrite.com/a/x",
    "http://www.readawrite.com/a/x",            # not https
    "ftp://www.readawrite.com/a/x", "javascript:alert(1)", "file:///etc/passwd",
    "https://evil.com/a/x",
    "https://readawrite.com.evil.com/a/x",
    "https://evil.com/?u=https://www.readawrite.com/",
    "https://www.readawrite.com@evil.com/a/x",
    "https://user:pw@www.readawrite.com/a/x",
    "https://www.readawrite.com:8443/a/x",
    "https://127.0.0.1/a/x", "https://10.0.0.5/a/x", "https://[::1]/a/x",
    "https://localhost/a/x", "https://printer.local/a/x",
    "https://www.readawrite.com/a/x y", "https://www.readawrite.com/a/x\ny",
    "https://sub.readawrite.com/a/x",           # not explicitly approved
])
def test_rejects_unsafe_or_unsupported(url):
    with pytest.raises(InvalidRequestError):
        ok(url)


def test_approved_subdomain_via_configuration():
    assert validate_readawrite_url("https://m.readawrite.com/a/x",
                                   ("m.readawrite.com",)).startswith("https://m.")


def test_too_long():
    with pytest.raises(InvalidRequestError):
        ok("https://www.readawrite.com/" + "a" * 3000)
