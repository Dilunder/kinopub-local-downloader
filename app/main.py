import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select

from app.config import ROOT, config
from app.db import Base, engine, Session
from app.models import Job, now
from app.schemas import JobCreate, MetadataRequest, Settings
from app.services.settings import get_settings, save_settings
from app.services.security import secure_cookie_file
from app.services.worker import Worker, log_path, update_job
from app.services import ytdlp

worker = Worker()
csrf_token = secrets.token_urlsafe(32)
metadata_lock = asyncio.Lock()
job_action_lock = asyncio.Lock()


def acquire_instance_lock():
    """OS-owned advisory lock. Released automatically after crashes; no stale lock file."""
    handle = (config.data_dir / "service.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"1")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Для этой папки data уже запущен сервис. Используйте один процесс Uvicorn")
    return handle


@asynccontextmanager
async def lifespan(app: FastAPI):
    instance = acquire_instance_lock()
    try:
        Base.metadata.create_all(engine)
        (config.data_dir / "logs").mkdir(exist_ok=True)
        config.data_dir.chmod(0o700)
        # Only remove our credential snapshots after acquiring the exclusive lock.
        for stale in (config.data_dir / "tmp").glob("cookies-*.txt"):
            stale.unlink(missing_ok=True)
        app.state.health_at_startup = ytdlp.health()
        await worker.start()
        yield
    finally:
        await worker.close()
        instance.close()


app = FastAPI(title="KinoPub Local Downloader", lifespan=lifespan, docs_url="/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "app" / "templates")


@app.middleware("http")
async def localhost_guard(request: Request, call_next):
    host = request.headers.get("host", "")
    try:
        host_url = urlsplit("http://" + host)
        valid_host = host_url.hostname == "127.0.0.1" and not host_url.username and not host_url.password
        host_url.port
    except ValueError:
        valid_host = False
    peer = request.client.host if request.client else ""
    if not valid_host or peer not in {"127.0.0.1", "testclient"}:
        return JSONResponse({"detail": "Сервис доступен только через 127.0.0.1"}, status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != "http://" + host):
            return JSONResponse({"detail": "Запрос с другого сайта запрещён"}, status_code=403)
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf_token):
            return JSONResponse({"detail": "Нет CSRF-токена. Обновите страницу"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({"detail": "; ".join(error["msg"].removeprefix("Value error, ") for error in exc.errors())}, status_code=422)


def require_job(job_id):
    with Session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        return job


def job_dict(job):
    return {key: getattr(job, key) for key in (
        "id", "url", "title", "status", "progress", "downloaded", "total", "speed", "eta",
        "phase", "error", "created_at", "updated_at"
    )} | {"files": json.loads(job.files)}


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"csrf_token": csrf_token})


@app.get("/settings")
async def settings_page(request: Request):
    return templates.TemplateResponse(request=request, name="settings.html", context={"csrf_token": csrf_token})


@app.get("/api/health")
async def health():
    return ytdlp.health()


@app.get("/partials/health")
async def health_partial(request: Request):
    state = ytdlp.health()
    checks = [("yt-dlp", state["yt_dlp"]), ("Плагин KinoPub", state["plugin"] and state["plugin_loadable"]), ("ffmpeg", state["ffmpeg"])]
    return templates.TemplateResponse(request=request, name="health.html", context={"state": state, "checks": checks})


@app.get("/api/settings")
async def read_settings():
    return get_settings().model_dump()


