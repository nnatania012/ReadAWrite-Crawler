# ReadAWrite Translated Reader — Implementation Audit

**Audit date:** 2026-11-20  
**Repository:** `nnatania012/ReadAWrite-Crawler`  
**Target branch:** `feature/translated-reader`  
**Base branch:** `main`  
**Audited commit:** `23258f9d382336e56dac73992ed31c2c1d053822`  
**Status:** Source inspection completed through GitHub repository access; local worktree and executable test baseline remain unverified because this environment has no checkout and cannot resolve `github.com`.

## 1. Branch and worktree preflight

- GitHub reports `feature/translated-reader` at `23258f9d382336e56dac73992ed31c2c1d053822`.
- GitHub reports `main` at the same commit, so the feature branch is exactly at the requested base commit (not behind or divergent).
- The commit message is `improve copy function`; its parent is `f7631b494b58790a5d1738e4c010fcf598086fc5`.
- No branch creation, reset, rebase, force-push, or change to `main` was performed.
- A local `git status --short` could not be run: `/mnt/data` is not a repository. A shallow clone attempt failed because the container could not resolve `github.com`. GitHub's committed tree can be inspected, but it cannot reveal uncommitted files in the user's separate local working tree. Therefore local changes/untracked user data have not been inspected and must be preserved when the branch is checked out locally.

## 2. Repository structure inspected

The current committed tree contains:

- Root crawler: `pipeline.py`, `chapext.py`, `rwfetch.py`, `rwext.py`, `requirements.txt`.
- FastAPI backend: `backend/main.py`, `backend/config.py`, `backend/api/routes.py`, `backend/api/schemas.py`.
- Backend services: `job_service.py`, `url_validation.py`, `errors.py`, `session_service.py`, `cleanup_service.py`.
- Backend storage: `file_manager.py`, `session_store.py`, `remote.py`.
- Backend workers: `job_manager.py`, `pipeline_adapter.py`.
- Backend tests: `test_api.py`, `test_chapter_content.py`, `test_file_manager.py`, `test_job_manager.py`, `test_sessions.py`, `test_url_validation.py`.
- Frontend: React 18 + TypeScript + Vite + Tailwind; `react-router-dom` is already installed. Pages are Home, Job, History, and NotFound. The API client is centralized in `frontend/src/api/client.ts`.
- E2E: `e2e/README.md`, fake pipeline, and three Chromium scripts for copy, session, and no-beacon behavior.
- No `docs/translation/` directory or translation-project implementation exists in this commit.
- Repository hygiene observations: both `.gitignore` and a separate tracked `gitignore` exist; several `__pycache__/*.pyc` files are tracked. These are reported only; they are not application source and are not deleted as part of this work.

Files inspected include the README, crawler/pipeline modules, backend routes/schemas/configuration/services/storage/workers, backend test modules, frontend package metadata/API/session provider/router/pages/storage/hooks, E2E README, environment example, and ignore files. Actual selectors and behavior below are based on those source files, not on the proposed feature specification.

## 3. Existing architecture and behavior

### 3.1 Job lifecycle and state transitions

1. `POST /api/jobs` in `backend/api/routes.py` calls `JobService.create_job`.
2. `JobService` validates the URL, creates a job directory through `FileManager`, stores job metadata including `session_id`, and submits the job to `JobManager`.
3. `JobManager` uses worker threads and a queue; it launches the existing pipeline as a separate process through `SubprocessPipelineAdapter` (argument list, `shell=False`).
4. Job statuses include `queued`, `discovering`, `processing`, `packaging`, `completed`, `completed_with_errors`, `failed`, `cancelling`, `cancelled`, and `interrupted`.
5. Progress is read from the pipeline's `pipeline_state.json`, not parsed from console output. Cancellation uses a stop file and is graceful before the configured force-stop grace period. Startup recovery requeues queued jobs and marks abandoned active jobs interrupted.
6. When the worker finishes, the cleanup hook runs. Archive creation is delegated to `FileManager`.

### 3.2 Exact chapter discovery point

