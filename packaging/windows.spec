from pathlib import Path
import os
import importlib.util

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
vendor = root / "vendor" / "ffmpeg"
for filename in ("ffmpeg.exe", "ffprobe.exe", "LICENSE"):
    if not (vendor / filename).is_file():
        raise RuntimeError("Run scripts/prepare_ffmpeg.py before building: missing " + filename)

datas = [(str(root / "app" / "templates"), "app/templates"),
         (str(root / "app" / "static"), "app/static"),
         (str(root / "packaging" / "THIRD_PARTY.md"), "licenses"),
         (str(vendor / "LICENSE"), "licenses/ffmpeg"),
         (str(vendor / "ffmpeg.exe"), "ffmpeg"),
         (str(vendor / "ffprobe.exe"), "ffmpeg")]
# Some managed Python distributions ship an additional SSL trust-policy file.
# Preserve that policy when freezing rather than changing certificate checks.
ssl_spec = importlib.util.find_spec("ssl")
ssl_policy = Path(ssl_spec.origin).with_name("codex-rejected-cas.pem")
if ssl_policy.is_file():
    datas.append((str(ssl_policy), "."))
binaries, hiddenimports = [], ["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"]
for package in ("yt_dlp",):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden
hiddenimports += collect_submodules("app")
plugin = importlib.util.find_spec("yt_dlp_plugins.extractor.kinopub")
datas.append((str(Path(plugin.origin).parent.parent), "yt_dlp_plugins"))
for distribution in ("yt-dlp", "yt-dlp-kinopub", "fastapi", "uvicorn", "pydantic", "pydantic-settings", "SQLAlchemy", "psutil"):
    datas += copy_metadata(distribution, recursive=True)

a = Analysis([str(root / "desktop.py")], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[],
             excludes=["pytest", "IPython", "notebook", "matplotlib", "numpy", "pandas", "scipy"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="KinoPub-Local-Downloader",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, hide_console="hide-early", disable_windowed_traceback=False)
