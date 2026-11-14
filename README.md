# ReadAWrite Extractor

Turn a ReadAWrite story into a ZIP of numbered text files, from a web page or from the command line.

```
React frontend  ──HTTP/JSON──▶  FastAPI backend  ──subprocess──▶  pipeline.py  ──▶  Chromium (Playwright)
 (progress UI)                  (jobs, queue, ZIP)                 (chapext / rwfetch / rwext)
```

* **Frontend** (`frontend/`): React + TypeScript + Vite + Tailwind. Paste a story URL, watch progress, download the ZIP **or copy any single chapter to the clipboard**, browse this session's history. It never scrapes or builds files itself.
* **Backend** (`backend/`): FastAPI. Creates jobs, queues them, runs the existing pipeline in a separate process, reports progress from the pipeline's own `pipeline_state.json`, builds and serves the ZIP.
* **Pipeline** (repository root): `pipeline.py`, `chapext.py`, `rwfetch.py`, `rwext.py`. Still fully usable from the terminal (see [Command-line use](#command-line-use)).

> **Personal use only.** Use this for content you have legitimate access to and respect ReadAWrite's terms and the authors' rights. The tool never logs in, solves CAPTCHAs or works around access restrictions, and the backend only accepts `https://readawrite.com` / `https://www.readawrite.com` URLs. **Do not expose the API to the public internet** without adding authentication and access controls; it has none.

## Repository layout

```
pipeline.py  chapext.py  rwfetch.py  rwext.py   extraction pipeline (CLI)
requirements.txt                                 pipeline dependencies
backend/
  main.py                    FastAPI app + startup/shutdown
  config.py                  environment settings
  api/        routes.py, schemas.py
  services/   job_service.py, url_validation.py, errors.py
  worker/     job_manager.py (queue, workers, cancel, recovery), pipeline_adapter.py
  services/   ... session_service.py, cleanup_service.py
  storage/    file_manager.py (all filesystem work, ZIP + report, verified chapter reads),
              session_store.py, remote.py (optional GCS deletion)
  tests/      automated tests (mocked pipeline)
frontend/
  src/api/client.ts          the only place that calls the backend
  src/hooks/                 polling + one-shot auto-download
  src/lib/session.ts         per-tab session ID (sessionStorage) + duplicate-tab handling
  src/pages/                 Home, Job (progress), History
  src/components/, src/lib/
```

## Setup

You need Python 3.10+ and Node.js 18+.

```bash
# 1. Backend + pipeline dependencies
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
python -m playwright install chromium

# 2. Frontend
cd frontend
npm install
cp .env.example .env                 # VITE_API_BASE_URL=http://localhost:8000
```

## Run

Two terminals.

```bash
# Terminal 1: backend  (run from the backend/ folder)
cd backend
uvicorn main:app --port 8000 --reload-exclude "data/*" --reload
```

```bash
# Terminal 2: frontend
cd frontend
npm run dev                          # http://localhost:5173
```

Open http://localhost:5173, paste a story URL such as `https://www.readawrite.com/a/ARTICLE_ID`, and press **Start Extraction**.

> If you use `--reload`, keep `--reload-exclude "data/*"` (or set `DATA_DIR` outside the project). Otherwise every file the job writes restarts the server mid-extraction.

### Configuration (environment variables, backend)

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data/jobs` | Where job folders are stored |
| `CORS_ORIGINS` | `http://localhost:5173` | Allowed frontend origin(s), comma separated |
| `MAX_ACTIVE_JOBS` | `1` | Jobs running at once (others wait in the queue) |
| `MAX_QUEUED_JOBS` | `20` | Queue limit (further requests get HTTP 429) |
| `DATA_RETENTION_DAYS` | `0` | `0` = never delete old jobs |
| `ALLOWED_HOSTS` | `readawrite.com,www.readawrite.com` | Hostnames accepted in URLs |
| `CANCEL_GRACE_SECONDS` | `900` | How long a cancelled job may finish its chapter before a forced stop |
| `JOB_MAX_RUNTIME_SECONDS` | `21600` | Maximum run time per job (then it stops and can be resumed) |
| `SESSION_HEARTBEAT_SECONDS` | `60` | How often the page sends a heartbeat |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `900` | No heartbeat for this long = session expired, data removed |
| `SESSION_CLOSE_GRACE_SECONDS` | `30` | After the page reports closing, a heartbeat inside this window reopens the session (a refresh) |
| `SESSION_SWEEP_SECONDS` | `30` | Background cleanup interval (`0` = off) |
| `CLEANUP_TOKEN` | empty | Enables `POST /api/internal/cleanup` for an external scheduler |
| `STORAGE_BACKEND`, `GCS_BUCKET`, `GCS_PREFIX` | `local`, empty, `jobs` | Optional Google Cloud Storage cleanup |
| `PIPELINE_SCRIPT`, `PYTHON_EXECUTABLE` | `../pipeline.py`, current Python | Advanced |

Frontend: `VITE_API_BASE_URL` (default `http://localhost:8000`).

## How a job works

1. `POST /api/jobs` validates the URL, creates `data/jobs/<job_id>/`, queues the job and returns **202** immediately.
2. A worker thread starts `pipeline.py <url> -o <job>/output --stop-file <job>/STOP` as a separate process (no shell). The pipeline opens the story page in Chromium, clicks open every collapsed chapter group, collects the chapter links, then fetches and extracts each chapter one at a time, writing `1.txt`, `2.txt`, … and `pipeline_state.json`.
3. While it runs, the backend reads `pipeline_state.json` for progress. Nothing is parsed from console output, and a chapter only counts as extracted when its `.txt` file exists with the recorded size.
4. When every chapter is finished (success, failed or partial) the backend builds the ZIP and sets the final status.

**Job states:** `queued → discovering → processing → packaging → completed | completed_with_errors | failed`, plus `cancelling → cancelled` and `interrupted`.

* `completed`: every chapter extracted. `completed_with_errors`: some failed/partial; the ZIP has the good chapters plus `extraction_report.json`. `failed`: nothing usable (no empty ZIP is ever made).
* **Cancel** creates the stop file. The pipeline finishes the chapter it is on, then exits; only then is the job `cancelled`. A forced stop is used only after `CANCEL_GRACE_SECONDS`.
* **Retry/Resume** re-runs the same pipeline on the same job folder. The pipeline verifies existing files against its manifest and redoes only what is unfinished. The old ZIP is removed and rebuilt afterwards.
* **Restart recovery:** on startup, queued jobs are re-queued and jobs that were running become `interrupted`; their files and manifests are kept and they can be resumed.

### Files on disk

```
data/jobs/<job_id>/
  job.json                 application metadata (status, counts, archive name)
  output/                  pipeline output: 1.txt, 2.txt, ..., pipeline_state.json,
                           logs/pipeline.log, input.html, last_partial.html
  downloads/               <name>.zip and extraction_report.json
  worker.log               raw pipeline console output (internal)
```

The ZIP contains `<name>/1.txt … N.txt` in numeric order (original chapter numbers, gaps kept) plus `extraction_report.json` when anything failed. Logs, HTML, manifests and metadata are never included.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Backend and worker status |
| POST | `/api/jobs` | Create a job `{url, output_name?}` → 202 |
| GET | `/api/jobs` | All jobs, newest first |
| GET | `/api/jobs/{id}` | Status and progress |
| GET | `/api/jobs/{id}/chapters` | Per-chapter status in order (metadata only, never text) |
| GET | `/api/jobs/{id}/chapters/{n}/content` | One chapter's exact text (`text/plain; charset=utf-8`) |
| POST | `/api/sessions/heartbeat` | Register/refresh the session |
| POST | `/api/sessions/close` | The page is closing (best effort) |
| GET/HEAD | `/api/jobs/{id}/download` | ZIP (streamed attachment) |
| POST | `/api/jobs/{id}/cancel` | Graceful cancel |
| POST | `/api/jobs/{id}/retry` | Resume / retry unfinished chapters |

Every job endpoint needs the caller's session ID: send `X-Session-ID: <uuid>` (plain download links use `?session_id=<uuid>`). A session can only see and use its own jobs; anything else is a 404.

Errors are always `{"error": {"code": "...", "message": "..."}}`; stack traces and server paths stay in the backend log. Interactive docs: http://localhost:8000/docs.

## Copying a chapter

Each chapter row has a **Copy** button next to its number and status.

* **It turns on chapter by chapter, as soon as each chapter is extracted.** You don't wait for the rest of the story: while the job is still running, every chapter the backend lists as `copyable` (recorded as successful in the pipeline manifest, with its file present and the right size) already has an active button. Chapters not extracted yet show a disabled **Copy**; failed/partial chapters show **Unavailable**. The list refreshes with the normal 2-second progress polling, so a button switches on within a couple of seconds of its chapter finishing. Chapters stay copyable if the job is later cancelled, interrupted or fails; only the ZIP waits for the whole job.
* Clicking it fetches `GET /api/jobs/{id}/chapters/{n}/content` for that one chapter and passes the response text straight to `navigator.clipboard.writeText`. Nothing is downloaded and no file is created. Each button has its own loading state and ignores repeated clicks while a request is in flight.
* **The backend enforces the rules too.** It answers `409` (`chapter_unavailable`) when the chapter isn't a verified success: not extracted yet, failed/partial, missing file, wrong size or SHA-256, or invalid UTF-8. The job's own state doesn't matter, `404` for an unknown job/chapter, `410` when the job's data has expired, and `422` for anything but a plain positive integer chapter number.
* **Exact text.** The file is read once as bytes, verified (size and SHA-256 from the manifest), checked to be valid UTF-8, and returned unchanged as `text/plain; charset=utf-8` with `Cache-Control: no-store`. No JSON, no trimming, no Unicode normalisation, no newline changes. This matches PowerShell's `Get-Content -Encoding UTF8 -Raw | Set-Clipboard`, including its dropping of a leading byte-order mark (the pipeline never writes one). The frontend does not call `trim`, `split`, `join` or any rendering step on the string.
* If the clipboard can't be written (permission denied, no Clipboard API, non-HTTPS page) the button explains why and opens a read-only text box so the text can be selected and copied by hand; the text is discarded when it is closed.

The Clipboard API needs HTTPS (or `localhost`). Browsers on some operating systems convert line endings when writing to the OS clipboard; that is outside the page's control.

## Sessions and automatic cleanup

* **Per tab.** The session ID (a UUID) and the IDs of this session's jobs are kept in `sessionStorage`: a refresh keeps them, closing the tab discards them, and a new tab starts a new session with no history. No chapter text, ZIPs or API responses are stored. A duplicated tab (which copies `sessionStorage`) is detected through a `BroadcastChannel` handshake and given its own new session ID, so closing one tab can't clean up the other's jobs.
* **Heartbeat.** The page sends `POST /api/sessions/heartbeat` about every 60 seconds. A session with no heartbeat for `SESSION_IDLE_TIMEOUT_SECONDS` (15 minutes) is expired.
* **Closing.** On `pagehide` the page sends a best-effort close notice (`sendBeacon`). Because a page *refresh* also fires `pagehide`, the session only becomes expired after `SESSION_CLOSE_GRACE_SECONDS` without a heartbeat; the reloaded page's first heartbeat reopens it. Browsers don't guarantee the notice arrives, so the idle timeout is always the fallback.
* **What is removed, and when.** Once a session has ended: finished jobs (`completed`, `completed_with_errors`, `failed`, `cancelled`, `interrupted`) are deleted completely (TXT files, ZIP, report, manifest, HTML, logs, metadata); queued jobs are taken out of the queue and deleted; a **running job is never stopped or deleted**. It is marked `cleanup_pending`, the pipeline finishes normally (no ZIP is built), and the job is deleted as soon as its worker has really stopped. A job with a still-living orphan process is kept until it is gone.
* **Safety.** Deletion happens under the same lock that starts/retries jobs, so a job can't be retried or started while it is being deleted, two cleanups can't delete the same job, and repeated calls are harmless. A `cleanup_pending` job can't be read, retried or downloaded (`410`). Closed or expired sessions get `410` and the page starts a fresh session with a notice.
* **Triggers.** Backend startup, job creation, job completion, heartbeats, a background sweeper thread, and (for scale-to-zero hosting) `POST /api/internal/cleanup` with header `X-Cleanup-Token: $CLEANUP_TOKEN`, which an external scheduler such as Cloud Scheduler can call. Nothing has to run permanently.
* **Cloud storage.** With `STORAGE_BACKEND=gcs`, deleting a job also deletes the objects under `<GCS_PREFIX>/<job_id>/` in `GCS_BUCKET` (after the worker has finished; never the bucket itself; install `requirements-gcs.txt` and give the service account `storage.objects.list` and `storage.objects.delete`). If the job directory is a gcsfuse mount, normal deletion already removes the objects.

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest                     # ~200 tests, mocked pipeline, temp directories
```

They cover URL validation, job creation, queueing and duplicate protection, progress, completion, failed/partial chapters, cancellation (graceful and forced), recovery, archives, downloads, path safety, **chapter content (byte-exact Thai/emoji/CRLF/blank-line text, every rejection case)**, **sessions (heartbeat, expiry, isolation, close and refresh, running/queued/interrupted jobs, concurrency, idempotent cleanup, scheduler endpoint)** and Google Cloud Storage deletion against a fake bucket. No test touches ReadAWrite.

Browser-level tests live in `e2e/` (see `e2e/README.md`): they check in real Chromium that the clipboard text equals the TXT file character for character, that a chapter's Copy button turns on the moment that chapter is extracted (while the job is still running) and copies exactly, that chapters not extracted yet are disabled and refused by the backend, that no download is triggered, and the session/cleanup flows.

## Command-line use

The original workflow still works without the web app:

```bash
python pipeline.py "https://www.readawrite.com/a/ARTICLE_ID"
python pipeline.py "https://www.readawrite.com/a/ARTICLE_ID" -o "My Story"
```

See the comments at the top of `pipeline.py` for details (retries, resume, Ctrl+C behaviour, `--stop-file`).

## Known limitations

* Single-user, local use; no authentication. Sessions separate browser tabs from each other but are **not a security boundary**: anyone who knows a session UUID can use it.
* Cleanup state (sessions, jobs) lives on the backend's local data directory; run a single backend instance, or share the directory, so every instance sees the same sessions.
* Google Cloud Storage deletion is implemented and unit-tested against a fake bucket, **not against real GCS**. Test it with your bucket before relying on it. Chromium and the pipeline still need a normal local working directory.
* Legacy jobs created before sessions existed (no `session_id`) are never shown or auto-deleted.
* A tab that sleeps longer than the idle timeout (for example a closed laptop) loses its session; the page then starts a new one.
* The pipeline only collects chapters that have a normal chapter link. Paid/locked chapters that open a payment prompt are skipped, which shifts later numbers.
* The chapter list loader handles collapsed "#11 – #21" groups and lazy scrolling; other "load more" buttons or tabs are not clicked.
* Windows process control (`taskkill`, PID checks) is implemented but was not exercised on Windows.