- For URL input, `Pipeline.load_from_url` calls `fetch_chapter_list_html` in `pipeline.py`.
- That function uses the existing Playwright browser to wait for chapter links, expand collapsed groups selected by `chapext.GROUP_ROW_SELECTOR`, and scroll to allow lazy-loaded rows.
- On a successful rendered list, `Pipeline.load_from_url` calls `urls_from_html`, which calls `chapext.extract_chapter_links`.
- `chapext.py` currently recognizes `.chatChapterTableRow.notChapterStatus3` and `.articleTableContent .table-content-row`. It extracts chapter URLs from `onclick` assignments to `window.location.href`, accepts ReadAWrite `/c/` URLs, and deduplicates exact URLs while retaining order.
- Collapsed groups are represented by `tr.group_get_chapter`; the current loader clicks `td.tdGroupChapter` only while `is_loaded` is not true, then verifies group row counts.

### 3.3 Chapter numbering and inaccessible slots

- `State.new` in `pipeline.py` creates chapter records by enumerating the extracted URL list starting at 1.
- `Pipeline.process` iterates that URL list and uses the enumeration number as the output filename and chapter number. Failed and partial attempts retain that enumerated number once present in the list.
- The current TOC extraction only sees supported chapter rows and deduplicates identical URLs. A locked/paid entry omitted from the normal chapter-link list is not represented as a slot, so later discovered links can shift. There is no explicit `locked`/inaccessible TOC-slot record today.
- The feature must retain every discovered TOC row and its original order before capture; it must not attempt to open locked content through a workaround. Duplicate-looking rows must remain represented, with any linkage/deduplication recorded explicitly.

### 3.4 Playwright chapter rendering and HTML handling

- `Pipeline.fetch` calls `rwfetch.fetch_chapter`; there is no second API-layer crawler.
- `rwfetch.fetch_chapter` creates a fresh browser context and page per chapter, navigates with `domcontentloaded`, checks HTTP 401/403 and login redirects as non-retryable access denials, and treats 404/410 as missing.
- It detects Chat when `window.allnum` is set and Lec when `#chapterContents .highlight.cursorPointer` exists. Chat readiness uses `window.openAllBubble` and `#chatStoryArea .chatStoryContainer`; it expands bubbles only when the rendered count is below `window.allnum`. Lec readiness waits for the count of highlighted spans to stabilize.
- It captures the final page with `page.content()` and returns a `FetchResult` containing status, HTML, layout, HTTP status, expected/rendered counts, error code, and stats.
- On a partial result, `Pipeline.process_chapter` writes the HTML to the single working file `last_partial.html`; on success it writes the HTML to the single working file `input.html`, then extracts TXT. These are mutable last-result working files, not per-chapter immutable source snapshots. Failed captures with no returned HTML do not create an original snapshot.
- The successful chapter HTML is therefore consumed by `Pipeline.extract` and overwritten by later successful chapters. This is the exact point where the new persistent capture/archive integration should branch: preserve a separate immutable per-chapter snapshot before/alongside the existing TXT extraction.

### 3.5 Chat/Lec extraction and current selectors/helpers

- `rwext.detect_mode` recognizes Chat from `#chatStoryArea` / `.chatStoryContainer` / `.chatStoryContent` / `.chatStoryBubble` with a name, text, social card, or image marker. It recognizes Lec through `#chapterContents` highlighted cursor-pointer elements, with a `p.par` fallback.
- Lec extraction uses `is_highlight`, `span_text`, and `extract_lec`; it extracts elements with both `highlight` and `cursorPointer`, joining nested text without a separator to preserve Thai words split across spans. Optional paragraph mode groups by nearest `p` or parent.
- Chat extraction uses `is_name`, `is_text`, `is_chat_image`, `is_icon_image`, `is_social_post`, `find_icon_text_container`, `extract_social_post`, `collect_chat_items`, and `render_chat_items`.
- Existing Chat support includes sender/name elements, ordinary text bubbles, attachment images, icon-associated text, and grouped social cards. Social-card descendants are suppressed from duplicate extraction. Existing `--pair` output can join a sender label to the following ordinary message; default pipeline output does not pair. The new manifest parser must not collapse separate messages even though legacy TXT formatting has optional pairing behavior.
- The inspected source provides selectors/helpers, but no actual user-owned Chapter 1 capture fixture was found in the committed tree. No new ReadAWrite selectors should be assumed without a real authorized capture.

### 3.6 TXT, manifest, ZIP, and clipboard semantics

