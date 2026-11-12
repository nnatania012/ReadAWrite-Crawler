import io
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from config import Settings
from main import create_app
from tests.fakes import FakeAdapter
from tests.helpers import wait_for

SESSION = "11111111-1111-4111-8111-111111111111"
URL = "https://www.readawrite.com/a/STORY1"
URL2 = "https://www.readawrite.com/a/STORY2"
URL3 = "https://www.readawrite.com/a/STORY3"


@pytest.fixture
def adapter():
    return FakeAdapter()


@pytest.fixture
def client(tmp_path, adapter):
    settings = Settings(
        data_dir=tmp_path / "jobs", pipeline_script=tmp_path / "pipeline.py",
        python_executable="python", max_active_jobs=1, max_queued_jobs=3,
        cors_origins=("http://localhost:5173",),
        allowed_hosts=("readawrite.com", "www.readawrite.com"),
        data_retention_days=0, cancel_grace_seconds=30, job_max_runtime_seconds=600,
        monitor_interval_seconds=0.02)
    with TestClient(create_app(settings, adapter),
                    headers={"X-Session-ID": SESSION}) as test_client:
        assert test_client.post("/api/sessions/heartbeat").status_code == 200
        yield test_client


def create(client, url=URL, **body):
    return client.post("/api/jobs", json={"url": url, **body})


def status_of(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json()["status"]


def wait_status(client, job_id, *wanted, timeout=10):
    return wait_for(lambda: status_of(client, job_id) in wanted and status_of(client, job_id),
                    timeout=timeout)


# ------------------------------------------------------------------ basics

def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["worker"]["running"] is True


def test_job_creation_returns_202_with_unique_ids(client, adapter):
    adapter.plan(URL2)
    first, second = create(client), create(client, URL2)
    assert first.status_code == 202 and second.status_code == 202
    body = first.json()
    assert body["status"] == "queued" and body["message"]
    assert len(body["job_id"]) == 12 and body["job_id"] != second.json()["job_id"]


def test_invalid_urls_rejected_with_consistent_json(client):
    for bad in ("http://www.readawrite.com/a/x", "https://evil.com/a/x",
                "https://127.0.0.1/a", "", "nonsense"):
        response = create(client, bad)
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "invalid_request" and error["message"]


def test_malformed_body_is_a_clean_422(client):
    response = client.post("/api/jobs", json={"nope": 1})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request"
    assert "Traceback" not in response.text


def test_default_and_custom_output_names(client, adapter):
    adapter.plan(URL2)
    custom = create(client, output_name="My: Story/../x").json()["job_id"]
    default = create(client, URL2).json()["job_id"]
    name = client.get(f"/api/jobs/{custom}").json()["output_name"]
    assert not any(ch in name for ch in '/\\:')          # no separators or reserved characters
    assert name == "My_ Story_.._x"                          # harmless once separators are gone
    assert client.get(f"/api/jobs/{default}").json()["output_name"] == "STORY2"


def test_output_name_too_long_rejected(client):
    assert create(client, output_name="x" * 101).status_code == 422


def test_duplicate_url_protection(client, adapter):
    hold = threading.Event()
    adapter.plan(URL, hold=hold, statuses=["success"] * 4)
    first = create(client)
    assert first.status_code == 202
    duplicate = create(client)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["job_id"] == first.json()["job_id"]
    hold.set()


def test_unknown_and_malformed_job_ids_are_404(client):
    for job_id in ("a1b2c3d4e5f6", "../etc", "nope"):
        for path in ("", "/chapters", "/download"):
            assert client.get(f"/api/jobs/{job_id}{path}").status_code == 404
    assert client.post("/api/jobs/a1b2c3d4e5f6/cancel").status_code == 404
    assert client.post("/api/jobs/a1b2c3d4e5f6/retry").status_code == 404


# ------------------------------------------------- progress / completion

def test_progress_reflects_the_manifest_while_running(client, adapter):
    hold = threading.Event()
    adapter.plan(URL, statuses=["success", "success", "failed", "success"], hold=hold)
    job_id = create(client).json()["job_id"]
    job = wait_for(lambda: (j := client.get(f"/api/jobs/{job_id}").json())
                   and j["progress"]["completed"] == 1
                   and j["progress"]["processing"] == 1 and j)
    assert job["status"] == "processing"
    assert job["current_chapter"] == 2
    progress = job["progress"]
    assert progress["total"] == 4 and progress["processed"] == 1
    assert progress["processing"] == 1 and progress["pending"] == 2
    assert progress["percentage"] == 25.0
    assert job["download_ready"] is False and job["download_url"] is None
    hold.set()


def test_completion_creates_archive_and_download_works(client, adapter):
    adapter.plan(URL, statuses=["success"] * 12)
    job_id = create(client, output_name="Big Story").json()["job_id"]
    wait_status(client, job_id, "completed")
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["download_ready"] and job["download_url"] == f"/api/jobs/{job_id}/download"
    assert job["progress"]["percentage"] == 100.0 and job["error"] is None

    first = client.get(job["download_url"])
    assert first.status_code == 200 and first.headers["content-type"] == "application/zip"
    assert "attachment" in first.headers["content-disposition"]
    assert "Big%20Story.zip" in first.headers["content-disposition"] or \
        "Big Story.zip" in first.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(first.content)) as z:
        assert z.namelist() == [f"Big Story/{n}.txt" for n in range(1, 13)]
    again = client.get(job["download_url"])                       # repeat download
    assert again.status_code == 200 and again.content == first.content
    assert client.head(job["download_url"]).status_code == 200


