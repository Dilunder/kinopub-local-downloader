"""Manual integration QA: actual yt-dlp + ffmpeg against generated local media."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
qa = root / "work" / f"real-pipeline-{time.time_ns()}"
qa.mkdir(parents=True, exist_ok=True)
os.environ["KP_DATA_DIR"] = str(qa / "data")
os.environ["KP_DOWNLOAD_DIR"] = str(qa / "downloads")
os.environ["KP_DEV_MODE"] = "true"
fixture = qa / "fixture.mp4"
subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=0x293623:s=640x360:d=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(fixture)], check=True)

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(qa)))
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    from fastapi.testclient import TestClient
    from app.main import app, csrf_token

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.headers["X-CSRF-Token"] = csrf_token
        cookie = "# Netscape HTTP Cookie File\n.kino.pub\tTRUE\t/\tTRUE\t2000000000\tqa_only\tnot-a-real-credential\n"
        result = client.post("/api/settings/cookies", files={"file": ("cookies.txt", cookie)})
        assert result.status_code == 200, result.text
        settings = client.get("/api/settings").json()
        settings.update(auth_source="file", quality="best")
        assert client.put("/api/settings", json=settings).status_code == 200
        health = client.get("/api/health").json()
        assert health["ok"], health
        url = f"http://127.0.0.1:{server.server_port}/fixture.mp4"
        metadata = client.post("/api/metadata", json={"url": url})
        assert metadata.status_code == 200, metadata.text
        job_response = client.post("/api/jobs", json={"url": url, "title": "Local generated fixture", "folder": "verified"})
        assert job_response.status_code == 201, job_response.text
        job_id = job_response.json()["id"]
        for _ in range(150):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"done", "error"}:
                break
            time.sleep(0.2)
        assert job["status"] == "done", (job, client.get(f"/api/jobs/{job_id}/log").json())
        output = Path(job["files"][0])
        assert output.suffix == ".mkv" and output.stat().st_size > 1000
        assert not list((qa / "data" / "tmp").glob("cookies-*.txt")), "Credential snapshots were not cleaned up"
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(output)], check=True, capture_output=True, text=True)
        report = {"health": health, "metadata": metadata.json(), "job": job,
                  "probe": json.loads(probe.stdout), "result": "PASS: actual yt-dlp metadata, progress, download and ffmpeg remux"}
        (qa / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("QA artifacts:", qa)
        print(json.dumps(report, ensure_ascii=False, indent=2))
finally:
    server.shutdown()
    server.server_close()
