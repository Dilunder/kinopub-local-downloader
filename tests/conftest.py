import os
from pathlib import Path
import tempfile

import pytest

# All tests use a disposable database and files, never the user's queue/cookies.
work_dir = Path(__file__).resolve().parent.parent / "work"
work_dir.mkdir(exist_ok=True)
test_root = tempfile.TemporaryDirectory(prefix="kinopub-test-", dir=work_dir, ignore_cleanup_errors=True)
os.environ["KP_DATA_DIR"] = str(Path(test_root.name) / "data")
os.environ["KP_DOWNLOAD_DIR"] = str(Path(test_root.name) / "downloads")
os.environ["KP_DEV_MODE"] = "false"

from fastapi.testclient import TestClient
from app.db import Base, Session, engine
from app.main import app, csrf_token, worker
from app.models import Job, Setting
from app.services import ytdlp


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.create_all(engine)
    with Session() as session:
        session.query(Job).delete()
        session.query(Setting).delete()
        session.commit()
    worker.cancel_requested.clear()
    worker.tasks.clear()
    worker.processes.clear()


@pytest.fixture
def client():
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.headers["X-CSRF-Token"] = csrf_token
        yield client
