import re
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from typing import Literal

from app.config import DEFAULT_TEMPLATE, config

BROWSERS = ("chrome", "chromium", "edge", "firefox", "safari", "brave", "opera", "vivaldi", "whale")
TEMPLATE_FIELDS = {"title", "id", "ext", "series", "season_number", "episode_number", "playlist_index"}


def validate_url(value: str) -> str:
    value = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("URL содержит управляющие символы")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("Некорректный URL")
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("Нужен HTTP(S) URL без логина и пароля")
    if not config.dev_mode:
        if url.hostname.lower() not in {"kino.pub", "kino.watch", "www.kino.pub", "www.kino.watch"} or port not in (None, 80, 443):
            raise ValueError("Разрешены только kino.pub и kino.watch")
        if not re.fullmatch(r"/item/view/\d+(?:/s\d+e\d+)?/?", url.path):
            raise ValueError("Ожидается ссылка /item/view/123 или /item/view/123/s1e1")
        if url.query or url.fragment:
            raise ValueError("Удалите query-параметры и фрагмент из ссылки")
    return value


def relative_folder(value: str) -> str:
    if not value:
        return ""
    parts = value.replace("\\", "/").split("/")
    if Path(value).is_absolute() or PureWindowsPath(value).drive or any(p in ("", ".", "..") for p in parts):
        raise ValueError("Подпапка должна быть относительной, без . и ..")
    if any(c in value for c in ':*?"<>|\x00') or any(ord(c) < 32 for c in value):
        raise ValueError("Недопустимые символы в подпапке")
    return "/".join(parts)


def validate_template(value: str) -> str:
    if not value or len(value) > 250 or any(c in value for c in '/\\:\x00'):
        raise ValueError("Шаблон — только имя файла, без путей")
    pattern = r"%\(([a-z_]+)\)(?:0?[1-9]\d?)?[sd]"
    for match in re.finditer(pattern, value):
        if match[1] not in TEMPLATE_FIELDS:
            raise ValueError(f"Поле шаблона не поддерживается: {match[1]}")
    remainder = re.sub(pattern, "field", value)
    if "%" in remainder or any(ord(c) < 32 for c in value) or any(c in remainder for c in '*?"<>|'):
        raise ValueError("Недопустимый шаблон имени")
    if remainder.startswith(".") or ".." in remainder or not value.endswith(".%(ext)s"):
        raise ValueError("Имя должно заканчиваться .%(ext)s и не начинаться с точки")
    return value


def validate_user_agent(value: str) -> str:
    value = value.strip()
    if value and (len(value) > 1024 or not value.startswith("Mozilla/") or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        raise ValueError("User-Agent браузера должен начинаться с Mozilla/ и не содержать переносов строк")
    return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    download_dir: str = str(config.download_dir)
    auth_source: str = "browser"
    browser: str = "firefox"
    quality: str = "best"
    audio: str = ""
    subtitles: str = ""
    filename_template: str = DEFAULT_TEMPLATE
    parallel_downloads: int = Field(1, ge=1, le=4)
    proxy: str = ""
    user_agent: str = ""

    @field_validator("user_agent")
    @classmethod
    def ua_valid(cls, value):
        return validate_user_agent(value)

    @field_validator("auth_source")
    @classmethod
    def auth_valid(cls, value):
        if value not in {"browser", "file"}:
            raise ValueError("Выберите browser или file")
        return value

    @field_validator("browser")
    @classmethod
    def browser_valid(cls, value):
        if value not in BROWSERS:
            raise ValueError("Браузер не поддерживается")
        return value

    @field_validator("download_dir")
    @classmethod
    def dir_valid(cls, value):
        if not value.strip() or "\x00" in value or ".." in Path(value).parts:
            raise ValueError("Укажите корректную корневую папку без ..")
        return str(Path(value).expanduser().resolve())

    @field_validator("filename_template")
    @classmethod
    def template_valid(cls, value):
        return validate_template(value)

    @field_validator("quality")
    @classmethod
    def quality_valid(cls, value):
        if value not in {"best", "2160", "1440", "1080", "720", "480", "360"}:
            raise ValueError("Неподдерживаемое качество")
        return value

    @field_validator("audio")
    @classmethod
    def audio_valid(cls, value):
        if len(value) > 80 or not re.fullmatch(r"[\w .+()/-]*", value):
            raise ValueError("Аудио: язык или название студии, до 80 символов")
        return value

    @field_validator("subtitles")
    @classmethod
    def subs_valid(cls, value):
        if len(value) > 100 or (value and not re.fullmatch(r"[a-zA-Z0-9_,-]+", value)):
            raise ValueError("Субтитры: языки через запятую или all")
        return value

    @field_validator("proxy")
    @classmethod
    def proxy_valid(cls, value):
        if value:
            url = urlsplit(value)
            if url.scheme not in {"http", "https", "socks5", "socks5h"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Proxy: HTTP(S)/SOCKS5 URL без учётных данных")
            try:
                url.port
            except ValueError:
                raise ValueError("Некорректный порт proxy")
        return value


class CookieHeaderImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["kino.watch", "kino.pub"] = "kino.watch"
    cookie_header: SecretStr
    user_agent: str = ""

    @field_validator("user_agent")
    @classmethod
    def ua_valid(cls, value):
        return validate_user_agent(value)

    @field_validator("cookie_header")
    @classmethod
    def header_size(cls, value):
        if not 1 <= len(value.get_secret_value()) <= 1_048_576:
            raise ValueError("Cookie должен содержать от 1 до 1048576 символов")
        return value


class MetadataRequest(BaseModel):
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def url_valid(cls, value):
        return validate_url(value)


class JobCreate(MetadataRequest):
    title: str = Field("", max_length=500)
    quality: str | None = None
    audio: str | None = None
    subtitles: str | None = None
    folder: str = Field("", max_length=200)
    filename_template: str | None = None
    episodes: list[int] = Field(default_factory=list, max_length=5000)

    @field_validator("folder")
    @classmethod
    def folder_valid(cls, value):
        return relative_folder(value)

    @field_validator("episodes")
    @classmethod
    def episodes_valid(cls, value):
        if any(i < 1 or i > 100000 for i in value):
            raise ValueError("Номера эпизодов должны быть от 1 до 100000")
        return sorted(set(value))