def test_failed_and_partial_chapters_give_completed_with_errors(client, adapter):
    adapter.plan(URL, statuses=["success", "failed", "partial", "success"])
    job_id = create(client).json()["job_id"]
    wait_status(client, job_id, "completed_with_errors")
    job = client.get(f"/api/jobs/{job_id}").json()
    assert (job["progress"]["completed"], job["progress"]["failed"],
            job["progress"]["partial"]) == (2, 1, 1)
    chapters = client.get(f"/api/jobs/{job_id}/chapters").json()["chapters"]
    assert [c["number"] for c in chapters] == [1, 2, 3, 4]
    assert [c["status"] for c in chapters] == ["success", "failed", "partial", "success"]
    assert chapters[1]["attempts"] == 3 and "/home" not in str(chapters[1]["error"])
    assert chapters[0]["filename"] == "1.txt" and chapters[0]["size_bytes"] > 0
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/jobs/{job_id}/download").content)) as z:
        assert "story1/extraction_report.json".lower() in [n.lower() for n in z.namelist()] \
            or any(n.endswith("extraction_report.json") for n in z.namelist())
        assert [n for n in z.namelist() if n.endswith(".txt")] == \
            [n for n in z.namelist() if n.endswith(("1.txt", "4.txt"))]


def test_job_with_no_successful_chapters_fails_without_archive(client, adapter):
    adapter.plan(URL, statuses=["failed", "partial"])
    job_id = create(client).json()["job_id"]
    wait_status(client, job_id, "failed")
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["download_ready"] is False and job["error"]
    response = client.get(f"/api/jobs/{job_id}/download")
    assert response.status_code == 409 and response.json()["error"]["code"] == "archive_not_ready"


def test_download_before_ready_is_409(client, adapter):
    hold = threading.Event()
    adapter.plan(URL, hold=hold)
    job_id = create(client).json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 409
    hold.set()


def test_pipeline_error_message_is_sanitised(client, adapter):
    adapter.plan(URL, statuses=[], exit_code=1,
                 log_text="Error: Could not retrieve the chapter list: HTTP 403 at /home/u/x/y.py\n")
    job_id = create(client).json()["job_id"]
    wait_status(client, job_id, "failed")
    error = client.get(f"/api/jobs/{job_id}").json()["error"]
    assert "403" in error and "/home" not in error


def test_pipeline_that_cannot_start_fails_cleanly(client, adapter):
    adapter.fail_start = True
    job_id = create(client).json()["job_id"]
    wait_status(client, job_id, "failed")
    assert client.get("/api/health").status_code == 200       # API still fine


def test_pipeline_stopping_early_is_interrupted_not_completed(client, adapter):
    adapter.plan(URL, statuses=["success", "pending", "pending"], exit_code=2)
    adapter.default["statuses"] = ["success"] * 3
    job_id = create(client).json()["job_id"]
    # the fake marks "pending" as terminal 'pending'; exit 2 with unfinished chapters
    wait_status(client, job_id, "interrupted", "failed", "completed_with_errors")
    assert status_of(client, job_id) == "interrupted"


