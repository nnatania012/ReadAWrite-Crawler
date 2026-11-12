import json
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from storage.file_manager import (
    ArchiveNotReadyError, FileManager, InvalidJobIdError, JobNotFoundError,
    NoSuccessfulChaptersError, PathEscapeError, sanitize_error, sanitize_name,
)
from tests.helpers import make_job


@pytest.fixture
def fm(tmp_path):
    return FileManager(tmp_path / "jobs")


# ------------------------------------------------------------ directories

def test_unique_job_directories_and_layout(fm):
    ids = {fm.create_job_dir() for _ in range(25)}
    assert len(ids) == 25
    for job_id in ids:
        assert fm.output_dir(job_id).is_dir() and fm.downloads_dir(job_id).is_dir()


def test_never_reuses_an_existing_directory(fm):
    job_id = fm.create_job_dir()
    with pytest.raises(FileExistsError):
        fm.create_job_dir(job_id)


def test_duplicate_output_names_get_separate_directories(fm):
    a = make_job(fm, {1: "success"}, name="Same Name")
    b = make_job(fm, {1: "success"}, name="Same Name")
    assert a != b and fm.output_dir(a) != fm.output_dir(b)


@pytest.mark.parametrize("bad", ["../etc", "..", "abc", "A" * 12, "g" * 12,
                                 "a1b2c3d4e5f6/../x", "", "a1b2c3d4e5f"])
def test_invalid_job_ids_and_traversal_rejected(fm, bad):
    with pytest.raises((InvalidJobIdError, PathEscapeError)):
        fm.job_dir(bad)


# ------------------------------------------------------------ sanitising

@pytest.mark.parametrize("raw,expected", [
    ("My Story", "My Story"),
    ("../../etc/passwd", "etc_passwd"),
    ("a/b\\c:d*e?f\"g<h>i|j", "a_b_c_d_e_f_g_h_i_j"),
    ("  ..hidden.. ", "hidden"),
    ("CON", "story"), ("nul", "story"), ("", "story"), (None, "story"),
    ("///", "story"), ("....", "story"),
    ("tab\tand\x00null\x1fctrl", "tabandnullctrl"),
])
def test_sanitize_name(raw, expected):
    assert sanitize_name(raw) == expected


def test_sanitize_name_limits_length_and_keeps_unicode():
    assert len(sanitize_name("x" * 500)) <= 80
    assert sanitize_name("ตัวอย่าง นิยาย") == "ตัวอย่าง นิยาย"


def test_sanitize_error_removes_paths_and_traces():
    text = "Timeout at /home/user/app/pipeline.py and C:\\Users\\me\\x.py\nTraceback (most recent call last):\n  File x"
    cleaned = sanitize_error(text)
    assert "/home" not in cleaned and "C:\\" not in cleaned and "Traceback" not in cleaned
    assert "https://www.readawrite.com/c/abc" in sanitize_error("see https://www.readawrite.com/c/abc")
    assert len(sanitize_error("x" * 1000)) <= 300


# -------------------------------------------------------------- metadata

def test_job_metadata_persists_across_manager_instances(fm, tmp_path):
    job_id = make_job(fm, {1: "success"})
    fm.update_job(job_id, status="completed", total_chapters=1)
    again = FileManager(tmp_path / "jobs")
    assert again.read_job(job_id)["status"] == "completed"
    assert [j["job_id"] for j in again.list_jobs()] == [job_id]


def test_atomic_metadata_updates_under_concurrency(fm):
    job_id = make_job(fm, {1: "success"})
    errors = []

    def hammer(n):
        try:
            for i in range(60):
                fm.update_job(job_id, **{f"field_{n}": i})
                fm.read_job(job_id)           # must always parse
        except Exception as exc:                # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    meta = fm.read_job(job_id)
    assert all(meta[f"field_{n}"] == 59 for n in range(6))
    assert not list(fm.job_dir(job_id).glob("*.tmp"))


def test_missing_job(fm):
    with pytest.raises(JobNotFoundError):
        fm.read_job("a1b2c3d4e5f6")


def test_list_jobs_newest_first(fm):
    a = make_job(fm, {1: "success"})
    b = make_job(fm, {1: "success"})
    fm.update_job(a, created_at="2026-02-01T00:00:00+00:00")
    assert [j["job_id"] for j in fm.list_jobs()] == [a, b]


# ---------------------------------------------------------- chapter files

