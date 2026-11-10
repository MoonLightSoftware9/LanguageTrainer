"""Desktop window for the portable Windows application."""
import ctypes
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

READY_PREFIX = "DICT_SERVER_READY "


def main():
    root = Path(os.environ.get("DICT_PORTABLE_ROOT", Path(sys.argv[0]).resolve().parent))
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    log = (data / "desktop.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    process = None
    closed = threading.Event()
    lock = threading.Lock()

    def stop_backend(*_):
        nonlocal process
        closed.set()
        with lock:
            child = process
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    try:
        import webview

        window = webview.create_window(
            "LanguageTrainer", html="<h2>LanguageTrainer</h2><p>Starting…</p>",
            width=1280, height=850, min_size=(900, 600),
        )
        window.events.closed += stop_backend

        def start_backend():
            nonlocal process
            try:
                environment = dict(os.environ)
                environment.update(
                    DICT_ROOT_DIR=str(root / "resources"), DICT_DATA_DIR=str(data),
                    DICT_ARGOS_ROOT=str(data / "argos-translate"),
                    NLTK_DATA=str(root / "resources/nltk_data"),
                    PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                )
                with lock:
                    if closed.is_set():
                        return
                    process = subprocess.Popen(
                        [str(root / "backend/python.exe"), str(root / "backend/dict.py"),
                         "server", "--host", "127.0.0.1", "--port", "0", "--init"],
                        cwd=root, env=environment, stdout=subprocess.PIPE, stderr=log,
                        text=True, encoding="utf-8", errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                for line in process.stdout:
                    log.write(line)
                    if line.startswith(READY_PREFIX) and not closed.is_set():
                        window.load_url(json.loads(line[len(READY_PREFIX):])["url"])
                if not closed.is_set():
                    raise RuntimeError("The backend stopped. See data/desktop.log for details.")
            except Exception as error:
                if not closed.is_set():
                    log.write(f"Desktop error: {error}\n")
                    window.load_html("<h2>LanguageTrainer</h2><p>" + html.escape(str(error)) + "</p>")

        webview.start(start_backend, gui="edgechromium", private_mode=False,
                      storage_path=str(data / "webview"))
    except Exception as error:
        log.write(f"Desktop startup error: {error}\n")
        ctypes.windll.user32.MessageBoxW(
            None, f"{error}\n\nDetails: {data / 'desktop.log'}", "LanguageTrainer", 0x10,
        )
        return 1
    finally:
        stop_backend()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
