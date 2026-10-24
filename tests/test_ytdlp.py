from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import config
from app.schemas import JobCreate, Settings, validate_template, validate_url
from app.services.ytdlp import download_args, metadata_args, normalize_metadata, parse_progress, safe_destination, sanitize_log


@pytest.mark.parametrize("url", [
    "https://kino.pub/item/view/123", "https://kino.watch/item/view/42/s2e3", "http://www.kino.pub/item/view/1/"
])
def test_allowed_url(url):
    assert validate_url(url) == url


@pytest.mark.parametrize("url", [
    "https://kino.pub.evil.test/item/view/1", "https://evilkino.pub/item/view/1",
    "file:///etc/passwd", "https://kino.pub@evil.test/item/view/1",
    "https://name:password@kino.pub/item/view/1", "https://kino.pub:999/item/view/1",
    "https://kino.pub/", "--exec rm", "https://kino.pub/item/view/1?token=secret",
    "https://kino.pub/item/view/1#token", "https://[bad", "https://kino.pub:bad/item/view/1",
    "https://kino.pub/item/vi\new/1"
])
def test_reject_url(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_developer_mode_explicit(monkeypatch):
    monkeypatch.setattr(config, "dev_mode", True)
    assert validate_url("https://example.org/video.mp4")


def test_progress_total_and_percent():
    data = parse_progress("KP_PROGRESS:downloading|512|1024|NA|128|4| 50.0%")
    assert data == {"downloaded": 512, "total": 1024, "speed": 128, "eta": 4, "progress": 50.0, "phase": "Загрузка"}


def test_progress_estimate_unknown_and_finished():
    assert parse_progress("KP_PROGRESS:downloading|25|NA|100|NA|NA|NA")["progress"] == 25
    unknown = parse_progress("KP_PROGRESS:downloading|NA|NA|NA|NA|NA|NA")
    assert unknown["total"] is None and unknown["progress"] == 0
    assert parse_progress("KP_PROGRESS:finished|100|100|NA|NA|0|100%")["phase"] == "Обработка / следующий файл"
    assert parse_progress("KP_PROGRESS:downloading|1|NA|NA|nan|inf|200%")["progress"] == 100
    assert parse_progress("KP_PROGRESS:bad") is None
    assert parse_progress("[download] 50%") is None


def test_metadata_arguments_browser():
    url = "https://kino.pub/item/view/1"
    args = metadata_args(url, Settings(browser="edge"))
    assert args[-2:] == ["--", url]
    assert args[args.index("--cookies-from-browser") + 1] == "edge"
    assert "--dump-single-json" in args and "--skip-download" in args
    assert "--ignore-config" in args and "--no-cache-dir" in args


def test_metadata_missing_cookies_file():
    (config.data_dir / "cookies.txt").unlink(missing_ok=True)
    with pytest.raises(ValueError, match="Нет cookies"):
        metadata_args("https://kino.pub/item/view/1", Settings(auth_source="file"))


def test_download_arguments_and_paths(tmp_path):
    settings = Settings(download_dir=str(tmp_path), quality="1080", audio="AniLibria", subtitles="ru,en", proxy="http://127.0.0.1:8080")
    args = download_args("https://kino.watch/item/view/1", settings, {"folder": "Сериалы/Вечер", "episodes": [2, 5]})
    assert args[args.index("--format") + 1] == "bv[height<=1080]+ba/b[height<=1080]"
    assert args[args.index("--playlist-items") + 1] == "2,5"
    assert args[args.index("--extractor-args") + 1] == "kinopub:audio=AniLibria"
    assert args[args.index("--sub-langs") + 1] == "ru,en"
    assert args[args.index("--paths") + 1] == str(tmp_path / "Сериалы" / "Вечер")
    assert "--newline" in args and "--progress-template" in args and "--no-simulate" in args
    assert args[args.index("--print") + 1].startswith("after_move:KP_FILE:")
    assert "--no-overwrites" in args


@pytest.mark.parametrize("folder", ["../outside", "a/../../b", "/tmp", "C:\\escape", "a\\..\\b", "a//b", "folder:stream"])
def test_folder_traversal(folder):
    with pytest.raises(ValidationError):
        JobCreate(url="https://kino.pub/item/view/1", folder=folder)


@pytest.mark.parametrize("template", ["../%(title)s.%(ext)s", "C:\\%(title)s", "%(filepath)s.%(ext)s", "%(title)s", "%(title)s\n.%(ext)s", "..%(id)s.%(ext)s"])
def test_template_traversal(template):
    with pytest.raises(ValueError):
        validate_template(template)


def test_valid_template():
    assert validate_template("S%(season_number)02dE%(episode_number)02d - %(title)s [%(id)s].%(ext)s")


def test_metadata_normalization_no_credentials():
    raw = {"title": "Сериал", "entries": [{"title": "Серия", "season_number": 2, "episode_number": 3,
           "formats": [{"height": 1080, "url": "https://secret"}, {"vcodec": "none", "language": "ru", "format_note": "Studio"}],
           "subtitles": {"ru": [{"url": "https://signed?token=SECRET"}]}, "http_headers": {"Authorization": "secret"}}]}
    result = normalize_metadata(raw)
    assert result["episodes"][0]["index"] == 1 and result["kind"] == "series"
    assert result["qualities"] == [1080] and result["subtitles"] == ["ru"]
    assert "secret" not in str(result).lower()


def test_log_redaction():
    assert "SECRET" not in sanitize_log("ERROR https://cdn.test/file?token=SECRET")
    assert "SECRET" not in sanitize_log("Authorization: Bearer SECRET")
    assert "SECRET" not in sanitize_log("cookie=SECRET")
    assert "SECRET" not in sanitize_log("access_token=SECRET")
    assert "SECRET" not in sanitize_log(".kino.pub\tTRUE\t/\tTRUE\t2000000000\tauth\tSECRET")
