"""Paths and helpers for the Windows offline desktop build."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "EuroglassHardware"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Read-only resources shipped with the app (templates, schema, static)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


def app_root() -> Path:
    """Project root (dev) or folder next to the .exe (frozen)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """Writable per-user data folder (database, sync state, logs)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        path = Path(base) / APP_NAME
    else:
        path = Path.home() / f".{APP_NAME.lower()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return data_dir() / "euroglass_stock.db"


def sync_state_path() -> Path:
    return data_dir() / "sync_state.json"


def log_path() -> Path:
    return data_dir() / "desktop.log"


def default_sqlite_url() -> str:
    # Absolute path; four slashes after scheme for absolute Unix paths,
    # Windows paths use sqlite:///C:/...
    path = database_path().resolve()
    if os.name == "nt":
        return "sqlite:///" + str(path).replace("\\", "/")
    return f"sqlite:///{path}"
