from pathlib import Path
import os
import sys

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
FROZEN = bool(getattr(sys, "frozen", False))
USER_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "KinoPubLocalDownloader"
DEFAULT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KP_", env_file=None if FROZEN else ".env", extra="ignore")
    data_dir: Path = (USER_ROOT / "data") if FROZEN else ROOT / "data"
    download_dir: Path = (Path.home() / "Downloads" / "KinoPub") if FROZEN else ROOT / "downloads"
    dev_mode: bool = False
    metadata_timeout: int = Field(180, ge=5, le=1800)


config = Config()
config.data_dir = config.data_dir.resolve()
config.download_dir = config.download_dir.resolve()
