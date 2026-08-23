"""
Sync foundation: local Windows DB ↔ Render (cloud) Postgres.

Phase 1 ships the offline app + local SQLite.
Phase 2 fills in push/pull against a cloud sync API.

Environment (optional, for when sync is enabled):
  SYNC_REMOTE_URL   e.g. https://hardware-stock.onrender.com
  SYNC_API_TOKEN    account token (future)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from desktop.paths import sync_state_path


def load_sync_state() -> dict:
    path = sync_state_path()
    if not path.exists():
        return {
            "last_sync_at": None,
            "last_status": "never",
            "remote_url": os.environ.get("SYNC_REMOTE_URL") or "",
            "notes": "Sync not configured yet. Local offline DB is primary until sync is enabled.",
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_sync_at": None, "last_status": "error", "remote_url": ""}


def save_sync_state(state: dict) -> None:
    path = sync_state_path()
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def remote_configured() -> bool:
    state = load_sync_state()
    url = (state.get("remote_url") or os.environ.get("SYNC_REMOTE_URL") or "").strip()
    return bool(url)


def sync_now() -> dict:
    """
    Attempt a sync cycle.

    Phase 1: records intent and returns a clear 'not implemented' status
    so the UI/docs can explain next steps without failing the desktop app.
    """
    state = load_sync_state()
    state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    if not remote_configured():
        state["last_status"] = "skipped_no_remote"
        state["last_message"] = (
            "Set SYNC_REMOTE_URL to your Render site to enable cloud sync later."
        )
        save_sync_state(state)
        return state

    # Phase 2 will:
    # 1) push local changes since last_sync_at
    # 2) pull remote changes
    # 3) apply conflict rules (latest-wins / field-level)
    state["last_status"] = "pending_implementation"
    state["last_message"] = (
        "Cloud sync API is not deployed yet. Offline local database works; "
        "sync with Render will be added in the next phase."
    )
    save_sync_state(state)
    return state
