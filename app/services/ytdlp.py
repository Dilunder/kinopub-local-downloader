"""CLI-only yt-dlp integration. No shell, user config, credentials or raw metadata in logs."""
import asyncio
import importlib.metadata
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import secrets
import subprocess
import sys

import psutil

from app.config import config
from app.version import APP_ID, VERSION
from app.schemas import Settings, relative_folder, validate_url
from app.services.security import secure_cookie_file

PROGRESS_PREFIX = "KP_PROGRESS:"
FILE_PREFIX = "KP_FILE:"
PROGRESS_TEMPLATE = (
    "download:" + PROGRESS_PREFIX
    + "%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|%(progress._percent_str)s"
)


class YtdlpError(ValueError):
    def __init__(self, message: str, code: str = "ytdlp_error"):
        super().__init__(message)
        self.code = code


def explain_error(output: str, settings: Settings) -> YtdlpError:
    lowered = output.lower()
    browser = {"edge": "Microsoft Edge", "chrome": "Google Chrome", "firefox": "Firefox"}.get(settings.browser, settings.browser)
    if "could not copy chrome cookie database" in lowered:
        return YtdlpError(
            f"{browser} заблокировал свою базу cookies. Не удалось прочитать сессию браузера.\n"
            "В настройках раскройте «Вставить Cookie из браузера» или загрузите свой Netscape cookies.txt — браузер можно оставить открытым.\n"
            "Для автоматического чтения cookies полностью закройте выбранный браузер, а интерфейс откройте в другом.",
            "cookies_locked")
    if any(part in lowered for part in ("failed to decrypt", "app-bound", "app bound", "unsupported cookie version", "cannot decrypt", "pycryptodomex", "no module named 'cryptodome'")):
        return YtdlpError(
            f"Не удалось расшифровать cookies {browser}. Защита Windows/браузера может запрещать чтение сессии сторонним процессом.\n"
            "В настройках используйте «Вставить Cookie из браузера» или собственный Netscape cookies.txt.",
            "cookies_decryption")
    if "cookies database" in lowered and ("could not find" in lowered or "not found" in lowered):
        return YtdlpError(f"Не найден профиль cookies {browser}. Войдите на сайт в этом браузере либо импортируйте Cookie в настройках.", "cookies_missing")
    if "cloudflare" in lowered or "http error 403" in lowered:
        return YtdlpError("Сайт отклонил запрос. Проверьте, что видео открывается в вашем браузере, и обновите Cookie вместе с User-Agent в настройках. Автоматический обход проверки сайта не выполняется.", "cookies_rejected")
    lines = list(dict.fromkeys(sanitize_log(line) for line in output.splitlines() if line.strip()))
    return YtdlpError("yt-dlp не смог обработать видео. " + "\n".join(lines[-8:]))


