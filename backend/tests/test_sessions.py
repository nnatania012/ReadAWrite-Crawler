"""Sessions, heartbeats, expiry, history isolation and automatic cleanup."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from config import Settings
from main import create_app
from services.cleanup_service import CleanupNotAuthorized
from storage.file_manager import FileManager
from storage.remote import GCSRemoteStorage
from tests.fakes import FakeAdapter
from tests.helpers import make_job, wait_for

A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
URL = "https://www.readawrite.com/a/S1"
URL2 = "https://www.readawrite.com/a/S2"
H = lambda sid: {"X-Session-ID": sid}          # noqa: E731


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_settings(tmp_path, **kw):
    values = dict(
        data_dir=tmp_path / "jobs", pipeline_script=tmp_path / "p.py",
        python_executable="python", max_active_jobs=1, max_queued_jobs=5,
        cors_origins=("http://localhost:5173",),
        allowed_hosts=("readawrite.com", "www.readawrite.com"),
        data_retention_days=0, cancel_grace_seconds=30, job_max_runtime_seconds=600,
        monitor_interval_seconds=0.02, session_idle_timeout_seconds=900,
        session_close_grace_seconds=30, session_sweep_seconds=0)
    values.update(kw)
    return Settings(**values)


@pytest.fixture
def env(tmp_path):
    clock, adapter = Clock(), FakeAdapter()
    app = create_app(make_settings(tmp_path), adapter, clock=clock)
    with TestClient(app) as client:
        for sid in (A, B):
            assert client.post("/api/sessions/heartbeat", headers=H(sid)).status_code == 200
        yield client, app, adapter, clock


def create(client, sid=A, url=URL, **kw):
    response = client.post("/api/jobs", json={"url": url, **kw}, headers=H(sid))
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def finish(client, job_id, sid=A, *statuses):
    statuses = statuses or ("completed", "completed_with_errors", "failed")
    wait_for(lambda: client.get(f"/api/jobs/{job_id}", headers=H(sid)).json()["status"] in statuses)


def job_dir_exists(app, job_id):
    return app.state.fm.job_dir(job_id).exists()


def sweep(app):
    return app.state.cleanup.cleanup_expired_sessions()


# ------------------------------------------------------------- heartbeat

def test_heartbeat_returns_timing_configuration(env):
    client, *_ = env
    body = client.post("/api/sessions/heartbeat", headers=H(A)).json()
    assert body["status"] == "active" and body["session_id"] == A
    assert body["heartbeat_seconds"] == 60 and body["idle_timeout_seconds"] == 900


def test_heartbeat_accepts_json_body_with_any_content_type(env):
    client, *_ = env
    response = client.post("/api/sessions/heartbeat", content=f'{{"session_id": "{A}"}}',
                           headers={"Content-Type": "text/plain;charset=UTF-8"})
    assert response.status_code == 200


@pytest.mark.parametrize("bad", ["", "abc", "1234", "../../etc/passwd", "g" * 36,
                                 "aaaaaaaa-aaaa-9aaa-8aaa-aaaaaaaaaaaa"])
def test_invalid_session_ids_are_rejected(env, bad):
    client, *_ = env
    response = client.post("/api/sessions/heartbeat", headers=H(bad))
    assert response.status_code == 400 and response.json()["error"]["code"] == "session_required"
    assert client.get("/api/jobs", headers=H(bad)).status_code == 400


def test_job_endpoints_need_a_registered_session(env):
    client, *_ = env
    stranger = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    assert client.get("/api/jobs", headers=H(stranger)).status_code == 410
    assert client.post("/api/jobs", json={"url": URL}, headers=H(stranger)).status_code == 410


def test_session_id_is_stored_in_job_metadata(env):
    client, app, *_ = env
    job_id = create(client)
    assert app.state.fm.read_job(job_id)["session_id"] == A


# ------------------------------------------------------------- isolation

def test_sessions_cannot_see_or_touch_each_others_jobs(env):
    client, app, adapter, _ = env
    job_id = create(client, A)
    finish(client, job_id)
    assert [j["job_id"] for j in client.get("/api/jobs", headers=H(A)).json()["jobs"]] == [job_id]
    assert client.get("/api/jobs", headers=H(B)).json()["jobs"] == []
    for method, path in (("get", ""), ("get", "/chapters"), ("get", "/chapters/1/content"),
                         ("get", "/download"), ("post", "/cancel"), ("post", "/retry")):
        response = getattr(client, method)(f"/api/jobs/{job_id}{path}", headers=H(B))
        assert response.status_code == 404, (method, path)


def test_same_url_in_two_sessions_is_allowed(env):
    client, *_ = env
    first, second = create(client, A), create(client, B)
    assert first != second


# ------------------------------------------------------------ expiration

def test_idle_session_expires_and_is_rejected_everywhere(env):
    client, app, adapter, clock = env
    clock.advance(901)
    assert client.get("/api/jobs", headers=H(A)).status_code == 410
    assert client.post("/api/sessions/heartbeat", headers=H(A)).status_code == 410
    body = client.get("/api/jobs", headers=H(A)).json()
    assert body["error"]["code"] == "session_expired"


def test_heartbeat_keeps_a_session_alive(env):
    client, app, adapter, clock = env
    for _ in range(5):
        clock.advance(600)                                   # 50 minutes in total
        assert client.post("/api/sessions/heartbeat", headers=H(A)).status_code == 200
    assert client.get("/api/jobs", headers=H(A)).status_code == 200


def test_abandoned_session_is_cleaned_without_any_close_notification(env):
    """The browser's close beacon is best-effort: the idle timeout is the fallback."""
    client, app, adapter, clock = env
    job_id = create(client, A)
    finish(client, job_id)
    clock.advance(899)
    assert sweep(app)["deleted"] == 0 and job_dir_exists(app, job_id)   # not yet expired
    clock.advance(2)
    assert sweep(app)["deleted"] == 1
    assert not job_dir_exists(app, job_id)
    assert client.post("/api/sessions/heartbeat", headers=H(A)).status_code == 410


