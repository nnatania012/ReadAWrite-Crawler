"""Stand-in for pipeline.py: same command line, writes numbered files + pipeline_state.json."""
import argparse, hashlib, json, os, sys, time
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0, os.path.dirname(__file__))
from texts import TEXTS

parser = argparse.ArgumentParser()
parser.add_argument("source"); parser.add_argument("-o", "--output"); parser.add_argument("--stop-file")
args = parser.parse_args()
out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
stop = Path(args.stop_file) if args.stop_file else None
name = urlparse(args.source).path.rstrip("/").split("/")[-1]
PLANS = {  # chapter -> (outcome, delay)
    "FAKEOK":   {1: ("success", 1.5), 2: ("success", 1.5), 3: ("success", 1.5)},
    "FAKEERR":  {1: ("success", 1.0), 2: ("failed", 1.0), 3: ("success", 1.0)},
    "FAKESLOW": {1: ("success", 6), 2: ("success", 6), 3: ("success", 6), 4: ("success", 6)},
}
plan = PLANS.get(name, PLANS["FAKEOK"])
data = {"version": 1, "chapter_list_hash": "x", "total_chapters": len(plan), "chapters": {
    str(i): {"url": f"https://www.readawrite.com/c/{i:032x}", "status": "pending", "output": None, "attempts": 0}
    for i in plan}}

def save():
    tmp = out / "pipeline_state.json.tmp"; tmp.write_text(json.dumps(data)); os.replace(tmp, out / "pipeline_state.json")

time.sleep(1.0); save()
for i, (outcome, delay) in plan.items():
    if stop and stop.exists():
        print("Stop file found"); sys.exit(130)
    entry = data["chapters"][str(i)]; entry.update(status="in_progress", attempts=1); save()
    time.sleep(delay)
    if outcome == "success":
        raw = TEXTS.get(i, f"chapter {i}\n").encode("utf-8")
        (out / f"{i}.txt").write_bytes(raw)
        entry.update(status="success", output=f"{i}.txt", format="Chat", entry_count=3,
                     output_bytes=len(raw), output_sha256=hashlib.sha256(raw).hexdigest())
    else:
        entry.update(status="failed", attempts=3, error="Timeout: page.goto at /home/user/pipeline.py")
    save()
sys.exit(0 if all(e["status"] == "success" for e in data["chapters"].values()) else 2)
