"""Offline smoke test of the actual frozen EXE, with no Python/ffmpeg on PATH."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent


def public_report(health, job):
    """Release assets contain only check results, never local paths or account data."""
    return {
        "result": "PASS",
        "app": health.get("app"), "version": health.get("version"),
        "yt_dlp": health.get("yt_dlp"), "plugin": health.get("plugin"),
        "checks": {"dependencies_ready": bool(health.get("ok")),
                   "plugin_loadable": bool(health.get("plugin_loadable")),
                   "pages": True, "metadata": True, "cyrillic_filename": True,
                   "temporary_cookies_cleaned": True},
        "download": {"status": job.get("status"), "progress": job.get("progress"),
                     "downloaded_bytes": job.get("downloaded"), "file_count": len(job.get("files", [])),
                     "format": "mkv"},
        "python_on_path": False, "system_ffmpeg_on_path": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, default=ROOT / "outputs" / "KinoPub-Local-Downloader.exe")
    args = parser.parse_args()
    qa = ROOT / "work" / ("exe-qa-" + str(time.time_ns()))
    qa.mkdir(parents=True)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = os.environ.copy()
    env.update(KP_DATA_DIR=str(qa / "data"), KP_DOWNLOAD_DIR=str(qa / "downloads"), KP_DEV_MODE="true",
               PATH=str(Path(os.environ["SystemRoot"]) / "System32"))
    log = (qa / "process.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(args.exe.resolve()), "--headless", "--no-browser", "--port", str(port)],
                               env=env, cwd=qa, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    media_server = None
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=60) as client:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("EXE exited early: " + (qa / "process.log").read_text(encoding="utf-8", errors="replace"))
                try:
                    health = client.get("/api/health").json()
                    break
                except (httpx.HTTPError, ValueError):
                    time.sleep(0.2)
            else:
                raise RuntimeError("EXE startup timed out")
            assert health["ok"] and health["plugin_loadable"], health
            assert "_MEI" in health["ffmpeg"], "FFmpeg was not loaded from the executable"
            import re
            page = client.get("/")
            assert page.status_code == 200
            client.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
            assert client.get("/settings").status_code == 200
            assert client.get("/static/app.js").status_code == 200
            settings = client.get("/api/settings").json()
            settings["auth_source"] = "file"
            cookies = "# Netscape HTTP Cookie File\n.kino.pub\tTRUE\t/\tTRUE\t2147483647\tqa\tFAKE\n"
            result = client.post("/api/settings/cookies", files={"file": ("cookies.txt", cookies)})
            assert result.status_code == 200, result.text
            assert client.put("/api/settings", json=settings).status_code == 200
            fixture = qa / "Тестовое видео.mp4"
            subprocess.run([health["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=0x293623:s=320x180:d=2", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(fixture)], check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)

            class Handler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass

            media_server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(qa)))
            threading.Thread(target=media_server.serve_forever, daemon=True).start()
            from urllib.parse import quote
            url = f"http://127.0.0.1:{media_server.server_port}/" + quote(fixture.name)
            metadata = client.post("/api/metadata", json={"url": url})
            if metadata.status_code != 200:
                debug = subprocess.run([str(args.exe.resolve()), "--internal-ytdlp", "--ignore-config", "--no-cache-dir", "--verbose",
                                        "--dump-single-json", "--skip-download", "--cookies", str(qa / "data" / "cookies.txt"), "--", url],
                                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                (qa / "ytdlp-debug.log").write_text(debug.stderr, encoding="utf-8")
                print("Frozen CLI debug (local fixture only):", debug.stderr[-6500:])
            assert metadata.status_code == 200, metadata.text
            assert "Тестовое" in metadata.json()["title"], "Cyrillic metadata was not decoded as UTF-8"
            response = client.post("/api/jobs", json={"url": url, "title": "Frozen EXE smoke test"})
            assert response.status_code == 201, response.text
            job_id = response.json()["id"]
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in {"done", "error"}:
                    break
                time.sleep(0.2)
            assert job["status"] == "done", (job, client.get(f"/api/jobs/{job_id}/log").json())
            output = Path(job["files"][0])
            assert output.suffix == ".mkv" and output.stat().st_size > 1000
            assert "Тестовое" in output.name, "Cyrillic output path was corrupted"
            assert not list((qa / "data" / "tmp").glob("cookies-*.txt"))
            report = public_report(health, job)
            (ROOT / "outputs" / "windows-exe-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("PASS: packaged EXE pages, health, cookies, real yt-dlp subprocess, progress and MKV; QA:", qa)
    finally:
        if media_server:
            media_server.shutdown()
            media_server.server_close()
        try:
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            parent.terminate()
            psutil.wait_procs(children + [parent], timeout=10)
        except psutil.Error:
            pass
        log.close()


if __name__ == "__main__":
    main()