def test_idle_cleanup_removes_only_the_expired_sessions_jobs(env):
    client, app, adapter, clock = env
    a_job, b_job = create(client, A), create(client, B, url=URL2)
    finish(client, a_job), finish(client, b_job, B)
    clock.advance(600)
    client.post("/api/sessions/heartbeat", headers=H(B))     # B stays active
    clock.advance(400)                                       # A idle 1000s, B idle 400s
    summary = sweep(app)
    assert summary["deleted"] == 1
    assert not job_dir_exists(app, a_job) and job_dir_exists(app, b_job)
    assert client.get(f"/api/jobs/{b_job}", headers=H(B)).status_code == 200
    assert client.get("/api/jobs", headers=H(A)).status_code == 410


def test_startup_sweep_removes_data_of_sessions_that_expired_while_down(tmp_path):
    clock, adapter = Clock(), FakeAdapter()
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, adapter, clock=clock)) as client:
        client.post("/api/sessions/heartbeat", headers=H(A))
        job_id = create(client, A)
        finish(client, job_id)
    clock.advance(5000)                                       # backend was down for a long time
    with TestClient(create_app(settings, FakeAdapter(), clock=clock)) as client:
        assert not client.app.state.fm.job_dir(job_id).exists()


def test_background_sweeper_thread_cleans_up_by_itself(tmp_path):
    clock, adapter = Clock(), FakeAdapter()
    settings = make_settings(tmp_path, session_sweep_seconds=0.05)
    with TestClient(create_app(settings, adapter, clock=clock)) as client:
        client.post("/api/sessions/heartbeat", headers=H(A))
        job_id = create(client, A)
        finish(client, job_id)
        clock.advance(901)
        wait_for(lambda: not client.app.state.fm.job_dir(job_id).exists())


# --------------------------------------------------------------- closing

def test_close_then_heartbeat_within_grace_is_a_refresh_and_keeps_everything(env):
    client, app, adapter, clock = env
    job_id = create(client)
    finish(client, job_id)
    assert client.post("/api/sessions/close", headers=H(A)).status_code == 202
    assert client.get("/api/jobs", headers=H(A)).status_code == 410     # history hidden while closing
    clock.advance(5)
    assert client.post("/api/sessions/heartbeat", headers=H(A)).status_code == 200   # the reloaded page
    assert [j["job_id"] for j in client.get("/api/jobs", headers=H(A)).json()["jobs"]] == [job_id]
    assert job_dir_exists(app, job_id)
    sweep(app)
    assert job_dir_exists(app, job_id)


def test_close_after_grace_removes_completed_output(env):
    client, app, adapter, clock = env
    job_id = create(client)
    finish(client, job_id)
    fm = app.state.fm
    assert (fm.downloads_dir(job_id) / "story1.zip").exists() or list(fm.downloads_dir(job_id).glob("*.zip"))
    client.post("/api/sessions/close", headers=H(A))
    clock.advance(31)
    sweep(app)
    assert not job_dir_exists(app, job_id)
    assert client.post("/api/sessions/heartbeat", headers=H(A)).status_code == 410   # can't come back


