"""Prepare explicit FFmpeg binaries for a Windows build; never downloads at runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = "9.0.2"
ARCHIVE = f"ffmpeg-{VERSION}-full_build.zip"
EXPECTED_SHA256 = "759d0a9831c436a0eb331ad36f236c06cb04aaa0005da46571f0e9d3d9206f6b"
API = f"https://api.github.com/repos/GyanD/codexffmpeg/releases/tags/{VERSION}"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url, target):
    request = urllib.request.Request(url, headers={"User-Agent": "KinoPub-Local-Downloader-build", "Accept": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=120) as source, target.open("wb") as destination:
        shutil.copyfileobj(source, destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-dir", type=Path, help="Directory containing bin/ffmpeg.exe, bin/ffprobe.exe and LICENSE")
    args = parser.parse_args()
    target = ROOT / "vendor" / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    provenance = {"vendor": "Gyan", "version": VERSION}
    if args.from_dir:
        source = args.from_dir.resolve()
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            shutil.copyfile(source / "bin" / name, target / name)
        shutil.copyfile(source / "LICENSE", target / "LICENSE")
        if (source / "README.txt").exists():
            shutil.copyfile(source / "README.txt", target / "README.txt")
        provenance["origin"] = "Existing local Gyan distribution"
    else:
        cache = ROOT / "work" / "ffmpeg-build"
        cache.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(API, headers={"User-Agent": "KinoPub-Local-Downloader-build", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
        asset = next(item for item in release["assets"] if item["name"] == ARCHIVE)
        expected = "sha256:" + EXPECTED_SHA256
        if asset.get("digest") != expected:
            raise RuntimeError("The upstream FFmpeg digest changed; verify and update the pinned checksum deliberately")
        archive = cache / ARCHIVE
        if not archive.exists() or digest(archive) != expected.split(":", 1)[1]:
            fetch(asset["browser_download_url"], archive)
        if digest(archive) != expected.split(":", 1)[1]:
            raise RuntimeError("FFmpeg archive checksum mismatch")
        with zipfile.ZipFile(archive) as zipped:
            for filename, suffix in (("ffmpeg.exe", "/bin/ffmpeg.exe"), ("ffprobe.exe", "/bin/ffprobe.exe"), ("LICENSE", "/LICENSE"), ("README.txt", "/README.txt")):
                matches = [name for name in zipped.namelist() if name.endswith(suffix)]
                if len(matches) != 1:
                    raise RuntimeError("Unexpected FFmpeg archive structure: " + filename)
                with zipped.open(matches[0]) as source, (target / filename).open("wb") as destination:
                    shutil.copyfileobj(source, destination)
        provenance.update(archive_url=asset["browser_download_url"], archive_sha256=digest(archive), release_url=release["html_url"])
    version = subprocess.run([str(target / "ffmpeg.exe"), "-version"], capture_output=True, text=True, check=True).stdout
    if f"ffmpeg version {VERSION}" not in version:
        raise RuntimeError("Unexpected FFmpeg version; update the build configuration deliberately")
    provenance.update(ffmpeg_sha256=digest(target / "ffmpeg.exe"), ffprobe_sha256=digest(target / "ffprobe.exe"), configuration=version)
    (target / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Prepared FFmpeg", VERSION, "in", target)


if __name__ == "__main__":
    main()
