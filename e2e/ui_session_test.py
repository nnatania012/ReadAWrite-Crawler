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

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs(["--no-sandbox", "--disable-setuid-sandbox", "--no-zygote"]))
        # ---- session Y: an unrelated, still-open session with a finished job
        ctxY = await ctx_new(browser); pageY = await ctxY.new_page()
        jobY = await start_job(pageY, "FAKEOK", "Other Session")
        sidY = await sid_of(pageY)
        await expect(pageY.get_by_text("Extraction complete")).to_be_visible(timeout=60_000)
        log(f"session Y has finished job {jobY}")

        # ---- session X: refresh keeps the session, then the tab is closed mid-extraction
        ctxX = await ctx_new(browser); pageX = await ctxX.new_page()
        jobX = await start_job(pageX, "FAKESLOW", "Slow Story")
        sidX = await sid_of(pageX)
        tracked = await pageX.evaluate("JSON.parse(sessionStorage.getItem('rw:sessionJobs'))")
        assert tracked == [jobX], tracked
        await expect(pageX.get_by_text("Now processing chapter")).to_be_visible(timeout=20_000)
        await pageX.reload()
        await expect(pageX.get_by_role("heading", name="Slow Story")).to_be_visible()
        assert await sid_of(pageX) == sidX and pageX.url.endswith(jobX)
        status, body = api("/api/jobs", sidX)
        assert status == 200 and [j["job_id"] for j in body["jobs"]] == [jobX]
        log("refresh mid-extraction: same session, same job restored (pagehide close was reopened by the heartbeat)")
        time.sleep(1)
        assert (DATA / jobX).exists()

        before = done_chapters(jobX)
        await pageX.goto("about:blank")                       # closes the app: pagehide -> close beacon
        log("tab navigated away (close notice sent); waiting past the grace period…")
        await asyncio.sleep(7)
        meta = json.loads((DATA / jobX / "job.json").read_text())
        assert (DATA / jobX).exists(), "files deleted while the crawler was active!"
        assert meta.get("cleanup_pending") is True, meta
        assert not (DATA / jobX / "STOP").exists(), "the running pipeline must not be told to stop"
        log(f"after close: job dir still there, cleanup_pending=True, status={meta['status']}")
        mid = done_chapters(jobX)
        await asyncio.sleep(8)
        later = done_chapters(jobX)
        assert later > before or later >= 2, (before, mid, later)
        log(f"crawler kept running with no browser: chapters done {before} -> {mid} -> {later}")
        status, body = api("/api/jobs", sidX)
        assert status == 410, (status, body)
        log("closed session can no longer read history (410)")
        assert await gone(jobX, 40), "job files were not removed after the worker finished"
        log("worker finished normally -> job directory removed")
        assert (DATA / jobY).exists() and api(f"/api/jobs/{jobY}", sidY)[0] == 200
        await pageY.reload(); await expect(pageY.get_by_text("Extraction complete")).to_be_visible()
        log("other active session's job untouched and still visible")

        # ---- a brand-new session shows no old history
        ctxZ = await ctx_new(browser); pageZ = await ctxZ.new_page()
        await pageZ.goto(APP); await pageZ.get_by_role("link", name="History").click()
        await expect(pageZ.get_by_text("No extractions yet.")).to_be_visible()
        assert await sid_of(pageZ) not in (sidX, sidY)
        log("new session: empty history")
        await ctxZ.close()

        # ---- the old tab coming back after cleanup gets a clean new session + notice
        pageX2 = await ctxX.new_page()
        await pageX2.add_init_script(f"sessionStorage.getItem('rw:sessionId') || sessionStorage.setItem('rw:sessionId','{sidX}')")
        await pageX2.goto(APP)
        await expect(pageX2.get_by_text("previous session ended")).to_be_visible(timeout=10_000)
        assert await sid_of(pageX2) != sidX
        log("stale tab: told its session ended, started a fresh one")
        await ctxX.close()

        # ---- duplicated tab must get its OWN session (and closing it must not hurt the original)
        ctxD = await ctx_new(browser); pageA = await ctxD.new_page()
        jobA = await start_job(pageA, "FAKEOK", "Dup Original")
        sidA = await sid_of(pageA)
        await expect(pageA.get_by_text("Extraction complete")).to_be_visible(timeout=60_000)
        pageB = await ctxD.new_page()
        await pageB.add_init_script(f"sessionStorage.setItem('rw:sessionId','{sidA}'); sessionStorage.setItem('rw:sessionJobs', '[\"{jobA}\"]')")
        await pageB.goto(APP)                                  # what a duplicated tab looks like (copied sessionStorage)
        await expect(pageB.get_by_role("heading", name="Extract a story")).to_be_visible()
        sidB = await sid_of(pageB)
        assert sidB != sidA, "duplicate tab kept the original's session ID"
        assert await sid_of(pageA) == sidA
        log("duplicated tab got a new session ID; original keeps its own")
        await pageB.goto("about:blank")
        await asyncio.sleep(7)
        assert (DATA / jobA).exists() and api(f"/api/jobs/{jobA}", sidA)[0] == 200
        log("closing the duplicate did not touch the original session's job")
        await ctxD.close()
        assert await gone(jobA, 30), "original's job should be cleaned after ITS tab closed"
        log("…and it was cleaned up once its own tab closed")

        log("PART B PASSED")
        await browser.close()

asyncio.run(main())