def test_close_with_zero_grace_removes_data_immediately(tmp_path):
    clock, adapter = Clock(), FakeAdapter()
    with TestClient(create_app(make_settings(tmp_path, session_close_grace_seconds=0), adapter,
                               clock=clock)) as client:
        client.post("/api/sessions/heartbeat", headers=H(A))
        job_id = create(client, A)
        finish(client, job_id)
        clock.advance(0.01)
        client.post("/api/sessions/close", headers=H(A))
        wait_for(lambda: not client.app.state.fm.job_dir(job_id).exists())


def test_new_session_never_sees_a_closed_sessions_history(env):
    client, app, adapter, clock = env
    old = create(client, A)
    finish(client, old)
    client.post("/api/sessions/close", headers=H(A))
    clock.advance(31)
    sweep(app)
    fresh = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    client.post("/api/sessions/heartbeat", headers=H(fresh))
    assert client.get("/api/jobs", headers=H(fresh)).json()["jobs"] == []


def test_closing_one_session_never_affects_another(env):
    client, app, adapter, clock = env
    a_job, b_job = create(client, A), create(client, B, url=URL2)
    finish(client, a_job), finish(client, b_job, B)
    client.post("/api/sessions/close", headers=H(A))
    clock.advance(31)
    client.post("/api/sessions/heartbeat", headers=H(B))
    sweep(app)
    assert not job_dir_exists(app, a_job) and job_dir_exists(app, b_job)
    assert client.get(f"/api/jobs/{b_job}", headers=H(B)).status_code == 200


def test_close_is_idempotent_and_safe_for_unknown_sessions(env):
    client, app, adapter, clock = env
    assert client.post("/api/sessions/close", headers=H(A)).status_code == 202
    assert client.post("/api/sessions/close", headers=H(A)).status_code == 202
    stranger = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    assert client.post("/api/sessions/close", headers=H(stranger)).status_code == 202
    assert client.post("/api/sessions/close", headers=H("nope")).status_code == 400


# ---------------------------------------- running jobs survive a closed tab

def test_running_job_keeps_running_and_keeps_its_files_after_the_session_closes(env):
    client, app, adapter, clock = env
    hold = threading.Event()
    adapter.plan(URL, hold=hold, statuses=["success"] * 4)
    job_id = create(client)
    wait_for(lambda: client.get(f"/api/jobs/{job_id}", headers=H(A)).json()["progress"]["processing"] == 1)
    client.post("/api/sessions/close", headers=H(A))
    clock.advance(31)
    summary = sweep(app)
    assert summary["deferred"] >= 1 and summary["deleted"] == 0
    fm = app.state.fm
    assert job_dir_exists(app, job_id)                          # nothing deleted while active
    assert fm.read_job(job_id)["cleanup_pending"] is True
    assert (fm.output_dir(job_id) / "1.txt").exists()
    assert adapter.started[0].exit_code is None                 # crawler not terminated
    sweep(app)
    assert job_dir_exists(app, job_id)                          # still alive on a second sweep


def test_job_is_deleted_once_its_worker_finishes_and_no_zip_is_built(env, monkeypatch):
    client, app, adapter, clock = env
    hold = threading.Event()
    adapter.plan(URL, hold=hold, statuses=["success"] * 3)
    job_id = create(client)
    wait_for(lambda: client.get(f"/api/jobs/{job_id}", headers=H(A)).json()["progress"]["processing"] == 1)
    built = []
    original = app.state.fm.create_archive
    monkeypatch.setattr(app.state.fm, "create_archive", lambda *a, **k: built.append(a) or original(*a, **k))
    client.post("/api/sessions/close", headers=H(A))
    clock.advance(31)
    sweep(app)
    hold.set()                                                  # let the pipeline finish normally
    wait_for(lambda: not job_dir_exists(app, job_id))
    assert built == []                                          # nobody wanted the ZIP


def test_cleanup_pending_job_cannot_be_retried_or_read(env):
    client, app, adapter, clock = env
    hold = threading.Event()
    adapter.plan(URL, hold=hold)
    job_id = create(client)
    wait_for(lambda: client.get(f"/api/jobs/{job_id}", headers=H(A)).json()["progress"]["processing"] == 1)
    app.state.cleanup.cleanup_job(job_id, authorized=True)      # marks pending
    assert client.post(f"/api/jobs/{job_id}/retry", headers=H(A)).status_code == 410
    assert client.get(f"/api/jobs/{job_id}", headers=H(A)).status_code == 410
    assert client.get("/api/jobs", headers=H(A)).json()["jobs"] == []
    hold.set()
    wait_for(lambda: not job_dir_exists(app, job_id))