def test_numeric_ordering_and_missing_numbers_preserved(fm):
    job_id = make_job(fm, {1: "success", 2: "success", 3: "failed", 10: "success",
                           11: "success"})
    assert [r.number for r in fm.chapter_records(job_id)] == [1, 2, 3, 10, 11]
    files = fm.list_chapter_files(job_id)
    assert [f["filename"] for f in files] == ["1.txt", "2.txt", "10.txt", "11.txt"]
    assert 3 not in [f["number"] for f in files]            # no renumbering


def test_success_needs_a_valid_file_not_just_manifest(fm):
    job_id = make_job(fm, {1: "success", 2: "success", 3: "success"})
    out = fm.output_dir(job_id)
    (out / "1.txt").unlink()                                 # missing
    (out / "2.txt").write_text("tampered content that is longer")   # size changed
    records = {r.number: r for r in fm.chapter_records(job_id)}
    assert records[1].status == "failed" and "missing" in records[1].error
    assert records[2].status == "failed" and "size" in records[2].error
    assert records[3].status == "success"


def test_deep_check_detects_same_size_tampering(fm):
    job_id = make_job(fm, {1: "success"})
    path = fm.output_dir(job_id) / "1.txt"
    path.write_text("X" * len(path.read_text()))
    assert fm.chapter_records(job_id)[0].status == "success"            # cheap check
    assert fm.chapter_records(job_id, deep=True)[0].status == "failed"  # checksum


def test_summary_counts(fm):
    job_id = make_job(fm, {1: "success", 2: "failed", 3: "partial", 4: "pending",
                           5: "in_progress"})
    counts = fm.summarize(fm.chapter_records(job_id))
    assert counts["total"] == 5 and counts["completed"] == 1
    assert counts["failed"] == 1 and counts["partial"] == 1
    assert counts["pending"] == 1 and counts["processing"] == 1
    assert counts["processed"] == 3 and counts["percentage"] == 60.0


def test_errors_in_records_are_sanitised(fm):
    job_id = make_job(fm, {1: "failed"})
    assert "/home" not in fm.chapter_records(job_id)[0].error


# ----------------------------------------------------------------- archive

def names(path):
    with zipfile.ZipFile(path) as z:
        return z.namelist()


def test_zip_contents_order_and_no_internal_files(fm):
    job_id = make_job(fm, {1: "success", 2: "success", 3: "success", 10: "success",
                           11: "success"}, name="My Story")
    (fm.output_dir(job_id) / "input.html").write_text("<html/>")
    (fm.output_dir(job_id) / "logs").mkdir()
    (fm.output_dir(job_id) / "logs" / "pipeline.log").write_text("log")
    info = fm.create_archive(job_id)
    path, filename = fm.get_archive_path(job_id)
    assert filename == "My Story.zip" and info.successful == 5
    assert names(path) == [f"My Story/{n}.txt" for n in (1, 2, 3, 10, 11)]   # numeric
    assert not any(x in " ".join(names(path)) for x in
                   ("html", "log", "json", "pipeline_state"))               # no report: all good


def test_partial_success_archive_includes_report_and_keeps_numbers(fm):
    job_id = make_job(fm, {1: "success", 2: "failed", 3: "partial", 4: "success"})
    info = fm.create_archive(job_id)
    path, _ = fm.get_archive_path(job_id)
    assert names(path) == ["My Story/1.txt", "My Story/4.txt",
                           "My Story/extraction_report.json"]
    with zipfile.ZipFile(path) as z:
        report = json.loads(z.read("My Story/extraction_report.json"))
    assert (report["successful"], report["failed"], report["partial"]) == (2, 1, 1)
    assert [c["number"] for c in report["chapters"]] == [1, 2, 3, 4]       # all chapters
    assert "/home" not in json.dumps(report)
    assert (info.failed, info.partial) == (1, 1)
    assert (fm.downloads_dir(job_id) / "extraction_report.json").is_file()


def test_empty_archive_is_never_created(fm):
    job_id = make_job(fm, {1: "failed", 2: "partial"})
    with pytest.raises(NoSuccessfulChaptersError):
        fm.create_archive(job_id)
    assert not list(fm.downloads_dir(job_id).glob("*.zip"))
    with pytest.raises(ArchiveNotReadyError):
        fm.get_archive_path(job_id)


def test_no_archive_while_chapters_unfinished(fm):
    job_id = make_job(fm, {1: "success", 2: "in_progress", 3: "pending"})
    with pytest.raises(ArchiveNotReadyError):
        fm.create_archive(job_id)