def health() -> dict:
    def package(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    plugin = package("yt-dlp-kinopub")
    try:
        module = importlib.import_module("yt_dlp_plugins.extractor.kinopub")
        plugin_loadable = hasattr(module, "KinoPubIE")
    except (ImportError, ValueError, SyntaxError):
        plugin_loadable = False
    data = {
        "app": APP_ID, "version": VERSION,
        "ffmpeg": shutil.which("ffmpeg"), "yt_dlp": package("yt-dlp"),
        "plugin": plugin, "plugin_loadable": plugin_loadable,
        "cookies_file": (config.data_dir / "cookies.txt").is_file(),
        "dev_mode": config.dev_mode,
    }
    data["ok"] = bool(data["ffmpeg"] and data["yt_dlp"] and plugin and plugin_loadable)
    return data


def check_dependencies(download=False):
    state = health()
    if not state["yt_dlp"]:
        raise ValueError("Не установлен yt-dlp: pip install -r requirements.txt")
    if not state["plugin"] or not state["plugin_loadable"]:
        raise ValueError("Не установлен или не загружается yt-dlp-kinopub")
    if download and not state["ffmpeg"]:
        raise ValueError("Не найден ffmpeg в PATH. Установите ffmpeg и перезапустите сервис")


def auth_args(settings: Settings) -> list[str]:
    if settings.auth_source == "browser":
        return ["--cookies-from-browser", settings.browser]
    cookies = config.data_dir / "cookies.txt"
    if not cookies.is_file():
        raise ValueError("Нет cookies.txt. Загрузите файл на странице настроек")
    secure_cookie_file(cookies)
    return ["--cookies", str(cookies)]


def cli_prefix() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-ytdlp"]
    return [sys.executable, "-m", "yt_dlp"]


def base_args(settings: Settings) -> list[str]:
    args = cli_prefix() + ["--ignore-config", "--no-cache-dir", "--no-colors", "--socket-timeout", "30", "--retries", "3"]
    args += auth_args(settings)
    if settings.user_agent:
        args += ["--user-agent", settings.user_agent]
    if settings.proxy:
        args += ["--proxy", settings.proxy]
    # Only this extractor is allowed outside explicit developer mode.
    if not config.dev_mode:
        args += ["--use-extractors", "kinopub"]
    return args


def metadata_args(url: str, settings: Settings) -> list[str]:
    return base_args(settings) + ["--dump-single-json", "--skip-download", "--no-warnings", "--", validate_url(url)]


def safe_destination(settings: Settings, folder: str) -> Path:
    root = Path(settings.download_dir).resolve()
    destination = (root / relative_folder(folder)).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("Папка выходит за пределы корневой папки загрузок")
    return destination


def download_args(url: str, settings: Settings, options: dict) -> list[str]:
    destination = safe_destination(settings, options.get("folder", ""))
    quality = settings.quality
    video = "bv" + (f"[height<={quality}]" if quality != "best" else "")
    combined = "b" + (f"[height<={quality}]" if quality != "best" else "")
    args = base_args(settings) + [
        "--newline", "--progress", "--no-simulate", "--no-quiet", "--progress-delta", "0.5",
        "--progress-template", PROGRESS_TEMPLATE,
        "--print", "after_move:" + FILE_PREFIX + "%(filepath)j",
        "--paths", str(destination), "--output", settings.filename_template,
        "--windows-filenames", "--trim-filenames", "180", "--no-overwrites",
        "--merge-output-format", "mkv", "--remux-video", "mkv",
        "--format", f"{video}+ba/{combined}",
    ]
    # The plugin ranks a studio/language preference consistently across episodes.
    if settings.audio:
        args += ["--extractor-args", "kinopub:audio=" + settings.audio]
    if settings.subtitles:
        args += ["--write-subs", "--embed-subs", "--sub-langs", settings.subtitles]
    episodes = options.get("episodes", [])
    if episodes:
        args += ["--playlist-items", ",".join(str(i) for i in episodes)]
    return args + ["--", validate_url(url)]


def number(value):
    try:
        result = float(str(value).strip().strip("%"))
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def parse_progress(line: str) -> dict | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    parts = line[len(PROGRESS_PREFIX):].strip().split("|")
    if len(parts) != 7:
        return None
    status, downloaded, total, estimate, speed, eta, percent = parts
    downloaded = number(downloaded)
    total = number(total) or number(estimate)
    progress = number(percent)
    if progress is None:
        progress = downloaded / total * 100 if total and downloaded is not None else 0
    return {
        "downloaded": int(downloaded or 0), "total": int(total) if total else None,
        "speed": number(speed), "eta": number(eta), "progress": min(100, progress),
        "phase": "Обработка / следующий файл" if status == "finished" else "Загрузка",
    }


def sanitize_log(line: str) -> str:
    # Strip whole URLs, header-bearing lines and common secret assignments.
    line = re.sub(r"\x1b\[[0-9;]*m", "", line)
    if re.search(r"(?i)(authorization|set-cookie|cookie\s*:|bearer\s|client_secret|access_token|refresh_token)", line) or line.count("\t") >= 6:
        return "[скрыто: строка может содержать учётные данные]"
    line = re.sub(r"https?://[^\s<>\"']+", "[URL скрыт]", line)
    line = re.sub(r"(?i)\b(token|password|secret|session|cookie|signature|sig)\s*[:=]\s*[^\s,;]+", r"\1=[скрыто]", line)
    return line[:2000]


async def spawn(args, **kwargs):
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    environment = os.environ.copy()
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    environment.update(kwargs.pop("env", {}))
    options["env"] = environment
    args = list(args)
    cookie_copy = None
    try:
        # yt-dlp writes its cookie jar on exit. A private copy prevents parallel
        # download/metadata processes from truncating the shared source file.
        if "--cookies" in args:
            index = args.index("--cookies") + 1
            source = Path(args[index])
            temporary = config.data_dir / "tmp"
            temporary.mkdir(mode=0o700, exist_ok=True)
            cookie_copy = temporary / ("cookies-" + secrets.token_hex(16) + ".txt")
            fd = os.open(cookie_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                secure_cookie_file(cookie_copy)
                stream.write(source.read_bytes())
            args[index] = str(cookie_copy)
        process = await asyncio.create_subprocess_exec(*args, **options, **kwargs)
        process.cookie_copy = cookie_copy
        return process
    except BaseException:
        if cookie_copy:
            cookie_copy.unlink(missing_ok=True)
        raise


def cleanup_process(process):
    cookie_copy = getattr(process, "cookie_copy", None)
    if cookie_copy:
        cookie_copy.unlink(missing_ok=True)


async def stop_process(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            parent.kill()
        except psutil.Error:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        await process.wait()


def normalize_metadata(raw: dict) -> dict:
    entries = []

    def flatten(node):
        if node.get("entries") is not None:
            for entry in node["entries"]:
                if isinstance(entry, dict):
                    flatten(entry)
        else:
            entries.append(node)

    flatten(raw)
    qualities, audio, subtitles = set(), {}, set()
    episodes = []
    for index, entry in enumerate(entries, 1):
        for fmt in entry.get("formats") or []:
            if fmt.get("height"):
                qualities.add(int(fmt["height"]))
            if fmt.get("vcodec") == "none":
                language = fmt.get("language") or ""
                studio = fmt.get("format_note") or language or fmt.get("format_id", "")
                # Labels are display-only. Selection is a safe plugin preference string.
                preference = studio if re.fullmatch(r"[\w .+()/-]{1,80}", studio) else language
                if re.fullmatch(r"[\w .+()/-]{1,80}", preference):
                    audio[preference] = studio
        subtitles.update((entry.get("subtitles") or {}).keys())
        episodes.append({"index": index, "title": str(entry.get("title") or f"Эпизод {index}"),
                         "season": entry.get("season_number"), "episode": entry.get("episode_number"),
                         "duration": entry.get("duration")})
    return {"title": str(raw.get("title") or "Без названия"),
            "kind": "series" if raw.get("entries") is not None else "movie",
            "episodes": episodes if raw.get("entries") is not None else [],
            "qualities": sorted(qualities, reverse=True),
            "audio": [{"value": key, "label": label} for key, label in audio.items()],
            "subtitles": sorted(subtitles), "duration": raw.get("duration")}


async def get_metadata(url: str, settings: Settings) -> dict:
    check_dependencies()
    process = await spawn(metadata_args(url, settings), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), config.metadata_timeout)
    except (TimeoutError, asyncio.CancelledError):
        await stop_process(process)
        if asyncio.current_task().cancelling():
            raise
        raise ValueError("Время получения метаданных истекло. Проверьте cookies и соединение")
    finally:
        cleanup_process(process)
    if process.returncode:
        raise explain_error(stderr.decode("utf-8", errors="replace")[-8000:], settings)
    try:
        return normalize_metadata(json.loads(stdout))
    except (ValueError, TypeError, KeyError):
        raise ValueError("yt-dlp вернул некорректные метаданные")
