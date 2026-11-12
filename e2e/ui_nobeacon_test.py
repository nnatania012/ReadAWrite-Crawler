import os
import asyncio, json, re, time, urllib.request
from pathlib import Path
from playwright.async_api import async_playwright, expect

APP, API = "http://localhost:5173", "http://localhost:8000"
DATA = Path(os.environ.get("E2E_DATA_DIR", "./e2e-data"))
URL = lambda name: f"https://www.readawrite.com/a/{name}"
def launch_kwargs(args):
    """Use CHROMIUM_PATH if set, otherwise Playwright's own Chromium."""
    kwargs = {"args": args}
    if os.environ.get("CHROMIUM_PATH"):
        kwargs["executable_path"] = os.environ["CHROMIUM_PATH"]
    return kwargs

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def api(path, sid):
    req = urllib.request.Request(API + path, headers={"X-Session-ID": sid})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

async def ctx_new(browser):
    c = await browser.new_context(accept_downloads=True, viewport={"width": 1100, "height": 900})
    return c

async def start_job(page, name, output):
    await page.goto(APP)
    await page.get_by_label("Story URL").fill(URL(name))
    await page.get_by_label("Output folder name").fill(output)
    await page.get_by_role("button", name="Start Extraction").click()
    await page.wait_for_url(re.compile(r"/jobs/[0-9a-f]{12}$"))
    return page.url.rsplit("/", 1)[1]

sid_of = lambda page: page.evaluate("sessionStorage.getItem('rw:sessionId')")
def done_chapters(job_id):
    f = DATA / job_id / "output" / "pipeline_state.json"
    try: return sum(1 for c in json.loads(f.read_text())["chapters"].values() if c["status"] == "success")
    except Exception: return -1
async def gone(job_id, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not (DATA / job_id).exists(): return True
        await asyncio.sleep(0.5)
    return False



import subprocess
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs(["--no-sandbox", "--disable-setuid-sandbox", "--no-zygote", "--rw-test-kill-me=1"]))
        ctxN = await ctx_new(browser); pageN = await ctxN.new_page()
        jobN = await start_job(pageN, "FAKEOK", "No Beacon")
        await expect(pageN.get_by_text("Extraction complete")).to_be_visible(timeout=60_000)
        subprocess.run(["pkill", "-9", "-f", "rw-test-kill-me"])      # browser crash: no pagehide, no beacon
        log("browser killed (crash): no close notice of any kind can be sent")
        await asyncio.sleep(6)
        assert (DATA / jobN).exists(), "job vanished although no close notice could arrive"
        log("6s later the job is still there (nothing told the backend)")
        assert await gone(jobN, 30), "idle-timeout fallback did not remove the job"
        log("fallback: abandoned session's files removed by the idle timeout alone")

try:
    asyncio.run(main())
except Exception as exc:
    if "closed" in str(exc).lower() and "AssertionError" not in str(exc): print("(playwright noted the browser died, as expected)")
    else: raise
