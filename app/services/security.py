"""Permissions for the one credential file managed by this application."""
import csv
import io
import os
from pathlib import Path
import subprocess


def secure_cookie_file(path: Path):
    os.chmod(path, 0o600)
    if os.name != "nt":
        return
    # chmod does not implement 0600 on NTFS. Remove inherited permissions and
    # grant the current user's SID full access before writing the credential.
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    try:
        identity = subprocess.run(
            [str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, check=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        sid = next(csv.reader(io.StringIO(identity.stdout)))[1].strip()
        if not sid.startswith("S-1-"):
            raise ValueError("SID unavailable")
        subprocess.run(
            [str(system32 / "icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
            capture_output=True, check=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, StopIteration):
        raise ValueError("Не удалось ограничить доступ к cookies.txt текущим пользователем Windows")