# ----------------------------------------------------------------- queue

def test_queue_runs_one_job_at_a_time_in_order(client, adapter):
    for url in (URL, URL2, URL3):
        adapter.plan(url, statuses=["success"] * 3, delay=0.05)
    ids = [create(client, url).json()["job_id"] for url in (URL, URL2, URL3)]
    for job_id in ids:
        wait_status(client, job_id, "completed")
    assert adapter.max_concurrent == 1
    assert adapter.start_order == [URL, URL2, URL3]


def test_queue_limit(client, adapter):
    hold = threading.Event()
    adapter.plan(URL, hold=hold)
    urls = [f"https://www.readawrite.com/a/Q{i}" for i in range(6)]
    codes = [create(client, URL).status_code] + [create(client, u).status_code for u in urls]
    assert codes.count(429) >= 1
    hold.set()


# ---------------------------------------------------------------- cancel

def test_cancel_running_job_is_graceful_and_keeps_progress(client, adapter):
    hold = threading.Event()
    adapter.plan(URL, statuses=["success"] * 5, hold=hold, delay=0.02)
    job_id = create(client).json()["job_id"]
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").json()["progress"]["completed"] == 1)
    response = client.post(f"/api/jobs/{job_id}/cancel")
    assert response.status_code == 202 and response.json()["status"] == "cancelling"
    assert status_of(client, job_id) == "cancelling"           # not cancelled yet
    hold.set()                                                 # let the pipeline reach the stop check
    wait_status(client, job_id, "cancelled")
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["progress"]["completed"] >= 1 and job["download_ready"] is False
    assert adapter.started[0].stopped_gracefully and not adapter.started[0].killed
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_cancel_queued_job_is_immediate_and_never_runs(client, adapter):
    hold = threading.Event()
    adapter.plan(URL, hold=hold)
    adapter.plan(URL2)
    create(client, URL)
    queued = create(client, URL2).json()["job_id"]
    response = client.post(f"/api/jobs/{queued}/cancel")
    assert response.json()["status"] == "cancelled"
    hold.set()
    wait_for(lambda: not client.app.state.manager.counts()[0])
    assert URL2 not in adapter.start_order


# ----------------------------------------------------------------- retry

def test_retry_resumes_only_unfinished_chapters_and_rebuilds_archive(client, adapter):
    adapter.plan(URL, statuses=["success", "failed", "success"])
    job_id = create(client).json()["job_id"]
    wait_status(client, job_id, "completed_with_errors")
    first = client.get(f"/api/jobs/{job_id}/download").content

    adapter.plan(URL, statuses=["success", "success", "success"])      # now it works
    response = client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 202 and response.json()["status"] == "queued"
    wait_status(client, job_id, "completed")
    second = client.get(f"/api/jobs/{job_id}/download").content
    assert first != second
    with zipfile.ZipFile(io.BytesIO(second)) as z:
        assert [n for n in z.namelist()] == [f"STORY1/{n}.txt" for n in (1, 2, 3)]
    assert client.get(f"/api/jobs/{job_id}").json()["progress"]["completed"] == 3
    assert len(client.get("/api/jobs").json()["jobs"]) == 1            # same job, not a new one


def test_retry_rules(client, adapter):
    hold = threading.Event()
    adapter.plan(URL, hold=hold)
    job_id = create(client).json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409      # running
    hold.set()
    wait_status(client, job_id, "completed")
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409      # nothing to retry


# --------------------------------------------------------------- history

def test_job_history_newest_first_without_server_paths(client, adapter):
    adapter.plan(URL2)
    first = create(client).json()["job_id"]
    wait_status(client, first, "completed")
    second = create(client, URL2).json()["job_id"]
    wait_status(client, second, "completed")
    jobs = client.get("/api/jobs").json()["jobs"]
    assert [j["job_id"] for j in jobs] == [second, first]
    assert jobs[0]["download_ready"] and jobs[0]["completed_chapters"] == 3
    text = client.get("/api/jobs").text + client.get(f"/api/jobs/{first}").text
    assert "/tmp" not in text and "output" not in text.replace("output_name", "")


def test_cors_only_allows_configured_origin(client):
    ok = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in bad.headers
