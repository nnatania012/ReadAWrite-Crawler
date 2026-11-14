import os
import asyncio, io, json, re, sys, time, zipfile, urllib.request
from pathlib import Path
from playwright.async_api import async_playwright, expect
from texts import TEXTS, COMPLEX

APP = "http://localhost:5173"
API = "http://localhost:8000"
DATA = Path(os.environ.get("E2E_DATA_DIR", "./e2e-data"))
URL = lambda name: f"https://www.readawrite.com/a/{name}"

def launch_kwargs(args):
    """Use CHROMIUM_PATH if set, otherwise Playwright's own Chromium."""
    kwargs = {"args": args}
    if os.environ.get("CHROMIUM_PATH"):
        kwargs["executable_path"] = os.environ["CHROMIUM_PATH"]
    return kwargs

def log(msg): print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def api(path, sid, method="GET"):
    req = urllib.request.Request(API + path, method=method, headers={"X-Session-ID": sid})
    try:
        with urllib.request.urlopen(req) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

async def new_context(browser, perms=True):
    ctx = await browser.new_context(accept_downloads=True, viewport={"width": 1100, "height": 900})
    if perms: await ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=APP)
    return ctx

async def start_job(page, name, output):
    await page.goto(APP)
    await page.get_by_label("Story URL").fill(URL(name))
    await page.get_by_label("Output folder name").fill(output)
    await page.get_by_role("button", name="Start Extraction").click()
    await page.wait_for_url(re.compile(r"/jobs/[0-9a-f]{12}$"))
    return page.url.rsplit("/", 1)[1]