@app.put("/api/settings")
async def write_settings(settings: Settings):
    try:
        Path(settings.download_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        raise HTTPException(422, "Не удалось создать папку загрузок. Проверьте путь и права")
    save_settings(settings)
    return settings.model_dump()


@app.post("/api/settings/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read(1_048_577)
    await file.close()
    if len(content) > 1_048_576:
        raise HTTPException(413, "Файл cookies слишком большой (лимит 1 МБ)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise HTTPException(422, "Нужен UTF-8 cookies.txt в Netscape-формате")
    if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise HTTPException(422, "Неверный формат: нужен Netscape cookies.txt")
    # Store only cookies belonging to approved sites, never all browser credentials.
    lines = ["# Netscape HTTP Cookie File"]
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = line.split("\t")
        domain = fields[0].removeprefix("#HttpOnly_").lstrip(".").lower()
        if domain not in {"kino.pub", "kino.watch", "www.kino.pub", "www.kino.watch"}:
            continue
        if len(fields) != 7 or fields[1] not in {"TRUE", "FALSE"} or fields[3] not in {"TRUE", "FALSE"} or not fields[4].isdigit():
            raise HTTPException(422, "Повреждённая запись cookies.txt")
        lines.append(line)
    if len(lines) == 1:
        raise HTTPException(422, "В файле нет cookies kino.pub / kino.watch")
    path = config.data_dir / "cookies.txt"
    temp = config.data_dir / ("cookies-" + secrets.token_hex(8) + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            secure_cookie_file(temp)
            stream.write("\n".join(lines) + "\n")
        os.replace(temp, path)
    except (OSError, ValueError):
        raise HTTPException(422, "Не удалось безопасно сохранить cookies.txt. Проверьте права папки data")
    finally:
        temp.unlink(missing_ok=True)
    return {"ok": True, "message": "cookies.txt сохранён; выберите источник «Файл» и сохраните настройки"}


@app.post("/api/metadata")
async def metadata(body: MetadataRequest):
    if metadata_lock.locked():
        raise HTTPException(409, "Уже запрашивается информация. Дождитесь результата")
    async with metadata_lock:
        try:
            return await ytdlp.get_metadata(body.url, get_settings())
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except OSError:
            raise HTTPException(503, "Не удалось запустить yt-dlp")


@app.post("/api/jobs", status_code=201)
async def create_job(body: JobCreate):
    current = get_settings().model_dump()
    current.update(body.model_dump(include={"quality", "audio", "subtitles", "filename_template"}, exclude_none=True))
    try:
        selected = Settings.model_validate(current)
        ytdlp.safe_destination(selected, body.folder)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc))
    snapshot = selected.model_dump(include={"download_dir", "quality", "audio", "subtitles", "filename_template"})
    with Session() as session:
        job = Job(url=body.url, title=body.title or body.url, options=json.dumps({"settings": snapshot, "folder": body.folder, "episodes": body.episodes}, ensure_ascii=False))
        session.add(job)
        session.commit()
        return job_dict(job)


@app.get("/api/jobs")
async def list_jobs(status: str | None = None, limit: int = 100, offset: int = 0):
    if status and status not in {"queued", "running", "done", "error", "canceled"}:
        raise HTTPException(422, "Неизвестный статус")
    with Session() as session:
        query = select(Job).order_by(Job.id.desc()).limit(max(1, min(limit, 500))).offset(max(offset, 0))
        if status:
            query = query.where(Job.status == status)
        return [job_dict(job) for job in session.scalars(query)]


@app.get("/api/jobs/{job_id}")
async def read_job(job_id: int):
    return job_dict(require_job(job_id))


@app.get("/api/jobs/{job_id}/log")
async def read_log(job_id: int):
    require_job(job_id)
    path = log_path(job_id)
    if not path.exists():
        return {"log": "Лог появится после запуска задачи"}
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 64_000))
        content = stream.read().decode("utf-8", "replace")
    return {"log": content}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    async with job_action_lock:
        job = require_job(job_id)
        if job.status not in {"queued", "running"}:
            raise HTTPException(409, "Отменять можно только ожидающую или активную задачу")
        # Persist before await so dispatcher cannot start a queued canceled task.
        update_job(job_id, status="canceled", phase="Отмена")
        await worker.cancel(job_id)
        return job_dict(require_job(job_id))


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: int):
    async with job_action_lock:
        job = require_job(job_id)
        if job.status not in {"error", "canceled"} or job_id in worker.processes:
            raise HTTPException(409, "Повтор доступен после ошибки или отмены")
        update_job(job_id, status="queued", phase="Ожидание", error=None, progress=0, downloaded=0, total=None, speed=None, eta=None, files="[]")
        return job_dict(require_job(job_id))


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int):
    async with job_action_lock:
        job = require_job(job_id)
        if job.status in {"queued", "running"} or job_id in worker.processes:
            raise HTTPException(409, "Сначала отмените задачу")
        with Session() as session:
            session.delete(session.get(Job, job_id))
            session.commit()
        log_path(job_id).unlink(missing_ok=True)
        return {"ok": True, "message": "Запись и лог удалены. Скачанные файлы сохранены"}


@app.post("/api/jobs/{job_id}/open-folder")
async def open_folder(job_id: int):
    job = require_job(job_id)
    options = json.loads(job.options)
    settings = Settings.model_validate(get_settings().model_dump() | options["settings"])
    folder = ytdlp.safe_destination(settings, options.get("folder", ""))
    if not folder.is_dir():
        raise HTTPException(404, "Папка ещё не создана")
    try:
        if os.name == "nt":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise OSError()
    except OSError:
        raise HTTPException(422, "Открытие папки не поддерживается в этой среде")
    return {"ok": True}