- The pipeline writes numbered `N.txt` files in UTF-8 using `"\n".join(lines)` and text-mode file I/O. It writes to a temporary sibling, flushes/fsyncs, verifies non-empty/readable content, then atomically replaces the final file. It records byte size and SHA-256 in `pipeline_state.json`.
- `rwext.write_output` (standalone CLI) writes UTF-8 `"\n".join(lines)` using `Path.write_text`; its CLI flags include Chat/Lec overrides, Lec paragraph/blank options, Chat pairing/keep-blank options, and output path.
- The pipeline state manifest is version 1 and stores the source label, chapter-list fingerprint, total chapter count, and per-number URL/status/output/attempt metadata.
- `FileManager` derives chapter records from that manifest and verifies successful TXT files against their expected name, size, and (for deep checks) SHA-256. ZIP creation is numeric-order and includes an extraction report when chapters failed/partial/skipped. Existing TXT/ZIP semantics must remain untouched.
- Clipboard API: `GET /api/jobs/{job_id}/chapters/{chapter_number}/content` returns the exact verified bytes as `text/plain; charset=utf-8`, with no trimming or newline normalization, after size/checksum/UTF-8 checks. The frontend passes the string to `navigator.clipboard.writeText`; if browser clipboard access fails, it shows a manual text-selection fallback.
- The frontend has an existing `useAutoDownload.ts` hook that attempts one ZIP download once per completed run and keeps a manual Download button fallback. Preserve its existing behavior and tests; do not add another download path or change manual download semantics.

### 3.7 Session lifecycle and cleanup

- `SessionStore` stores one JSON record per UUID under `<DATA_DIR>/_sessions`.
- `SessionService` treats sessions as active, closing, or expired based on heartbeat idle timeout and close grace. `X-Session-ID` is validated as a UUID and required for job routes.
- `CleanupService` only enumerates jobs under the existing `FileManager` root and removes jobs owned by expired sessions. Queued jobs are removed; running jobs are marked `cleanup_pending` and removed after the worker has stopped. Startup, finish hooks, heartbeat, sweeper, and optional cleanup-token endpoint can trigger sweeps.
- The job storage root is `Settings.data_dir` (default `./data/jobs`). `FileManager` lists only directories with the 12-hex job ID pattern. This gives a clean boundary for a new sibling persistent project root, provided the project store is not nested under `data_dir` and is never passed to job cleanup.

### 3.8 Frontend routes and lifecycle

- `frontend/src/main.tsx` mounts React Router; `App.tsx` defines `/`, `/jobs`, `/jobs/:jobId`, and fallback routes under `Layout`.
- `SessionProvider` initializes a unique per-tab session, sends heartbeat, sends a best-effort close beacon on pagehide, refreshes on visibility/page-show, and resets after HTTP 410. The UUID is stored in `sessionStorage`.
- `frontend/src/api/client.ts` centralizes API calls and attaches `X-Session-ID`; downloads use a query parameter because a plain anchor cannot set request headers.
- Existing pages are HomePage, HistoryPage, JobPage, and NotFoundPage. The router package is already installed; no new router is required.
- `useAutoDownload.ts` must be retained as inspected above.

## 4. Security controls and gaps

### Existing controls

- Backend URL validation requires HTTPS, rejects URL credentials, custom ports, IP literals, localhost-like names, and hosts outside the configured ReadAWrite allowlist.
- Chapter-list redirects are checked against ReadAWrite hosts; chapter fetch checks login redirects and HTTP 401/403 without bypassing restrictions.
- Job IDs are format-validated; filesystem paths are resolved and checked against trusted roots; metadata writes are atomic.
- API errors use the structured `{"error":{"code":"...","message":"..."}}` shape and sanitize file paths from exposed errors.
- CORS uses configured explicit origins and does not enable credentialed cross-origin requests.
- Cleanup scheduler endpoint uses a separate constant-time checked cleanup token.

### Gaps to address before persistent project endpoints are exposed

- README and code confirm there is no user authentication. Session UUIDs are caller-provided identifiers, not proof of identity.
- Existing job endpoints authorize by session UUID ownership only. This is not adequate for a public tunnel or persistent private projects.
- No password hashing, login/logout, authenticated server-side sessions, remember-me, session rotation, or CSRF protection exists.
- Production HTTPS cookie enforcement and fail-closed auth configuration do not exist.
- Existing CORS is exact-origin but currently does not allow credentialed cookie authentication; this must be deliberately and narrowly updated if browser auth uses cookies.
- Existing `DATA_DIR` defaults to `./data/jobs`; a separate project root configuration is needed. Project deletion and job/session cleanup must remain distinct.
- Resource-fetch SSRF protections for assets do not yet exist; they belong to the later capture/asset subsystem and must be designed before that subsystem fetches remote assets.

## 5. Current tests and baseline

