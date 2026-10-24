import json

from scripts.verify_windows_exe import public_report


def test_public_release_report_omits_private_machine_and_account_data():
    health = {"app": "kinopub-local-downloader", "version": "0.2.0", "ok": True,
              "plugin_loadable": True, "yt_dlp": "test", "plugin": "test",
              "ffmpeg": r"C:\Users\PrivateUser\AppData\Local\Temp\ffmpeg.exe",
              "access_token": "PRIVATE_TOKEN"}
    job = {"status": "done", "progress": 100, "downloaded": 28058,
           "files": [r"C:\Users\PrivateUser\Downloads\PrivateTitle.mkv"],
           "title": "PrivateTitle", "url": "https://kino.pub/?token=PRIVATE_TOKEN",
           "error": "private@example.org"}
    report = public_report(health, job)
    serialized = json.dumps(report)
    for private in ("PrivateUser", "PrivateTitle", "PRIVATE_TOKEN", "private@example.org", "C:\\", "https://"):
        assert private not in serialized
    assert report["download"]["file_count"] == 1
    assert report["download"]["status"] == "done"
