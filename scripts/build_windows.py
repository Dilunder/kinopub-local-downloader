"""Build one EXE with Python, yt-dlp, the plugin, FFmpeg and the launcher UI."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != "win32":
        raise SystemExit("Build the Windows EXE on Windows (or use the GitHub Actions workflow)")
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(output), "--workpath", str(ROOT / "work" / "pyinstaller"),
                    str(ROOT / "packaging" / "windows.spec")], cwd=ROOT, check=True)
    executable = output / "KinoPub-Local-Downloader.exe"
    with executable.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{checksum}  {executable.name}\n", encoding="utf-8")
    shutil.copyfile(ROOT / "packaging" / "WINDOWS-README.txt", output / "WINDOWS-README.txt")
    with zipfile.ZipFile(output / "third-party-notices.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / "packaging" / "THIRD_PARTY.md", "THIRD_PARTY.md")
        for name in ("LICENSE", "README.txt", "provenance.json"):
            archive.write(ROOT / "vendor" / "ffmpeg" / name, "ffmpeg/" + name)
        versions = []
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata["Name"]
            versions.append({"name": name, "version": distribution.version})
            for file in distribution.files or []:
                if any(word in str(file).lower() for word in ("license", "copying", "notice")) and ".dist-info/" in str(file).replace("\\", "/"):
                    actual = Path(distribution.locate_file(file))
                    if actual.is_file():
                        archive.write(actual, "python/" + str(file).replace("\\", "/"))
        archive.writestr("python-versions.json", json.dumps(versions, ensure_ascii=False, indent=2))
    print("Built", executable, f"({executable.stat().st_size / 1024 / 1024:.1f} MiB)")


if __name__ == "__main__":
    main()
