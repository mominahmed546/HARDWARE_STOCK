"""Euroglass Hardware — offline desktop launcher.

Starts the Flask app against a local SQLite database and opens it in a
native window (pywebview). On Windows, build a single .exe with PyInstaller
(see desktop/README.md).
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path


def _prepare_environment() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    os.environ.setdefault("DESKTOP_MODE", "1")
    os.environ.setdefault("FLASK_ENV", "desktop")
    os.environ.setdefault("APP_ENV", "desktop")

    from desktop.paths import default_sqlite_url, log_path

    os.environ.setdefault("DATABASE_URL", default_sqlite_url())
    os.environ.setdefault("SECRET_KEY", "euroglass-desktop-local-key")

    logging.basicConfig(
        filename=str(log_path()),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _run_flask(host: str, port: int) -> None:
    from app import app

    # threaded=True so the UI stays responsive while pages load.
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def main() -> int:
    _prepare_environment()
    host = "127.0.0.1"
    port = int(os.environ.get("DESKTOP_PORT") or _free_port())
    url = f"http://{host}:{port}/"

    server = threading.Thread(target=_run_flask, args=(host, port), daemon=True)
    server.start()

    if not _wait_for_server(host, port):
        logging.error("Flask server failed to start on %s", url)
        print("Failed to start Euroglass Hardware. See desktop.log for details.", file=sys.stderr)
        return 1

    icon = Path(__file__).resolve().parent / "euroglass.ico"
    window_kwargs = {
        "title": "Euroglass Hardware",
        "url": url,
        "width": 1280,
        "height": 800,
        "min_size": (900, 600),
    }

    try:
        import webview
    except ImportError:
        print(
            "pywebview is not installed. Run:\n"
            "  pip install -r requirements-desktop.txt\n"
            f"Or open the app in a browser at {url}",
            file=sys.stderr,
        )
        # Keep server alive for browser fallback during development.
        try:
            while server.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    # Older pywebview builds reject `icon=`; try with icon, then without.
    if icon.exists():
        try:
            webview.create_window(**window_kwargs, icon=str(icon))
        except TypeError:
            webview.create_window(**window_kwargs)
    else:
        webview.create_window(**window_kwargs)
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
