"""A real subprocess used in tests. Never contacts kino.pub or downloads content."""
import json
from pathlib import Path
import sys
import time
import subprocess

mode, folder = sys.argv[1:]
folder = Path(folder)
if mode == "error":
    print("ERROR: test error https://cdn.test/video?token=SECRET", flush=True)
    sys.exit(1)
if mode in {"slow", "child"}:
    if mode == "child":
        folder.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (folder / "child.pid").write_text(str(child.pid))
    for index in range(600):
        print(f"KP_PROGRESS:downloading|{index}|600|NA|10|60|{index / 6:.1f}%", flush=True)
        time.sleep(0.1)
else:
    print("KP_PROGRESS:downloading|50|100|NA|50|1|50%", flush=True)
    time.sleep(0.1)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "test.mkv"
    target.write_bytes(b"FAKE MEDIA FOR TEST ONLY")
    print("KP_PROGRESS:finished|100|100|NA|NA|0|100%", flush=True)
    print("KP_FILE:" + json.dumps(str(target)), flush=True)
