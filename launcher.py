# GTC 股票專業版看盤分析系統 v5.3.2 EXE Launcher
# Purpose:
#   PyInstaller EXE entry point for Streamlit Web app.
#   Do NOT package main.py directly as EXE entry.
#   This launcher starts Streamlit properly, then opens http://localhost:8501.

from __future__ import annotations

import os
import sys
import time
import socket
import logging
import threading
import webbrowser
from pathlib import Path
from urllib.request import urlopen

APP_NAME = "GTC Stock Web"
APP_VERSION = "v5.3.2-Launcher"
DEFAULT_PORT = 8501
LOG_FILE = "gtc_launcher.log"


def app_dir() -> Path:
    """Return PyInstaller extraction folder when frozen, otherwise source folder."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def setup_logging() -> None:
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
    )
    logging.info("%s %s starting", APP_NAME, APP_VERSION)
    logging.info("cwd=%s", os.getcwd())
    logging.info("executable=%s", sys.executable)
    logging.info("frozen=%s", getattr(sys, "frozen", False))
    logging.info("app_dir=%s", app_dir())


def find_free_port(start_port: int = DEFAULT_PORT, max_try: int = 20) -> int:
    for port in range(start_port, start_port + max_try):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"No free localhost port found from {start_port} to {start_port + max_try - 1}")


def wait_and_open_browser(port: int, timeout_sec: int = 60) -> None:
    url = f"http://localhost:{port}"
    deadline = time.time() + timeout_sec
    logging.info("waiting for Streamlit server: %s", url)
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as resp:
                if 200 <= resp.status < 500:
                    logging.info("Streamlit server reachable, opening browser: %s", url)
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(1)
    logging.error("Streamlit server did not become reachable within %s seconds: %s", timeout_sec, url)
    print(f"[ERROR] Streamlit server did not start within {timeout_sec} seconds: {url}")
    print(f"[INFO] Please check {LOG_FILE} in the same folder as this EXE.")


def main() -> int:
    setup_logging()
    base = app_dir()
    script_path = base / "main.py"
    core_path = base / "gtc_core_engine.py"

    if not script_path.exists():
        msg = f"Missing main.py at {script_path}"
        logging.error(msg)
        print("[ERROR]", msg)
        input("Press Enter to close...")
        return 2
    if not core_path.exists():
        msg = f"Missing gtc_core_engine.py at {core_path}"
        logging.error(msg)
        print("[ERROR]", msg)
        input("Press Enter to close...")
        return 3

    # Ensure Streamlit app can import gtc_core_engine.py from PyInstaller extraction folder.
    sys.path.insert(0, str(base))
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "false")

    try:
        port = find_free_port(DEFAULT_PORT)
        url = f"http://localhost:{port}"
        logging.info("selected port=%s url=%s", port, url)
        print("=" * 72)
        print(f"{APP_NAME} {APP_VERSION}")
        print(f"Starting local Streamlit server: {url}")
        print("Do not close this black window while using the web app.")
        print("關閉此黑色視窗會停止本機 Web 系統。")
        print("=" * 72)

        opener = threading.Thread(target=wait_and_open_browser, args=(port,), daemon=True)
        opener.start()

        from streamlit.web import cli as stcli

        sys.argv = [
            "streamlit",
            "run",
            str(script_path),
            "--server.address=localhost",
            f"--server.port={port}",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
            "--global.developmentMode=false",
        ]
        logging.info("streamlit argv=%s", sys.argv)
        stcli.main()
        return 0
    except SystemExit as exc:
        code = int(exc.code or 0) if isinstance(exc.code, int) else 0
        logging.info("Streamlit exited with code=%s", code)
        return code
    except Exception as exc:
        logging.exception("Launcher failed")
        print("[ERROR] Launcher failed:", exc)
        print(f"[INFO] Please check {LOG_FILE} in the same folder as this EXE.")
        input("Press Enter to close...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
