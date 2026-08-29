"""Saved cloud sync settings for the offline Windows app."""

from __future__ import annotations

import json
import os
from pathlib import Path

from desktop.paths import data_dir


def sync_config_path() -> Path:
    return data_dir() / "sync_config.json"


def _normalize_url(url: str) -> str:
    try:
        from desktop.sync_engine import normalize_database_url

        return normalize_database_url(url)
    except Exception:
        text = (url or "").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"', "`"}:
            text = text[1:-1].strip()
        if text.startswith("postgres://"):
            text = "postgresql://" + text[len("postgres://") :]
        return text


def load_sync_config() -> dict:
    path = sync_config_path()
    env_url = _normalize_url(
        os.environ.get("SYNC_DATABASE_URL")
        or os.environ.get("RENDER_DATABASE_URL")
        or ""
    )
    defaults = {
        "database_url": env_url,
        "username": (os.environ.get("SYNC_USERNAME") or "").strip(),
        "password": os.environ.get("SYNC_PASSWORD") or "",
        "mode": "sync",
        "prefer": "local",
        "required": True,
        "sync_on_start": True,
        "sync_on_exit": True,
    }
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    if not isinstance(data, dict):
        return defaults
    merged = dict(defaults)
    merged.update({k: v for k, v in data.items() if v is not None})
    raw_url = str(merged.get("database_url") or "")
    if raw_url:
        cleaned = _normalize_url(raw_url)
        merged["database_url"] = cleaned
        # Rewrite file when paste left quotes/junk so Sync Now keeps working.
        if cleaned and cleaned != raw_url.strip():
            try:
                save_sync_config(merged)
            except Exception:
                pass
    return merged


def save_sync_config(config: dict) -> None:
    path = sync_config_path()
    clean = {
        "database_url": _normalize_url(config.get("database_url") or ""),
        "username": (config.get("username") or "").strip(),
        "password": config.get("password") or "",
        "mode": config.get("mode") or "sync",
        "prefer": config.get("prefer") or "local",
        "required": bool(config.get("required", True)),
        "sync_on_start": bool(config.get("sync_on_start", True)),
        "sync_on_exit": bool(config.get("sync_on_exit", True)),
    }
    path.write_text(json.dumps(clean, indent=2), encoding="utf-8")


def config_ready(config: dict | None = None) -> bool:
    cfg = config or load_sync_config()
    url = _normalize_url(cfg.get("database_url") or "")
    user = (cfg.get("username") or "").strip()
    password = cfg.get("password") or ""
    return bool(url) and not url.lower().startswith("sqlite:") and bool(user) and bool(password)
