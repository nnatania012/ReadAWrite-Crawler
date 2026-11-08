# ReadAWrite Chapter Extraction Pipeline

A set of Python scripts that turn a ReadAWrite chapter-list page into numbered plain-text files, one per chapter, with a single command:

```bash
python pipeline.py chapters.html
```

It fetches each chapter in a real browser (Playwright + Chromium) so the site's own JavaScript can load and decrypt the content, detects whether the chapter is **Chat** or **Lec** format, extracts the text, and saves it as `1.txt`, `2.txt`, `3.txt`, and so on. Runs are resumable, so you can stop at any point and pick up where you left off.

> **Personal use only.** This tool is for reading and archiving content you already have legitimate access to. It does not log in, solve CAPTCHAs, or work around access restrictions, and it will stop if the site denies access. Please respect ReadAWrite's terms of service and the authors' rights, and keep the request rate low (the pipeline already pauses between chapters).

## How it works

```text
chapters.html
     |
     v
chapext.py   -> extract every chapter URL (in order, no duplicates)
     |
     v   (one chapter at a time)
rwfetch.py   -> load chapter in Chromium, wait for decryption, expand all bubbles
     |
     v
rwext.py     -> detect Chat or Lec, extract text
     |
     v
N.txt        -> verified, atomically written, recorded in the manifest
```

| File | Role |
|---|---|
| `pipeline.py` | Orchestrates everything: output folder, retries, resume, logging, progress report. |
| `chapext.py` | Extracts chapter URLs from a saved chapter-list HTML file. Can also be run on its own. |
| `rwfetch.py` | Playwright fetcher. Returns `SUCCESS`, `PARTIAL` or `FAILED` for each chapter. Can also be run on its own. |
| `rwext.py` | Extracts text from rendered chapter HTML, with automatic Chat/Lec detection. Can also be run on its own. |

`pipeline.py` imports the other three, so **all four files must be in the same folder.**

## Requirements

- Python 3.10 or newer
- Packages: `beautifulsoup4`, `requests`, `playwright`
- Chromium, installed through Playwright

```bash
pip install beautifulsoup4 requests playwright
python -m playwright install chromium
```

## Usage

1. Open the story's chapter-list page in your browser and save it as HTML (for example `chapters.html`).
2. Put it in the same folder as the scripts, then run:

```bash
python pipeline.py chapters.html
```

No other arguments are needed. Chat or Lec format, output folder, filenames, retries and resume are all handled automatically.

### Output

Output goes in a folder next to the input file, named after it (`chapters.html` becomes `chapters_output/`):

```text
chapters_output/
├── 1.txt, 2.txt, 3.txt ...   extracted chapters (original chapter numbers)
├── input.html                working copy of the last fetched chapter
├── last_partial.html         last chapter that rendered only partially (for debugging)
├── pipeline_state.json       progress manifest used for resuming
└── logs/
    └── pipeline.log          detailed log
```

Chapter numbers always follow the order of the chapter list. If chapter 3 fails, chapter 4 is still saved as `4.txt`.

### Retry and resume

- Each chapter gets up to 3 attempts, with short waits between them.
- If Chromium crashes, it is restarted and the chapter is retried.
- A chapter that is still failing after 3 attempts is recorded as `failed` or `partial`, and the run moves on to the next one.
- A `PARTIAL` fetch (page saved but full rendering could not be confirmed) is never turned into a text file.
- Run the same command again to resume. A chapter is skipped only if the manifest says it succeeded, its URL matches, and its `.txt` file still has the recorded size and checksum. Everything else is retried.
- If the chapter list changes (for example, new chapters are added), the pipeline treats it as a different story and writes to a separate `<name>_output_<hash>/` folder instead of mixing results.
- Press **Ctrl+C** once to finish the current chapter and stop cleanly. Press it twice to abort immediately. Completed chapters are kept either way.
- If the site refuses access to three chapters in a row (login or permission required), the pipeline stops rather than keep sending blocked requests.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every chapter is complete |
| 1 | Input or setup error (nothing was processed) |
| 2 | Finished, but some chapters failed, are partial, or were not processed |
| 130 | Interrupted with Ctrl+C |

## Using the scripts on their own

The original command-line tools still work individually:

```bash
# Extract chapter URLs
python chapext.py chapters.html -o chapter_links.txt

# Fetch one chapter's rendered HTML
python rwfetch.py "https://www.readawrite.com/c/CHAPTER_ID" -o chapter.html

# Extract text from rendered HTML (Chat/Lec detected automatically)
python rwext.py chapter.html
python rwext.py chapter.html --pair            # Chat: "name: message" on one line
python rwext.py chapter.html --by-paragraph    # Lec: join spans per paragraph
```

The pipeline always uses the default extraction behavior and does not pass `--pair`, `--keep-blank`, `--by-paragraph` or `--blank-between`.

## Known limitations

- **Lec chapter fetching is less proven than Chat.** For Lec chapters, `rwfetch.py` treats the page as ready once the highlighted chapter text stops changing for about two seconds. If real Lec pages load differently, you may see `PARTIAL` results and need to adjust `_wait_for_stable_lec` in `rwfetch.py`.
- **Depends on ReadAWrite's current page structure.** The selectors and JavaScript hooks (`window.allnum`, `openAllBubble()`, `errorcode`) may break if the site changes.
- **Chapters that require a login are not supported.** The pipeline reports them as failed and does not try to sign in.
- **No concurrency.** Chapters are processed strictly one at a time, by design.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `Import error` on start | All four `.py` files must be in the same folder, and the dependencies must be installed. |
| `Could not launch Chromium` | Run `python -m playwright install chromium`. |
| `No chapter links were found` | The saved HTML needs to contain a supported chapter-list structure (see `CHAPTER_ROW_SELECTORS` in `chapext.py`). |
| Many `partial` chapters | Check `logs/pipeline.log` and `last_partial.html` to see what rendered. |
| Chapter says Chat and Lec detected together | The page contained both structures. Try that chapter manually with `rwext.py --Chat` or `--Lec`. |

## Suggested `.gitignore`

Keep fetched content and logs out of the repository:

```gitignore
*_output/
*_output_*/
chapters.html
*.html
__pycache__/
*.pyc
```

## License

Private project. No license is granted.
