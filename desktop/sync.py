"""
Sync foundation: local Windows DB ↔ Render (cloud) Postgres.

Pull your Render account into the offline app:

  python desktop/pull_from_render.py --url https://YOUR-APP.onrender.com

Or (no redeploy needed) with the Render External Database URL:

  python desktop/pull_from_render.py --database-url "postgresql://..."

Environment (optional):
  SYNC_REMOTE_URL       e.g. https://hardware-stock.onrender.com
  RENDER_DATABASE_URL   External Postgres URL from Render
  SYNC_USERNAME / SYNC_PASSWORD
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
                "Run: python desktop/pull_from_render.py --url https://YOUR-APP.onrender.com"
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
    """
    Record sync intent. Full pull is done via desktop/pull_from_render.py
    (needs username/password interactively).
    """
    state = load_sync_state()
    state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    if not remote_configured():
        state["last_status"] = "skipped_no_remote"
        state["last_message"] = (
            "Set SYNC_REMOTE_URL or run desktop/pull_from_render.py --url ..."
        )
        save_sync_state(state)
        return state

    state["last_status"] = "use_pull_script"
    state["last_message"] = (
        "Run: python desktop/pull_from_render.py --url "
        f"{state.get('remote_url') or os.environ.get('SYNC_REMOTE_URL')}"
    )
    save_sync_state(state)
    return state
