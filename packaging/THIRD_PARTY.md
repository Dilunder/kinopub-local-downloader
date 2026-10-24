# Third-party components in the Windows build

Python: https://www.python.org/ — PSF license.
PyInstaller: https://pyinstaller.org/ — GPL with the bootloader exception.
yt-dlp: https://github.com/yt-dlp/yt-dlp — Unlicense; bundled dependencies retain their own licenses.
yt-dlp-kinopub 0.1.1: https://github.com/niazlv/yt-dlp-kinopub — GPL-3.0-or-later.
FFmpeg/ffprobe: https://www.ffmpeg.org/ and https://www.gyan.dev/ffmpeg/builds/.
The included Gyan build is GPLv3. Its LICENSE is retained in the executable
and the release notices archive. FFmpeg and external libraries remain the
property of their respective authors.

Build provenance, original archive URL, checksums and FFmpeg configuration
are recorded in vendor/ffmpeg/provenance.json and the notices release archive.
Gyan build scripts and source references: https://github.com/GyanD/codexffmpeg.
FFmpeg source: https://git.ffmpeg.org/ffmpeg.git.
Plugin source is included with the PyInstaller data files for discovery.

Before publishing a public binary release, include the corresponding source
and license materials required by the components you redistribute. Automated
tag builds create a draft release so the owner can review the distribution.