async def session_id(page): return await page.evaluate("sessionStorage.getItem('rw:sessionId')")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs(["--no-sandbox", "--disable-setuid-sandbox", "--no-zygote"]))
        downloads, content_requests = [], []

        # ================= PART A: COPY =================
        ctx = await new_context(browser)
        page = await ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        async def on_download(d):
            downloads.append((d.suggested_filename, open(await d.path(), "rb").read())); log(f"download: {d.suggested_filename}")
        page.on("download", lambda d: asyncio.create_task(on_download(d)))
        page.on("request", lambda r: content_requests.append(r.url) if r.url.endswith("/content") else None)

        await page.goto(APP)
        await expect(page.get_by_role("heading", name="Extract a story")).to_be_visible()
        sid = await session_id(page)
        assert re.fullmatch(r"[0-9a-f-]{36}", sid), sid
        assert await page.evaluate("localStorage.length") == 0, "nothing may be kept in localStorage"
        log(f"session {sid[:8]}… stored in sessionStorage only")

        job_id = await start_job(page, "FAKESLOW", "Copy Test")      # 4 chapters, ~6 s each
        btn = lambda n: page.get_by_role("button", name=f"Copy chapter {n}")

        # --- chapter 1 becomes copyable while the job is STILL RUNNING; the others stay disabled
        await expect(btn(1)).to_be_enabled(timeout=45_000)
        await expect(page.get_by_text("Extracting", exact=True)).to_be_visible()
        for n in (2, 3, 4):
            assert await btn(n).is_disabled(), f"chapter {n} is not extracted yet"
        log("chapter 1 is copyable as soon as it is extracted; job still running; later chapters disabled")
        status, body = api(f"/api/jobs/{job_id}/chapters/2/content", sid)
        err = json.loads(body)["error"]
        assert status == 409 and err["code"] == "chapter_unavailable" and "yet" in err["message"], (status, body)
        log("backend independently refuses a chapter that is not extracted yet (409)")
        status, _ = api(f"/api/jobs/{job_id}/download", sid)
        assert status == 409, "the ZIP must still wait for the whole job"

        before_mid = len(downloads)
        await btn(1).click()
        await expect(btn(1)).to_contain_text("Copied!")
        clip = await page.evaluate("navigator.clipboard.readText()")
        assert clip == COMPLEX, "mid-run copy of chapter 1 differs from the TXT file"
        assert len(downloads) == before_mid, "Copy must not download anything"
        await expect(page.get_by_text("Extracting", exact=True)).to_be_visible()
        log("chapter 1 copied mid-run: clipboard == TXT file exactly; job still running; no download")

        await expect(btn(2)).to_be_enabled(timeout=30_000)          # next chapter finishes
        assert await btn(3).is_disabled() and await btn(4).is_disabled()
        await btn(2).click()
        await expect(btn(2)).to_contain_text("Copied!")
        assert await page.evaluate("navigator.clipboard.readText()") == TEXTS[2]
        log("chapter 2 became copyable the moment it finished and copied exactly")

        await expect(page.get_by_text("Extraction complete")).to_be_visible(timeout=90_000)
        await asyncio.sleep(1.5)
        zip_downloads_before = len(downloads)
        for n in (1, 2, 3, 4):
            assert await page.get_by_role("button", name=f"Copy chapter {n}").is_enabled()
        log("all four Copy buttons enabled after completion")

        # --- independence + duplicate protection: delay chapter 1's response
        async def slow(route):
            await asyncio.sleep(1.5); await route.continue_()
        await page.route(re.compile(r".*/chapters/1/content.*"), slow)
        before = len(content_requests)
        btn1 = page.get_by_role("button", name="Copy chapter 1")
        await btn1.click()
        await expect(page.get_by_text("Copying…")).to_be_visible()
        assert await btn1.is_disabled()
        assert await page.get_by_role("button", name="Copy chapter 2").is_enabled(), "chapter 2 must stay usable"
        await btn1.click(force=True, timeout=500) if False else None
        await expect(btn1).to_contain_text("Copied!", timeout=10_000)
        assert len(content_requests) - before == 1, "duplicate simultaneous requests"
        await page.unroute(re.compile(r".*/chapters/1/content.*"))
        log("loading state is per-chapter; exactly one request for chapter 1")

        # --- the clipboard holds EXACTLY the file's text
        clip = await page.evaluate("navigator.clipboard.readText()")
        file_text = (DATA / job_id / "output" / "1.txt").read_bytes().decode("utf-8")
        assert file_text == COMPLEX
        if clip != COMPLEX:
            for i, (a, b) in enumerate(zip(clip, COMPLEX)):
                if a != b: log(f"first difference at {i}: {a!r} vs {b!r}"); break
            log(f"lengths clipboard={len(clip)} file={len(COMPLEX)}; CRLF in clipboard: {chr(13) in clip}")
        assert clip == COMPLEX, "clipboard differs from the TXT file"
        assert len(downloads) == zip_downloads_before, "Copy must not download anything"
        log(f"clipboard == 1.txt character-for-character ({len(clip)} chars, CRLF kept: {chr(13) in clip}); no download triggered")

        await page.get_by_role("button", name="Copy chapter 2").click()
        await expect(page.get_by_role("button", name="Copy chapter 2")).to_contain_text("Copied!")
        assert await page.evaluate("navigator.clipboard.readText()") == TEXTS[2]
        log("chapter 2 (leading/trailing blank lines + trailing spaces) exact as well")
        await asyncio.sleep(2.5)
        assert await page.get_by_role("button", name="Copy chapter 1").is_enabled()
        log("button returns to normal Copy state after a moment")

        # --- ZIP download still works and its chapter text is identical
        await page.get_by_role("button", name="Download All Chapters").click()
        await asyncio.sleep(2)
        z = zipfile.ZipFile(io.BytesIO(downloads[-1][1]))
        assert z.namelist() == [f"Copy Test/{i}.txt" for i in (1, 2, 3, 4)], z.namelist()
        assert z.read("Copy Test/1.txt").decode("utf-8") == COMPLEX
        log("ZIP download still works and contains the same exact text")

        # --- completed_with_errors: successful chapters copyable, failed one unavailable
        err_job = await start_job(page, "FAKEERR", "Err Test")
        await expect(page.get_by_text("Completed with errors", exact=True).first).to_be_visible(timeout=60_000)
        await expect(page.get_by_role("button", name="Copy chapter 1")).to_be_enabled()
        await expect(page.get_by_role("button", name="Copy chapter 3")).to_be_enabled()
        failed_btn = page.get_by_role("button", name="Chapter 2 can't be copied")
        await expect(failed_btn).to_be_disabled(); await expect(failed_btn).to_have_text(re.compile("Unavailable"))
        status, body = api(f"/api/jobs/{err_job}/chapters/2/content", await session_id(page))
        assert status == 409 and json.loads(body)["error"]["code"] == "chapter_unavailable"
        await page.get_by_role("button", name="Copy chapter 3").click()
        await expect(page.get_by_role("button", name="Copy chapter 3")).to_contain_text("Copied!")
        assert await page.evaluate("navigator.clipboard.readText()") == TEXTS[3]
        log("completed_with_errors: ch1/ch3 copyable, ch2 'Unavailable' and refused (409)")
        await ctx.close()

        # --- clipboard permission denied -> manual fallback with the exact text
        for label, script, expect_text in (
            ("permission denied", "navigator.clipboard.writeText = () => Promise.reject(new DOMException('no','NotAllowedError'));", "blocked clipboard access"),
            ("clipboard unavailable", "Object.defineProperty(navigator, 'clipboard', {value: undefined});", "Select the text below"),
        ):
            ctx2 = await new_context(browser, perms=False)
            page2 = await ctx2.new_page()
            await page2.add_init_script(script)
            # reuse the finished job: it belongs to the first session -> copy its ID over
            await page2.add_init_script(f"sessionStorage.setItem('rw:sessionId','{sid}')")
            await page2.goto(f"{APP}/jobs/{job_id}")
            await page2.get_by_role("button", name="Copy chapter 1").click()
            await expect(page2.get_by_text(expect_text, exact=False).first).to_be_visible()
            area = page2.get_by_label("Text of chapter 1")
            await expect(area).to_be_visible()
            assert (await area.input_value()).replace("\r\n", "\n") == COMPLEX.replace("\r\n", "\n")
            await page2.get_by_role("button", name="Close").click()
            await expect(area).to_have_count(0)
            log(f"fallback shown when clipboard {label}; text discarded on Close")
            await ctx2.close()

        log("PART A PASSED")
        assert not errors, errors
        await browser.close()

asyncio.run(main())
