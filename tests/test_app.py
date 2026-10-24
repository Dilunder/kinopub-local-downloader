import asyncio
import json
from pathlib import Path
import sys
import time
import psutil

from fastapi.testclient import TestClient

from app.config import config
from app.db import Session
from app.main import app, csrf_token, worker
from app.models import Job
from app.services import ytdlp


def wait_for(client, job_id, statuses, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"Timed out: {job}")


def install_fake(monkeypatch, mode="done"):
    monkeypatch.setattr(ytdlp, "check_dependencies", lambda **kwargs: None)
    monkeypatch.setattr(ytdlp, "download_args", lambda url, settings, options: [
        sys.executable, str(Path(__file__).with_name("fake_downloader.py")), mode,
        str(ytdlp.safe_destination(settings, options.get("folder", "")))])


def test_import_health_and_pages(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert {"ffmpeg", "yt_dlp", "plugin", "plugin_loadable", "ok"} <= response.json().keys()
    for page in ("/", "/settings", "/?view=history", "/partials/health", "/static/app.js"):
        assert client.get(page).status_code == 200
    assert "csrf-token" in client.get("/").text


def test_localhost_csrf_and_origin(client):
    assert client.get("/api/settings", headers={"host": "evil.test"}).status_code == 403
    assert client.get("/api/settings", headers={"host": "127.0.0.1.evil.test"}).status_code == 403
    assert client.post("/api/jobs", json={"url": "https://kino.pub/item/view/1"}, headers={"x-csrf-token": "wrong"}).status_code == 403
    assert client.post("/api/jobs", json={}, headers={"origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/jobs", json={}, headers={"sec-fetch-site": "cross-site"}).status_code == 403


def test_settings_and_cookies_persist(client):
    settings = client.get("/api/settings").json()
    settings.update(auth_source="file", parallel_downloads=2, quality="720")
    assert client.put("/api/settings", json=settings).status_code == 200
    assert client.get("/api/settings").json()["parallel_downloads"] == 2
    cookies = "# Netscape HTTP Cookie File\n.kino.pub\tTRUE\t/\tTRUE\t2000000000\tsession\tSECRET\n.evil.test\tTRUE\t/\tTRUE\t2000000000\tsession\tOTHER_SECRET\n"
    response = client.post("/api/settings/cookies", files={"file": ("cookies.txt", cookies)})
    assert response.status_code == 200
    saved = (config.data_dir / "cookies.txt").read_text()
    assert "SECRET" in saved and "OTHER_SECRET" not in saved
    assert "SECRET" not in response.text
    assert "SECRET" not in client.get("/api/settings").text
    assert client.post("/api/settings/cookies", files={"file": ("x.txt", "bad format")}).status_code == 422


def test_real_subprocess_progress_done_and_delete_keeps_file(client, monkeypatch):
    install_fake(monkeypatch)
    response = client.post("/api/jobs", json={"url": "https://kino.pub/item/view/1", "title": "Фильм", "folder": "success"})
    assert response.status_code == 201
    job = wait_for(client, response.json()["id"], {"done", "error"})
    assert job["status"] == "done", job
    assert job["progress"] == 100 and job["downloaded"] == 100
    path = Path(job["files"][0]); assert path.exists()
    assert "Загрузка завершена" in client.get(f'/api/jobs/{job["id"]}/log').json()["log"]
    assert len(client.get("/api/jobs?status=done").json()) == 1
    assert client.delete(f'/api/jobs/{job["id"]}').status_code == 200
    assert path.exists()


def test_real_subprocess_cancel_retry(client, monkeypatch):
    install_fake(monkeypatch, "slow")
    job_id = client.post("/api/jobs", json={"url": "https://kino.pub/item/view/1", "folder": "cancel"}).json()["id"]
    wait_for(client, job_id, {"running"})
    deadline = time.monotonic() + 5
    while job_id not in worker.processes and time.monotonic() < deadline:
        time.sleep(0.03)
    process = worker.processes[job_id]
    assert client.delete(f"/api/jobs/{job_id}").status_code == 409
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert process.returncode is not None
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "canceled"
    install_fake(monkeypatch, "done")
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert wait_for(client, job_id, {"done", "error"})["status"] == "done"


def test_error_and_log_redacted(client, monkeypatch):
    install_fake(monkeypatch, "error")
    job_id = client.post("/api/jobs", json={"url": "https://kino.pub/item/view/1"}).json()["id"]
    job = wait_for(client, job_id, {"error"})
    assert "SECRET" not in job["error"]
    assert "SECRET" not in client.get(f"/api/jobs/{job_id}/log").text


def test_interrupted_recovery_and_queue_survives(monkeypatch):
    install_fake(monkeypatch)
    with Session() as session:
        running = Job(url="https://kino.pub/item/view/1", title="Interrupted", status="running")
        pending = Job(url="https://kino.pub/item/view/2", title="Pending", options=json.dumps({"settings": {}, "folder": "restart", "episodes": []}))
        session.add_all([running, pending]); session.commit()
        running_id, pending_id = running.id, pending.id
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        interrupted = client.get(f"/api/jobs/{running_id}").json()
        assert interrupted["status"] == "error" and "interrupted" in interrupted["error"]
        assert wait_for(client, pending_id, {"done", "error"})["status"] == "done"


def test_metadata_api_normalized(client, monkeypatch):
    async def fake_metadata(url, settings):
        return {"title": "Название", "kind": "movie", "episodes": [], "qualities": [720], "audio": [], "subtitles": []}
    monkeypatch.setattr(ytdlp, "get_metadata", fake_metadata)
    result = client.post("/api/metadata", json={"url": "https://kino.pub/item/view/1"})
    assert result.status_code == 200 and result.json()["title"] == "Название"
    assert client.post("/api/metadata", json={"url": "https://evil.test"}).status_code == 422


def test_bad_options_do_not_create_job(client):
    assert client.post("/api/jobs", json={"url": "https://kino.pub/item/view/1", "folder": "../escape"}).status_code == 422
    assert client.post("/api/jobs", json={"url": "https://kino.pub/item/view/1", "audio": "x;token=secret"}).status_code == 422
    assert client.get("/api/jobs").json() == []


def test_parallel_limit_and_reduction(client, monkeypatch):
    install_fake(monkeypatch, "slow")
    settings = client.get("/api/settings").json()
    settings["parallel_downloads"] = 2
    assert client.put("/api/settings", json=settings).status_code == 200
    ids = [client.post("/api/jobs", json={"url": f"https://kino.pub/item/view/{index}"}).json()["id"] for index in [1, 2, 3]]
    wait_for(client, ids[0], {"running"})
    wait_for(client, ids[1], {"running"})
    assert client.get(f"/api/jobs/{ids[2]}").json()["status"] == "queued"
    settings["parallel_downloads"] = 1
    assert client.put("/api/settings", json=settings).status_code == 200
    client.post(f"/api/jobs/{ids[0]}/cancel")
    time.sleep(1)
    assert client.get(f"/api/jobs/{ids[2]}").json()["status"] == "queued"
    client.post(f"/api/jobs/{ids[1]}/cancel")
    wait_for(client, ids[2], {"running"})
    assert client.post(f"/api/jobs/{ids[2]}/cancel").status_code == 200


def test_cancel_kills_child_process(client, monkeypatch):
    install_fake(monkeypatch, "child")
    folder = Path(config.download_dir) / "child-process"
    job_id = client.post("/api/jobs", json={"url": "https://kino.pub/item/view/1", "folder": "child-process"}).json()["id"]
    deadline = time.monotonic() + 5
    while not (folder / "child.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child_pid = int((folder / "child.pid").read_text())
    assert psutil.pid_exists(child_pid)
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    if psutil.pid_exists(child_pid):
        process = psutil.Process(child_pid)
        assert not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
