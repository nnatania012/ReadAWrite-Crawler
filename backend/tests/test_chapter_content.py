"""GET /api/jobs/{id}/chapters/{n}/content: exact text, strict gating."""

import hashlib
import json
import unicodedata

import pytest
from fastapi.testclient import TestClient

from config import Settings
from main import create_app
from tests.fakes import FakeAdapter
from tests.helpers import make_job

SESSION = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
H = lambda sid: {"X-Session-ID": sid}          # noqa: E731

# Thai, English, emoji, special Unicode, paragraphs, blank-line runs, leading and
# trailing whitespace, image placeholders, URLs, and mixed CRLF / LF line endings.
THAI = "สวัสดีครับ นี่คือบทที่หนึ่ง\u200b ผู้ชาย: “ไปไหนมา?” — ฉันถามเบา ๆ"
COMPLEX = (
    "   \tleading spaces and a tab\r\n"
    f"{THAI}\n"
    "English line with punctuation: !?;,.…\u00a0(non-breaking space)\n"
    "Emoji: 😀🔥👩‍👩‍👧‍👦 ❤\ufe0f 🇹🇭\n"
    "NFD e + combining acute: e\u0301 | ANGSTROM SIGN: \u212b | ligature: \ufb01 | ZWJ:\u200d|\n"
    "\n\n\n"
    "Paragraph two after three blank lines.\r\n\r\n"
    "[image: https://cdn.example.com/img/ch1/pic_001.jpg]\n"
    "[รูปภาพ] https://www.readawrite.com/c/669e6e939a5f5c79b7eb5ee2ce220116?x=1&y=%E0%B8%81\n"
    "Backslashes \\ and quotes \" ' and <html>&amp; {json: \"x\"} 100% $HOME `cmd`\n"
    "trailing spaces on last line   \n\n  "
)
COMPLEX_BYTES = COMPLEX.encode("utf-8")


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "jobs", pipeline_script=tmp_path / "p.py",
        python_executable="python", max_active_jobs=1, max_queued_jobs=3,
        cors_origins=("http://localhost:5173",),
        allowed_hosts=("readawrite.com", "www.readawrite.com"),
        data_retention_days=0, cancel_grace_seconds=30, job_max_runtime_seconds=600,
        monitor_interval_seconds=0.02, session_sweep_seconds=0)
    with TestClient(create_app(settings, FakeAdapter()), headers=H(SESSION)) as client:
        client.post("/api/sessions/heartbeat")
        client.post("/api/sessions/heartbeat", headers=H(OTHER))
        yield client, client.app.state.fm


def job(fm, statuses, status="completed", texts=None, session=SESSION, **kw):
    return make_job(fm, statuses, job_status=status, session_id=session, texts=texts, **kw)


def content(client, job_id, n, **kw):
    return client.get(f"/api/jobs/{job_id}/chapters/{n}/content", **kw)


# ------------------------------------------------------------ exactness

def test_exact_utf8_text_is_returned_character_for_character(env):
    client, fm = env
    job_id = job(fm, {1: "success"}, texts={1: COMPLEX_BYTES})
    response = content(client, job_id, 1)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == COMPLEX_BYTES                    # byte-for-byte
    assert response.text == COMPLEX                             # exact string
    # what Get-Content -Encoding UTF8 -Raw would give for the same file
    assert (fm.output_dir(job_id) / "1.txt").read_bytes().decode("utf-8") == response.text
    # nothing was normalised or re-encoded
    assert "e\u0301" in response.text and "\u212b" in response.text and "\ufb01" in response.text
    assert unicodedata.normalize("NFC", response.text) != response.text
    assert "\r\n" in response.text and "\n\n\n" in response.text
    assert response.text.startswith("   \t") and response.text.endswith("\n\n  ")
    assert "😀" in response.text and "\u200d" in response.text


@pytest.mark.parametrize("raw", [
    b"x", b"x\n", b"x\r\n", b"\n\n\nx\n\n\n", b"  x  ", b"\t\tx\t\t\n",
    b"a\r\nb\nc\r\nd", b"a\rb", b"\r\n\r\n\r\n x", "ก\n\nข\n\n\nค".encode(),
    "😀".encode(), b"[img] https://a.example/b.png?x=1&y=2\n",
])
def test_whitespace_and_line_endings_are_never_altered(env, raw):
    client, fm = env
    job_id = job(fm, {1: "success"}, texts={1: raw})
    assert content(client, job_id, 1).content == raw


def test_body_is_plain_text_not_json(env):
    client, fm = env
    job_id = job(fm, {1: "success"}, texts={1: b'{"a": "b"}\n'})
    response = content(client, job_id, 1)
    assert response.content == b'{"a": "b"}\n'          # not wrapped or escaped
    assert not response.headers["content-type"].startswith("application/json")


