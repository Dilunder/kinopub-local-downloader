import json

from app.db import Session
from app.models import Setting
from app.schemas import Settings


def get_settings() -> Settings:
    with Session() as session:
        row = session.get(Setting, "app")
        return Settings.model_validate_json(row.value) if row else Settings()


def save_settings(settings: Settings):
    with Session() as session:
        session.merge(Setting(key="app", value=json.dumps(settings.model_dump(), ensure_ascii=False)))
        session.commit()
