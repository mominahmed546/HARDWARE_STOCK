"""
Sync helpers for the offline Windows app ↔ cloud Postgres.

Use the CLI for real sync:

  python desktop/sync_db.py --database-url "postgresql://..." --mode sync

Modes: sync (both ways), pull (cloud→PC), push (PC→cloud)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from desktop.paths import sync_state_path


def load_sync_state() -> dict:
    path = sync_state_path()
    if not path.exists():
        return {
            "last_sync_at": None,
            "last_status": "never",
            "remote_url": os.environ.get("SYNC_REMOTE_URL") or "",
            "notes": (
                'Run: python desktop/sync_db.py --database-url "postgresql://..." --mode sync'
            ),
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
    state = load_sync_state()
    state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    state["last_status"] = "use_cli"
    state["last_message"] = (
        'Run: python desktop/sync_db.py --database-url "postgresql://..." --mode sync'
    )
    save_sync_state(state)
    return state
