import json
from pathlib import Path
import socket
import sys

import pytest

from app.services.ytdlp import cli_prefix
from desktop import reserve_socket, running_instance


def test_cli_prefix_python():
    assert cli_prefix() == [sys.executable, "-m", "yt_dlp"]


def test_cli_prefix_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert cli_prefix() == [sys.executable, "--internal-ytdlp"]


def test_launcher_binds_only_loopback():
    connection = reserve_socket(0)
    try:
        assert connection.getsockname()[0] == "127.0.0.1"
        assert connection.getsockname()[1] > 0
        with pytest.raises(RuntimeError):
            reserve_socket(connection.getsockname()[1])
    finally:
        connection.close()


@pytest.mark.parametrize("url", ["https://example.com", "http://127.0.0.1.evil.test", "http://user:password@127.0.0.1:8000", "http://127.0.0.1:8000/private", "http://127.0.0.1:8000?x=1"])
def test_existing_instance_rejects_external_address(tmp_path, url):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"url": url}))
    assert running_instance(path) is None