def test_queued_job_of_a_closed_session_is_removed_and_never_runs(env):
    client, app, adapter, clock = env
    hold = threading.Event()
    adapter.plan(URL, hold=hold)
    adapter.plan(URL2)
    running = create(client, B, url=URL)
    wait_for(lambda: client.get(f"/api/jobs/{running}", headers=H(B)).json()["status"] == "processing")
    queued = create(client, A, url=URL2)
    client.post("/api/sessions/close", headers=H(A))
    clock.advance(31)
    client.post("/api/sessions/heartbeat", headers=H(B))
    sweep(app)
    assert not job_dir_exists(app, queued)
    hold.set()
    finish(client, running, B)
    assert URL2 not in adapter.start_order
    assert job_dir_exists(app, running)                          # B untouched


def test_interrupted_job_is_removed_once_no_worker_is_active(env):
    client, app, adapter, clock = env
    fm = app.state.fm
    job_id = make_job(fm, {1: "success", 2: "pending"}, job_status="interrupted", session_id=A)
    clock.advance(1000)
    sweep(app)
    assert not job_dir_exists(app, job_id)


def test_interrupted_job_with_a_surviving_orphan_process_is_kept(env):
    import os
    client, app, adapter, clock = env
    fm = app.state.fm
    job_id = make_job(fm, {1: "success"}, job_status="interrupted", session_id=A)
    fm.update_job(job_id, pid=os.getpid())                       # a process that is alive
    clock.advance(1000)
    sweep(app)
    assert job_dir_exists(app, job_id) and fm.read_job(job_id)["cleanup_pending"]


# ------------------------------------------------- cleanup service itself

def test_cleanup_is_idempotent(env):
    client, app, adapter, clock = env
    job_id = create(client)
    finish(client, job_id)
    cleanup = app.state.cleanup
    assert cleanup.cleanup_job(job_id, authorized=True) == "deleted"
    assert cleanup.cleanup_job(job_id, authorized=True) == "missing"
    assert cleanup.cleanup_session(A) == {"deleted": 0, "deferred": 0}
    sweep(app), sweep(app)


def test_cleanup_refuses_a_live_sessions_job(env):
    client, app, adapter, clock = env
    job_id = create(client)
    finish(client, job_id)
    with pytest.raises(CleanupNotAuthorized):
        app.state.cleanup.cleanup_job(job_id)
    assert job_dir_exists(app, job_id)


def test_concurrent_cleanups_delete_a_job_only_once(env):
    client, app, adapter, clock = env
    job_id = create(client)
    finish(client, job_id)
    results, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(app.state.cleanup.cleanup_job(job_id, authorized=True))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("deleted") == 1
    assert set(results) <= {"deleted", "missing", "deferred"}
    assert not job_dir_exists(app, job_id)


def test_a_job_being_deleted_cannot_be_started_or_retried(env):
    from services.errors import JobConflict
    client, app, adapter, clock = env
    job_id = create(client)
    finish(client, job_id)
    manager = app.state.manager
    with manager.lock:
        manager.deleting.add(job_id)
    with pytest.raises(JobConflict):
        manager.submit(job_id)
    assert app.state.cleanup.cleanup_job(job_id, authorized=True) == "deferred"
    with manager.lock:
        manager.deleting.discard(job_id)


def test_cleanup_session_only_touches_its_own_jobs(env):
    client, app, adapter, clock = env
    a_job, b_job = create(client, A), create(client, B, url=URL2)
    finish(client, a_job), finish(client, b_job, B)
    app.state.cleanup.cleanup_session(A)
    assert not job_dir_exists(app, a_job) and job_dir_exists(app, b_job)


def test_legacy_jobs_without_a_session_are_never_auto_deleted(env):
    client, app, adapter, clock = env
    legacy = make_job(app.state.fm, {1: "success"}, job_status="completed", session_id=None)
    clock.advance(100000)
    sweep(app)
    assert job_dir_exists(app, legacy)


def test_orphaned_job_with_a_lost_session_record_is_eventually_removed(env):
    client, app, adapter, clock = env
    fm = app.state.fm
    job_id = make_job(fm, {1: "success"}, job_status="completed", session_id=B)
    fm.update_job(job_id)                                          # fresh updated_at
    app.state.sessions.store.delete(B)                            # record lost
    sweep(app)
    assert job_dir_exists(app, job_id)                            # grace: maybe just not registered yet
    clock.advance(901)
    sweep(app)
    assert not job_dir_exists(app, job_id)


