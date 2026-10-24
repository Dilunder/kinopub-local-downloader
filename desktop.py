"""Windows entry point; --internal-ytdlp runs a cancellable download child."""
import argparse
import json
import logging
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request
import webbrowser


def prepare_environment():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    ffmpeg = bundle / "ffmpeg"
    if ffmpeg.is_dir():
        os.environ["PATH"] = str(ffmpeg) + os.pathsep + os.environ.get("PATH", "")


def running_instance(path: Path) -> str | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        address = state["url"]
        from urllib.parse import urlsplit
        parsed = urlsplit(address)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(address + "/api/health", timeout=1) as response:
            health = json.load(response)
        from app.version import APP_ID
        return address if health.get("app") == APP_ID else None
    except (OSError, ValueError, KeyError):
        return None


def reserve_socket(port: int | None):
    for candidate in ([port] if port is not None else range(8000, 8011)):
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            connection.bind(("127.0.0.1", candidate))
            connection.listen(128)
            return connection
        except OSError:
            connection.close()
    raise RuntimeError("Не удалось занять локальный порт. Закройте предыдущий экземпляр приложения")


def main():
    multiprocessing.freeze_support()
    prepare_environment()
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-ytdlp":
        # Console-subsystem EXE + redirected handles keep normal stdout/stderr
        # available even when the launcher console is hidden.
        import yt_dlp
        yt_dlp.main(sys.argv[2:])
        return 0

    parser = argparse.ArgumentParser(description="KinoPub Local Downloader")
    parser.add_argument("--headless", action="store_true", help="Run without launcher window (for checks)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open a browser")
    parser.add_argument("--port", type=int, choices=range(1, 65536), metavar="PORT")
    args = parser.parse_args()
    from app.config import config
    from app.version import VERSION

    config.data_dir.mkdir(parents=True, exist_ok=True)
    state_path = config.data_dir / "desktop.json"
    existing = running_instance(state_path)
    if existing:
        if not args.no_browser:
            webbrowser.open(existing)
        return 0

    log_file = config.data_dir / "desktop.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    connection = reserve_socket(args.port)
    url = f"http://127.0.0.1:{connection.getsockname()[1]}"
    import uvicorn
    from app.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", log_config=None, access_log=False,
                                          loop="asyncio", http="h11", ws="none"))
    errors = []

    def serve():
        try:
            server.run(sockets=[connection])
        except BaseException as error:
            errors.append(str(error))
            logging.exception("Launcher server stopped")

    thread = threading.Thread(target=serve, name="kinopub-server", daemon=True)
    thread.start()

    def publish_ready():
        state_path.write_text(json.dumps({"pid": os.getpid(), "url": url, "version": VERSION}), encoding="utf-8")
        if not args.no_browser:
            webbrowser.open(url)

    try:
        if args.headless:
            deadline = time.monotonic() + 45
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not server.started:
                raise RuntimeError("Не удалось запустить сервер: " + "; ".join(errors))
            publish_ready()
            while thread.is_alive():
                thread.join(0.25)
        else:
            import tkinter as tk
            from tkinter import messagebox
            window = tk.Tk()
            window.title("KinoPub Local Downloader")
            window.geometry("510x270")
            window.resizable(False, False)
            window.configure(bg="#101212")
            tk.Label(window, text="KinoPub", bg="#101212", fg="#d6f27d", font=("Segoe UI", 26, "bold")).pack(pady=(24, 2))
            tk.Label(window, text=f"Local Downloader · {VERSION}", bg="#101212", fg="#969e99", font=("Segoe UI", 10)).pack()
            status = tk.StringVar(value="Запускаем локальный сервис…")
            tk.Label(window, textvariable=status, bg="#101212", fg="#f0f1eb", font=("Segoe UI", 11)).pack(pady=17)
            actions = tk.Frame(window, bg="#101212")
            actions.pack()
            open_button = tk.Button(actions, text="Открыть интерфейс", command=lambda: webbrowser.open(url),
                                    state="disabled", bg="#d6f27d", fg="#202719", relief="flat", padx=18, pady=9)
            open_button.pack(side="left", padx=6)
            closing = False

            def close():
                nonlocal closing
                if closing:
                    return
                closing = True
                status.set("Останавливаем загрузки и сервис…")
                server.should_exit = True
                open_button.configure(state="disabled")
                stop_button.configure(state="disabled")
                wait_for_stop()

            def wait_for_stop():
                if thread.is_alive():
                    window.after(100, wait_for_stop)
                else:
                    window.destroy()

            stop_button = tk.Button(actions, text="Остановить и закрыть", command=close,
                                    bg="#252c25", fg="#f0f1eb", relief="flat", padx=18, pady=9)
            stop_button.pack(side="left", padx=6)
            tk.Label(window, text="Закрытие вкладки браузера не останавливает приложение.", bg="#101212", fg="#969e99", font=("Segoe UI", 9)).pack(pady=19)
            window.protocol("WM_DELETE_WINDOW", close)
            ready = False

            def tick():
                nonlocal ready
                if closing:
                    return
                if server.started and not ready:
                    ready = True
                    publish_ready()
                    status.set("Работает: " + url)
                    open_button.configure(state="normal")
                if not thread.is_alive():
                    messagebox.showerror("KinoPub", f"Сервис остановлен. Подробности: {log_file}", parent=window)
                    window.destroy()
                    return
                window.after(150, tick)

            window.after(100, tick)
            window.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        connection.close()
        if state_path.exists():
            try:
                if json.loads(state_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    state_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        logging.exception("KinoPub could not start")
        if "--headless" not in sys.argv and "--internal-ytdlp" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), "KinoPub — ошибка запуска", 0x10)
        raise
