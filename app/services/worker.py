import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import select

from app.config import config
from app.db import Session
from app.models import Job, now
from app.schemas import Settings
from app.services.settings import get_settings
from app.services import ytdlp


def log_path(job_id: int) -> Path:
    return config.data_dir / "logs" / f"{job_id}.log"


def append_log(job_id: int, message: str):
    path = log_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Bounded disk usage; retain last 1 MB per task.
    if path.exists() and path.stat().st_size > 2_000_000:
        tail = path.read_bytes()[-1_000_000:].decode("utf-8", "replace")
        path.write_text("[предыдущие строки сокращены]\n" + tail, encoding="utf-8")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(ytdlp.sanitize_log(message).rstrip() + "\n")
    os.chmod(path, 0o600)


def update_job(job_id: int, **values):
    with Session() as session:
        job = session.get(Job, job_id)
        if job:
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = now()
            session.commit()


class Worker:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}
        self.processes = {}
        self.cancel_requested: set[int] = set()
        self.loop_task = None
        self.stopping = False

    async def start(self):
        self.stopping = False
        with Session() as session:
            interrupted = session.scalars(select(Job).where(Job.status == "running")).all()
            for job in interrupted:
                job.status = "error"
                job.error = "interrupted: сервис был остановлен во время загрузки"
                job.phase = "Прервано"
                job.speed = job.eta = None
                job.updated_at = now()
                append_log(job.id, job.error)
            session.commit()
        self.loop_task = asyncio.create_task(self.dispatch())

    async def close(self):
        self.stopping = True
        if self.loop_task:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def dispatch(self):
        while not self.stopping:
            try:
                self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
                slots = get_settings().parallel_downloads - len(self.tasks)
                if slots > 0:
                    with Session() as session:
                        pending = session.scalars(select(Job).where(Job.status == "queued").order_by(Job.id).limit(slots)).all()
                        for job in pending:
                            job.status = "running"
                            job.phase = "Подготовка"
                            job.updated_at = now()
                        session.commit()
                        for job in pending:
                            self.tasks[job.id] = asyncio.create_task(self.run(job.id))
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient DB error should not permanently stop scheduling.
                import logging
                logging.getLogger(__name__).error("Ошибка планировщика; повтор через секунду")
            await asyncio.sleep(0.75)

    async def cancel(self, job_id: int):
        self.cancel_requested.add(job_id)
        process = self.processes.get(job_id)
        if process:
            await ytdlp.stop_process(process)
        task = self.tasks.get(job_id)
        if task:
            # Wait for process cleanup before allowing retry/delete.
            await asyncio.gather(task, return_exceptions=True)
        update_job(job_id, status="canceled", phase="Отменено", speed=None, eta=None)
        append_log(job_id, "Отменено пользователем; частичные файлы сохранены для докачки")
        self.cancel_requested.discard(job_id)

    async def run(self, job_id: int):
        process = None
        last_error = ""
        try:
            if job_id in self.cancel_requested:
                return
            with Session() as session:
                job = session.get(Job, job_id)
                options = json.loads(job.options)
                url = job.url
            # Snapshot output and media preferences; use current auth/proxy to allow cookie repair.
            settings = get_settings().model_dump()
            settings.update(options["settings"])
            settings = Settings.model_validate(settings)
            ytdlp.check_dependencies(download=True)
            destination = ytdlp.safe_destination(settings, options.get("folder", ""))
            destination.mkdir(parents=True, exist_ok=True)
            args = ytdlp.download_args(url, settings, options)
            append_log(job_id, "Запуск yt-dlp. Формат: MKV; возможна докачка .part")
            process = await ytdlp.spawn(args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=1024 * 1024)
            self.processes[job_id] = process
            if job_id in self.cancel_requested:
                await ytdlp.stop_process(process)
            files = []
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                progress = ytdlp.parse_progress(line)
                if progress:
                    update_job(job_id, **progress)
                elif line.startswith(ytdlp.FILE_PREFIX):
                    try:
                        path = Path(json.loads(line[len(ytdlp.FILE_PREFIX):])).resolve()
                        if not path.is_relative_to(destination):
                            raise ValueError("Путь результата вне папки загрузок")
                        files.append(str(path))
                        update_job(job_id, files=json.dumps(files, ensure_ascii=False))
                        append_log(job_id, "Файл готов: " + path.name)
                    except (ValueError, TypeError):
                        append_log(job_id, "Некорректный путь результата yt-dlp")
                else:
                    append_log(job_id, line)
                    if "ERROR" in line:
                        last_error = ytdlp.sanitize_log(line)
            code = await process.wait()
            if job_id in self.cancel_requested:
                update_job(job_id, status="canceled", phase="Отменено", speed=None, eta=None)
            elif code:
                message = last_error or f"yt-dlp завершился с кодом {code}. Проверьте лог, cookies и свободное место"
                update_job(job_id, status="error", phase="Ошибка", error=message, speed=None, eta=None)
            elif not files:
                raise ValueError("yt-dlp завершился без файлов. Проверьте выбор эпизодов и лог")
            else:
                update_job(job_id, status="done", phase="Готово", progress=100, speed=None, eta=None)
                append_log(job_id, "Загрузка завершена")
        except asyncio.CancelledError:
            if process:
                await ytdlp.stop_process(process)
            update_job(job_id, status="error", phase="Прервано", error="interrupted: сервис остановлен", speed=None, eta=None)
            append_log(job_id, "interrupted: сервис остановлен")
            raise
        except Exception as exc:
            if process:
                await ytdlp.stop_process(process)
            message = ytdlp.sanitize_log(str(exc))
            update_job(job_id, status="error", phase="Ошибка", error=message, speed=None, eta=None)
            append_log(job_id, message)
        finally:
            self.processes.pop(job_id, None)
            if process:
                ytdlp.cleanup_process(process)