def test_leading_bom_is_not_part_of_the_content_like_get_content(env):
    """PowerShell's Get-Content -Encoding UTF8 drops a BOM; the pipeline never writes one."""
    client, fm = env
    raw = b"\xef\xbb\xbf" + "สวัสดี\n".encode()
    job_id = job(fm, {1: "success"}, texts={1: raw})
    assert content(client, job_id, 1).content == "สวัสดี\n".encode()


def test_helper_returns_the_same_string(env):
    _, fm = env
    job_id = job(fm, {1: "success"}, texts={1: COMPLEX_BYTES})
    assert fm.get_chapter_content(job_id, 1) == COMPLEX
    assert fm.read_chapter_bytes(job_id, 1) == COMPLEX_BYTES


# --------------------------------------------------- job state restrictions

ALL_JOB_STATUSES = ["queued", "discovering", "processing", "packaging", "completed",
                    "completed_with_errors", "failed", "cancelling", "cancelled", "interrupted"]


@pytest.mark.parametrize("status", ALL_JOB_STATUSES)
def test_an_extracted_chapter_is_copyable_whatever_state_the_job_is_in(env, status):
    """Copy no longer waits for the whole job: a verified chapter is readable at once."""
    client, fm = env
    job_id = job(fm, {1: "success"}, status=status, texts={1: COMPLEX_BYTES})
    response = content(client, job_id, 1)
    assert response.status_code == 200 and response.content == COMPLEX_BYTES


@pytest.mark.parametrize("status", ["queued", "discovering", "processing", "packaging"])
def test_chapters_not_extracted_yet_are_refused_while_the_job_runs(env, status):
    client, fm = env
    job_id = job(fm, {1: "success", 2: "in_progress", 3: "pending"}, status=status)
    assert content(client, job_id, 1).status_code == 200
    for n in (2, 3):
        response = content(client, job_id, n)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "chapter_unavailable"
        assert "yet" in response.json()["error"]["message"]


def test_a_job_that_has_produced_no_manifest_yet_has_no_chapters(env):
    client, fm = env
    job_id = fm.create_job_dir()
    fm.write_job(job_id, {"job_id": job_id, "session_id": SESSION, "output_name": "x",
                          "source_url": "https://www.readawrite.com/a/x", "status": "queued",
                          "created_at": "2026-01-01T00:00:00+00:00",
                          "updated_at": "2026-01-01T00:00:00+00:00"})
    assert content(client, job_id, 1).status_code == 404


@pytest.mark.parametrize("status", ["completed", "completed_with_errors"])
def test_allowed_for_completed_jobs(env, status):
    client, fm = env
    job_id = job(fm, {1: "success"}, status=status)
    assert content(client, job_id, 1).status_code == 200


def test_completed_with_errors_only_successful_chapters_are_copyable(env):
    client, fm = env
    job_id = job(fm, {1: "success", 2: "failed", 3: "partial", 4: "success", 5: "pending"},
                 status="completed_with_errors")
    assert content(client, job_id, 1).status_code == 200
    assert content(client, job_id, 4).status_code == 200
    for n in (2, 3, 5):
        response = content(client, job_id, n)
        assert response.status_code == 409 and response.json()["error"]["code"] == "chapter_unavailable"
    flags = {c["number"]: c["copyable"]
             for c in client.get(f"/api/jobs/{job_id}/chapters").json()["chapters"]}
    assert flags == {1: True, 2: False, 3: False, 4: True, 5: False}


# --------------------------------------------------------- file integrity

def test_missing_file_is_unavailable(env):
    client, fm = env
    job_id = job(fm, {1: "success"})
    (fm.output_dir(job_id) / "1.txt").unlink()
    assert content(client, job_id, 1).json()["error"]["code"] == "chapter_unavailable"


def test_size_mismatch_is_rejected(env):
    client, fm = env
    job_id = job(fm, {1: "success"})
    (fm.output_dir(job_id) / "1.txt").write_bytes(b"something else entirely")
    assert content(client, job_id, 1).status_code == 409


def test_same_size_tampering_is_caught_by_the_checksum(env):
    client, fm = env
    job_id = job(fm, {1: "success"}, texts={1: b"AAAA"})
    (fm.output_dir(job_id) / "1.txt").write_bytes(b"BBBB")
    response = content(client, job_id, 1)
    assert response.status_code == 409 and "integrity" in response.json()["error"]["message"]


def test_empty_file_is_rejected(env):
    client, fm = env
    job_id = job(fm, {1: "success"}, texts={1: b""})
    assert content(client, job_id, 1).status_code == 409


def test_invalid_utf8_is_rejected_even_with_a_matching_checksum(env):
    client, fm = env
    job_id = job(fm, {1: "success"}, texts={1: b"caf\xe9 \xff\xfe"})
    response = content(client, job_id, 1)
    assert response.status_code == 409 and "UTF-8" in response.json()["error"]["message"]


