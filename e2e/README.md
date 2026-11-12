# End-to-end browser tests

Reference scripts that drive the real UI in Chromium against the real backend, using a **stand-in
pipeline** (`fake_pipeline_cli.py`) that writes chapters containing Thai text, emoji, mixed CRLF/LF,
blank-line runs, leading/trailing whitespace, image placeholders and URLs. They check that the
clipboard receives exactly the TXT file's text, plus the session and cleanup behaviour.

## Run

Use short session timeouts so the cleanup paths finish quickly. From the repository root:

```bash
pip install playwright && python -m playwright install chromium

# 1. backend (own terminal)
cd backend
DATA_DIR=../e2e-data PIPELINE_SCRIPT=../e2e/fake_pipeline_cli.py \
MONITOR_INTERVAL_SECONDS=0.3 SESSION_HEARTBEAT_SECONDS=2 SESSION_IDLE_TIMEOUT_SECONDS=12 \
SESSION_CLOSE_GRACE_SECONDS=3 SESSION_SWEEP_SECONDS=1 \
uvicorn main:app --port 8000

# 2. frontend (own terminal)
cd frontend && npm run build && npx vite preview --port 5173

# 3. tests (own terminal, repository root)
export E2E_DATA_DIR=./e2e-data
python e2e/ui_copy_test.py        # Copy button, exact clipboard text, ZIP still works
python e2e/ui_session_test.py     # refresh, closed tab, cleanup, isolation, duplicate tab
python e2e/ui_nobeacon_test.py    # browser crash: the idle-timeout fallback
```

Set `CHROMIUM_PATH` to use a specific Chromium binary. `ui_nobeacon_test.py` kills the browser with
`pkill` (Linux/macOS). Nothing else should be using ports 8000/5173, and `e2e-data` is created and
cleaned by the tests.

The clipboard comparison reads `navigator.clipboard.readText()` back in the same browser. Chromium on
Linux preserved `\r\n` exactly; browsers on other operating systems may convert line endings when they
write to the OS clipboard, which no web page can control.
