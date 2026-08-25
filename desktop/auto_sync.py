"""Automatic bidirectional sync on desktop app start/exit."""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timezone
from typing import Any

from desktop.paths import database_path
from desktop.sync import load_sync_state, save_sync_state
from desktop.sync_config import config_ready, load_sync_config, save_sync_config
from desktop.sync_engine import run_sync


def _has_network(database_url: str = "") -> bool:
    hosts = []
    if database_url:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(database_url)
            if parsed.hostname:
                hosts.append((parsed.hostname, parsed.port or 5432))
        except Exception:
            pass
    hosts.extend(
        [
            ("aws-0-ap-northeast-1.pooler.supabase.com", 5432),
            ("1.1.1.1", 53),
        ]
    )
    for host, port in hosts:
        try:
            with socket.create_connection((host, int(port)), timeout=5):
                return True
        except OSError:
            continue
    return False


def _prompt_sync_setup(existing: dict | None = None) -> dict | None:
    """Collect cloud sync settings (tkinter on Windows)."""
    existing = existing or {}
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog
    except Exception:
        logging.exception("tkinter unavailable for sync setup")
        return None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    messagebox.showinfo(
        "Euroglass Sync Setup",
        "Cloud sync is required.\n\n"
        "Enter your Supabase Postgres URI and app login.\n"
        "These are saved on this PC and used every time the app starts.",
        parent=root,
    )

    database_url = simpledialog.askstring(
        "Cloud database URL",
        "Supabase Session pooler URI:\n"
        "postgresql://postgres.PROJECT:PASSWORD@....pooler.supabase.com:5432/postgres",
        initialvalue=existing.get("database_url") or "",
        parent=root,
    )
    if not database_url:
        root.destroy()
        return None

    username = simpledialog.askstring(
        "App username",
        "Stock app username (same as website):",
        initialvalue=existing.get("username") or "",
        parent=root,
    )
    if not username:
        root.destroy()
        return None

    password = simpledialog.askstring(
        "App password",
        "Stock app password:",
        show="*",
        parent=root,
    )
    root.destroy()
    if not password:
        return None

    from desktop.sync_engine import normalize_database_url

    cfg = {
        "database_url": normalize_database_url(database_url),
        "username": username.strip(),
        "password": password,
        "mode": "sync",
        "prefer": "local",
        "required": True,
        "sync_on_start": True,
        "sync_on_exit": True,
    }
    save_sync_config(cfg)
    return cfg


def _show_message(title: str, text: str, error: bool = False) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if error:
            messagebox.showerror(title, text, parent=root)
        else:
            messagebox.showinfo(title, text, parent=root)
        root.destroy()
    except Exception:
        logging.info("%s: %s", title, text)


def ensure_sync_config() -> dict | None:
    cfg = load_sync_config()
    if config_ready(cfg):
        return cfg
    return _prompt_sync_setup(cfg)


def perform_sync(cfg: dict, *, reason: str = "manual") -> dict[str, Any]:
    result = run_sync(
        database_url=cfg["database_url"],
        db_path=database_path(),
        username=cfg["username"],
        password=cfg["password"],
        mode=cfg.get("mode") or "sync",
        prefer=cfg.get("prefer") or "local",
    )
    state = load_sync_state()
    state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    state["last_status"] = f"ok_{reason}"
    state["last_message"] = f"Auto sync ({reason}) mode={result.get('mode')}"
    state["remote_url"] = cfg.get("database_url") or ""
    state["counts"] = {
        "local": result.get("local_counts") or {},
        "remote": result.get("remote_counts") or {},
    }
    save_sync_state(state)
    return result


def sync_on_startup() -> dict[str, Any]:
    """
    Compulsory startup sync.
    Returns dict with keys: ok, skipped, offline, error, result
    """
    cfg = ensure_sync_config()
    if not cfg or not config_ready(cfg):
        return {
            "ok": False,
            "skipped": False,
            "offline": False,
            "error": "Sync settings are required before the app can open.",
            "result": None,
        }

    if not cfg.get("sync_on_start", True):
        return {"ok": True, "skipped": True, "offline": False, "error": None, "result": None}

    if not _has_network(cfg.get("database_url") or ""):
        if cfg.get("required", True):
            # Still allow offline work, but warn clearly.
            _show_message(
                "Offline mode",
                "No internet — cloud sync could not run.\n"
                "The app will open with local data.\n"
                "Sync will run automatically when you are online next time.",
                error=False,
            )
        return {
            "ok": True,
            "skipped": True,
            "offline": True,
            "error": "No network",
            "result": None,
        }

    try:
        result = perform_sync(cfg, reason="startup")
        logging.info("Startup sync OK for %s", cfg.get("username"))
        return {
            "ok": True,
            "skipped": False,
            "offline": False,
            "error": None,
            "result": result,
        }
    except SystemExit as exc:
        msg = str(exc) or "Sync failed"
        logging.exception("Startup sync failed")
        _show_message(
            "Sync failed — opening offline",
            f"{msg}\n\nCheck your cloud URL / username / password later.\n"
            "The app will open with local data now.",
            error=True,
        )
        return {
            "ok": True,
            "skipped": True,
            "offline": True,
            "error": msg,
            "result": None,
        }
    except Exception as exc:
        logging.exception("Startup sync failed")
        _show_message(
            "Sync failed — opening offline",
            f"{exc}\n\nThe app will open with local data.\n"
            "Fix cloud sync settings and restart when ready.",
            error=True,
        )
        return {
            "ok": True,
            "skipped": True,
            "offline": True,
            "error": str(exc),
            "result": None,
        }


def sync_on_shutdown() -> None:
    cfg = load_sync_config()
    if not cfg.get("sync_on_exit", True):
        return
    if not config_ready(cfg):
        return
    if not _has_network(cfg.get("database_url") or ""):
        logging.info("Skip exit sync: offline")
        return
    try:
        perform_sync(cfg, reason="shutdown")
        logging.info("Shutdown sync OK")
    except Exception:
        logging.exception("Shutdown sync failed")


def sync_now() -> dict[str, Any]:
    """Manual sync from the in-app Sync button."""
    cfg = ensure_sync_config()
    if not cfg or not config_ready(cfg):
        return {
            "ok": False,
            "error": "Sync settings are missing. Restart the app to configure cloud sync.",
        }

    if not _has_network(cfg.get("database_url") or ""):
        return {
            "ok": False,
            "offline": True,
            "error": "No internet connection. Connect to the network and try again.",
        }

    try:
        result = perform_sync(cfg, reason="manual")
        return {
            "ok": True,
            "mode": result.get("mode"),
            "message": "Local and cloud databases are synced.",
            "local_counts": result.get("local_counts") or {},
            "remote_counts": result.get("remote_counts") or {},
            "last_sync_at": datetime.now(timezone.utc).isoformat(),
        }
    except SystemExit as exc:
        return {"ok": False, "error": str(exc) or "Sync failed"}
    except Exception as exc:
        logging.exception("Manual sync failed")
        msg = str(exc) or "Sync failed"
        if "missing =" in msg.lower() or "connection string" in msg.lower():
            msg = (
                "Cloud database URL looks invalid. "
                "Delete %LOCALAPPDATA%\\EuroglassHardware\\sync_config.json "
                "and restart the app, then paste the Supabase URI without quotes."
            )
        return {"ok": False, "error": msg}