def test_manifest_output_name_mismatch_is_rejected(env):
    client, fm = env
    job_id = job(fm, {1: "success", 2: "success"})
    manifest_path = fm.output_dir(job_id) / "pipeline_state.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["chapters"]["1"]["output"] = "2.txt"          # points at another chapter
    manifest_path.write_text(json.dumps(manifest))
    assert content(client, job_id, 1).status_code == 409


# ----------------------------------------------- chapter number validation

def test_unknown_chapter_is_404(env):
    client, fm = env
    job_id = job(fm, {1: "success"})
    for n in (2, 999, 0):
        response = content(client, job_id, n)
        assert response.status_code == 404 and response.json()["error"]["code"] == "chapter_not_found"


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "%2e%2e", "１", "1%2f..%2f2", "0x1", "+1", " 1"])
def test_only_plain_positive_integers_are_accepted(env, bad):
    client, fm = env
    job_id = job(fm, {1: "success", 2: "success"})
    response = content(client, job_id, bad)
    assert response.status_code in (404, 422)
    assert response.status_code != 200


# ------------------------------------------------ sessions and visibility

def test_other_sessions_cannot_read_the_chapter(env):
    client, fm = env
    job_id = job(fm, {1: "success"})
    assert content(client, job_id, 1, headers=H(OTHER)).status_code == 404


def test_session_is_required(env):
    client, fm = env
    job_id = job(fm, {1: "success"})
    response = client.get(f"/api/jobs/{job_id}/chapters/1/content", headers={"X-Session-ID": ""})
    assert response.status_code == 400 and response.json()["error"]["code"] == "session_required"
    assert content(client, job_id, 1, headers=H("not-a-uuid")).status_code == 400


def test_unknown_job_is_404_and_cleanup_pending_job_is_410(env):
    client, fm = env
    assert content(client, "a1b2c3d4e5f6", 1).status_code == 404
    job_id = job(fm, {1: "success"})
    fm.update_job(job_id, cleanup_pending=True)
    response = content(client, job_id, 1)
    assert response.status_code == 410 and response.json()["error"]["code"] == "job_expired"


def test_session_query_parameter_works_for_plain_links(env):
    client, fm = env
    job_id = job(fm, {1: "success"})
    response = client.get(f"/api/jobs/{job_id}/chapters/1/content?session_id={SESSION}",
                          headers={"X-Session-ID": ""})
    assert response.status_code == 200


# ---------------------------------------------- metadata endpoints stay light

def test_status_and_chapter_endpoints_never_contain_chapter_text(env):
    client, fm = env
    secret = "SECRET-CHAPTER-TEXT-สวัสดี"
    job_id = job(fm, {1: "success", 2: "failed"}, texts={1: secret.encode()})
    for path in ("", "/chapters"):
        body = client.get(f"/api/jobs/{job_id}{path}").text
        assert secret not in body and "SECRET" not in body
    assert secret not in client.get("/api/jobs").text


def test_copyable_flags_follow_each_chapter_while_the_job_is_still_running(env):
    client, fm = env
    job_id = job(fm, {1: "success", 2: "success", 3: "in_progress", 4: "pending", 5: "failed"},
                 status="processing")
    chapters = client.get(f"/api/jobs/{job_id}/chapters").json()["chapters"]
    assert {c["number"]: c["copyable"] for c in chapters} == {
        1: True, 2: True, 3: False, 4: False, 5: False}


def test_a_chapter_whose_file_is_damaged_is_not_offered_even_mid_run(env):
    client, fm = env
    job_id = job(fm, {1: "success", 2: "success"}, status="processing")
    (fm.output_dir(job_id) / "2.txt").unlink()
    chapters = client.get(f"/api/jobs/{job_id}/chapters").json()["chapters"]
    assert {c["number"]: c["copyable"] for c in chapters} == {1: True, 2: False}


def test_errors_never_leak_server_paths(env):
    client, fm = env
    job_id = job(fm, {1: "success"})
    (fm.output_dir(job_id) / "1.txt").unlink()
    text = content(client, job_id, 1).text
    assert str(fm.root) not in text and "/tmp" not in text and "output" not in text.lower().replace("output_", "")


def test_each_request_reads_the_file_fresh_and_keeps_nothing_in_memory(env):
    client, fm = env
    job_id = job(fm, {1: "success"}, texts={1: b"first"})
    assert content(client, job_id, 1).content == b"first"
    path = fm.output_dir(job_id) / "1.txt"
    manifest_path = fm.output_dir(job_id) / "pipeline_state.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["chapters"]["1"].update(output_bytes=6,
                                     output_sha256=hashlib.sha256(b"second").hexdigest())
    manifest_path.write_text(json.dumps(manifest))
    path.write_bytes(b"second")
    assert content(client, job_id, 1).content == b"second"        # no stale cache