def test_cleanup_removes_everything_belonging_to_the_job(env):
    client, app, adapter, clock = env
    fm = app.state.fm
    job_id = create(client)
    finish(client, job_id)
    (fm.output_dir(job_id) / "input.html").write_text("<html/>")
    (fm.output_dir(job_id) / "last_partial.html").write_text("<html/>")
    (fm.output_dir(job_id) / "logs").mkdir()
    (fm.output_dir(job_id) / "logs" / "pipeline.log").write_text("x")
    app.state.cleanup.cleanup_job(job_id, authorized=True)
    assert not fm.job_dir(job_id).exists()
    assert fm.root.exists() and (fm.root / "_sessions").exists()   # shared storage stays


# ------------------------------------------- scheduler endpoint (scale to zero)

def test_internal_cleanup_endpoint_is_disabled_without_a_token(env):
    client, *_ = env
    assert client.post("/api/internal/cleanup").status_code == 404


def test_internal_cleanup_endpoint_with_token(tmp_path):
    clock, adapter = Clock(), FakeAdapter()
    with TestClient(create_app(make_settings(tmp_path, cleanup_token="s3cret"), adapter,
                               clock=clock)) as client:
        client.post("/api/sessions/heartbeat", headers=H(A))
        job_id = create(client, A)
        finish(client, job_id)
        clock.advance(901)
        assert client.post("/api/internal/cleanup").status_code == 403
        assert client.post("/api/internal/cleanup", headers={"X-Cleanup-Token": "wrong"}).status_code == 403
        response = client.post("/api/internal/cleanup", headers={"X-Cleanup-Token": "s3cret"})
        assert response.status_code == 200 and response.json()["deleted"] == 1
        assert not client.app.state.fm.job_dir(job_id).exists()


# ------------------------------------------------------- remote (GCS) storage

class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def delete(self):
        self.bucket.objects.discard(self.name)


class FakeBucket:
    def __init__(self, names):
        self.objects = set(names)
        self.deleted_bucket = False

    def list_blobs(self, prefix=""):
        return [FakeBlob(self, n) for n in sorted(self.objects) if n.startswith(prefix)]


class FakeGCSClient:
    def __init__(self, names):
        self.the_bucket = FakeBucket(names)

    def bucket(self, name):
        return self.the_bucket


def test_gcs_cleanup_deletes_only_the_jobs_prefix(tmp_path):
    keep, drop = "a" * 12, "b" * 12
    client = FakeGCSClient({f"jobs/{drop}/output/1.txt", f"jobs/{drop}/downloads/x.zip",
                            f"jobs/{keep}/output/1.txt", f"jobs/{drop}x/output/1.txt",
                            "other/file.txt"})
    remote = GCSRemoteStorage("my-bucket", "jobs", client=client)
    fm = FileManager(tmp_path / "jobs", remote=remote)
    job_id = fm.create_job_dir(drop)
    assert fm.delete_job(job_id) is True
    assert client.the_bucket.objects == {f"jobs/{keep}/output/1.txt",
                                         f"jobs/{drop}x/output/1.txt", "other/file.txt"}
    assert not client.the_bucket.deleted_bucket
    assert fm.delete_job(job_id) is False                          # idempotent


def test_gcs_storage_rejects_bad_input():
    with pytest.raises(ValueError):
        GCSRemoteStorage("", "jobs", client=FakeGCSClient(set()))
    remote = GCSRemoteStorage("b", "jobs", client=FakeGCSClient({"jobs/x"}))
    with pytest.raises(ValueError):
        remote.delete_job("../../")


def test_cleanup_waits_for_the_worker_before_touching_remote_storage(tmp_path):
    clock, adapter = Clock(), FakeAdapter()
    gcs = FakeGCSClient(set())
    remote = GCSRemoteStorage("bkt", "jobs", client=gcs)
    hold = threading.Event()
    adapter.plan(URL, hold=hold)
    with TestClient(create_app(make_settings(tmp_path), adapter, remote=remote, clock=clock)) as client:
        client.post("/api/sessions/heartbeat", headers=H(A))
        job_id = create(client, A)
        wait_for(lambda: client.get(f"/api/jobs/{job_id}", headers=H(A)).json()["progress"]["processing"] == 1)
        gcs.the_bucket.objects.add(f"jobs/{job_id}/output/1.txt")
        client.app.state.cleanup.cleanup_job(job_id, authorized=True)
        assert f"jobs/{job_id}/output/1.txt" in gcs.the_bucket.objects     # worker still active
        hold.set()
        wait_for(lambda: not gcs.the_bucket.objects)                         # deleted after it finished