def test_archive_reused_then_regenerated_after_retry(fm):
    job_id = make_job(fm, {1: "success", 2: "failed"})
    first = fm.create_archive(job_id)
    assert not first.reused
    assert fm.create_archive(job_id).reused                  # unchanged -> not rebuilt
    path, _ = fm.get_archive_path(job_id)
    stamp = path.stat().st_mtime_ns

    fm.invalidate_archive(job_id)                            # retry started
    with pytest.raises(ArchiveNotReadyError):
        fm.get_archive_path(job_id)

    # the retry fixes chapter 2
    state = json.loads((fm.output_dir(job_id) / "pipeline_state.json").read_text())
    text = "fixed chapter\n"
    (fm.output_dir(job_id) / "2.txt").write_text(text)
    import hashlib
    state["chapters"]["2"].update(status="success", output="2.txt", output_bytes=len(text),
                                  output_sha256=hashlib.sha256(text.encode()).hexdigest())
    (fm.output_dir(job_id) / "pipeline_state.json").write_text(json.dumps(state))
    second = fm.create_archive(job_id)
    path2, _ = fm.get_archive_path(job_id)
    assert not second.reused and second.successful == 2
    assert names(path2) == ["My Story/1.txt", "My Story/2.txt"]   # report no longer needed
    assert path2.stat().st_mtime_ns != stamp


def test_repeated_downloads_use_the_same_archive(fm):
    job_id = make_job(fm, {1: "success"})
    fm.create_archive(job_id)
    first, _ = fm.get_archive_path(job_id)
    second, _ = fm.get_archive_path(job_id)
    assert first == second and len(list(fm.downloads_dir(job_id).glob("*.zip"))) == 1


def test_corrupt_archive_is_not_served(fm):
    job_id = make_job(fm, {1: "success"})
    fm.create_archive(job_id)
    path, _ = fm.get_archive_path(job_id)
    path.write_bytes(b"not a zip")
    with pytest.raises(ArchiveNotReadyError):
        fm.get_archive_path(job_id)


def test_tampered_archive_name_in_metadata_is_rejected(fm):
    job_id = make_job(fm, {1: "success"})
    fm.create_archive(job_id)
    fm.update_job(job_id, archive_filename="../../../etc/passwd")
    with pytest.raises((PathEscapeError, ArchiveNotReadyError)):
        fm.get_archive_path(job_id)


def test_jobs_are_isolated(fm):
    a = make_job(fm, {1: "success"}, name="Same")
    b = make_job(fm, {1: "success", 2: "success"}, name="Same")
    fm.create_archive(a)
    fm.create_archive(b)
    pa, _ = fm.get_archive_path(a)
    pb, _ = fm.get_archive_path(b)
    assert pa.parent != pb.parent
    assert len(names(pa)) == 1 and len(names(pb)) == 2
    assert str(fm.job_dir(a)) in str(pa) and str(fm.job_dir(b)) not in str(pa)


def test_no_leftover_temp_files_after_archive(fm):
    job_id = make_job(fm, {1: "success"})
    fm.create_archive(job_id)
    assert not list(fm.downloads_dir(job_id).glob("*.tmp"))
    (fm.downloads_dir(job_id) / "half.zip.tmp").write_text("partial")
    assert fm.cleanup_temp_files() == 1


# ------------------------------------------------------------------ retention

def test_retention_disabled_by_default_never_deletes(fm):
    job_id = make_job(fm, {1: "success"}, job_status="completed")
    fm.update_job(job_id, created_at="2000-01-01T00:00:00+00:00")
    assert fm.cleanup_old_jobs(0) == [] and fm.job_exists(job_id)


def test_retention_only_removes_old_finished_unprotected_jobs(fm):
    old_done = make_job(fm, {1: "success"}, job_status="completed")
    old_active = make_job(fm, {1: "success"}, job_status="processing")
    old_queued = make_job(fm, {1: "success"}, job_status="queued")
    protected = make_job(fm, {1: "success"}, job_status="completed")
    recent = make_job(fm, {1: "success"}, job_status="completed")
    long_ago = "2000-01-01T00:00:00+00:00"
    for jid in (old_done, old_active, old_queued, protected):
        fm.update_job(jid, created_at=long_ago)
    fm.update_job(recent, created_at=datetime.now(timezone.utc).isoformat())
    deleted = fm.cleanup_old_jobs(30, protected_ids={protected})
    assert deleted == [old_done]
    assert all(fm.job_exists(j) for j in (old_active, old_queued, protected, recent))