### Backend test inventory

Source inspection found 133 pytest test functions across six modules:

| Module | Test functions |
|---|---:|
| `test_api.py` | 26 |
| `test_chapter_content.py` | 27 |
| `test_file_manager.py` | 28 |
| `test_job_manager.py` | 10 |
| `test_sessions.py` | 38 |
| `test_url_validation.py` | 4 |
| **Total** | **133** |

Backend test configuration is `backend/pytest.ini`; `backend/requirements-dev.txt` installs pytest and httpx. The README baseline command is:

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

### Frontend commands available

`frontend/package.json` defines `npm run build` (TypeScript project build plus Vite production build) and `npm run typecheck` (`tsc -b --noEmit`). It has no lint or unit-test script in the inspected package metadata. The lockfile is `frontend/package-lock.json`.

### Browser E2E commands

`e2e/README.md` documents installing Playwright/Chromium, running the backend with `e2e/fake_pipeline_cli.py`, building/previewing the frontend, then running:

- `python e2e/ui_copy_test.py`
- `python e2e/ui_session_test.py`
- `python e2e/ui_nobeacon_test.py`

### Execution result / limitation

No baseline test command was executed in this workspace. There is no local repository checkout, and `git clone` failed with `Could not resolve host: github.com`. Therefore backend, frontend, and browser test pass/fail counts are **not available**, not assumed to be green. The source-defined baseline inventory is 133 backend tests and three E2E scripts. Once a local checkout with dependencies and Chromium is available, run the commands above before feature changes and append exact results here.

## 6. Proposed implementation map for Parts 1 onward

The following are proposed extension points, subject to implementation against the inspected code:

| Capability | Existing module / proposed addition | Reason |
|---|---|---|
| Project settings | `backend/config.py`, `backend/.env.example` | Add a dedicated project root as a sibling of `DATA_DIR`; add explicit auth/session/cookie settings. |
| Domain records | `backend/api/schemas.py` or a new `backend/models/translation.py` | Typed records for project, chapter slots, capture attempts, assets, source blocks, translation versions, alignment, render artifacts, and story context. Keep API and persisted models explicit. |
| Persistent storage | New `backend/storage/project_store.py` | Own a root independent of `FileManager`; UUID-scoped project paths, path/symlink validation, atomic JSON writes, per-project locks, immutable version directories. |
| Project operations | New `backend/services/project_service.py` | Enforce owner binding and project lifecycle without coupling projects to job/session IDs. |
| Authentication | New `backend/services/auth_service.py` and `backend/storage/auth_store.py` (or equivalent existing-convention modules) | Password hash verification, server-side opaque sessions, expiry/revocation, remember-me, CSRF, throttling, and one configured owner identity. |
| Auth API | `backend/api/routes.py`, `backend/api/schemas.py`, `backend/main.py` | Add login/logout/me routes and authoritative route dependencies; retain X-Session-ID only for tab lifecycle. |
| Frontend authentication | `frontend/src/api/client.ts`, new auth context/login page, `App.tsx`, `SessionProvider.tsx` | Include credentials and CSRF on protected requests; preserve existing tab-session behavior as a separate concern. |
| Cleanup isolation | `backend/services/cleanup_service.py`, `backend/storage/file_manager.py` | Keep cleanup scoped to FileManager's jobs root; add regression tests proving project files survive session/job cleanup. |
| Chapter-slot correction | Later extension of `chapext.py` / pipeline TOC model | Preserve each TOC row's original position and status instead of numbering only successfully linked/captured chapters. This is not Part 1 implementation. |

## 7. Part 0 decisions and remaining blockers

- No feature implementation should be mixed into the audit commit.
- No existing extraction modules, tests, tracked bytecode, or ignore files should be deleted as cleanup.
- Persistent projects should default to a sibling path such as `data/translation_projects`, never a child of `data/jobs`.
- The first implementation stage should establish storage/domain/auth and cleanup isolation before any project endpoints become publicly usable.
- **Blocker:** local working-tree status and executable baseline tests are unavailable in this environment. The user should check `git status --short` in their local checkout before pulling any remote audit commit; do not overwrite local changes. Test results must be recorded after a runnable checkout is available.
- **Not a blocker for source-level planning:** the remote feature branch exists and exactly matches the specified base commit.
- **Next stage:** Part 1 — persistent domain records/store, path-safe atomic persistence, cleanup isolation, and first-party authentication/authorization foundation. Do not begin Part 2 crawler work yet.
