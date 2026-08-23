"""
Bidirectional sync CLI: local offline SQLite ↔ Supabase/Render Postgres.

Examples:

  # Both ways (default). Conflicts keep LOCAL changes.
  python desktop/sync_db.py --database-url "postgresql://..." --mode sync

  # Cloud → PC only
  python desktop/sync_db.py --database-url "postgresql://..." --mode pull

  # PC → cloud only
  python desktop/sync_db.py --database-url "postgresql://..." --mode push

  # Both ways, conflicts keep CLOUD changes
  python desktop/sync_db.py --database-url "postgresql://..." --mode sync --prefer remote
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from desktop.paths import database_path
from desktop.sync import load_sync_state, save_sync_state
from desktop.sync_engine import run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bidirectional sync between offline SQLite and cloud Postgres."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("RENDER_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "",
        help="Supabase/Render Postgres URI",
    )
    parser.add_argument(
        "--mode",
        choices=("sync", "pull", "push"),
        default="sync",
        help="sync=both ways (default), pull=cloud→PC, push=PC→cloud",
    )
    parser.add_argument(
        "--prefer",
        choices=("local", "remote"),
        default="local",
        help="On sync conflicts, keep local (default) or remote rows",
    )
    parser.add_argument("--username", default=os.environ.get("SYNC_USERNAME") or "")
    parser.add_argument("--password", default=os.environ.get("SYNC_PASSWORD") or "")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    db_url = (args.database_url or "").strip()
    if not db_url or db_url.lower().startswith("sqlite:"):
        print(
            'Required: --database-url "postgresql://..."\n'
            "(Supabase → Connect → URI, or Render Postgres External URL)",
            file=sys.stderr,
        )
        return 1

    username = (args.username or "").strip() or input("App username: ").strip()
    password = args.password or getpass.getpass("App password: ")
    if not username or not password:
        print("Username and password are required.", file=sys.stderr)
        return 1

    db_file = database_path()
    print(f"Local DB : {db_file}")
    print(f"Mode     : {args.mode}")
    if args.mode == "sync":
        print(f"Prefer   : {args.prefer} (on conflicts)")
    print()
    if args.mode == "pull":
        print("Cloud data will REPLACE local offline data for this account.")
    elif args.mode == "push":
        print("Local data will REPLACE cloud data for this account.")
    else:
        print(
            "Both sides will be merged.\n"
            f"If the same row differs, the {args.prefer.upper()} copy is kept,\n"
            "then both databases are updated to the merged result."
        )
    if not args.yes:
        confirm = input("Type YES to continue: ").strip()
        if confirm != "YES":
            print("Cancelled.")
            return 1

    try:
        result = run_sync(
            database_url=db_url,
            db_path=db_file,
            username=username,
            password=password,
            mode=args.mode,
            prefer=args.prefer,
        )
    except SystemExit as exc:
        print(str(exc) or "Sync failed.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1

    state = load_sync_state()
    state["last_sync_at"] = result["synced_at"]
    state["last_status"] = f"ok_{args.mode}"
    state["last_message"] = json.dumps(result.get("stats") or {})
    state["counts"] = {
        "local": result.get("local_counts") or {},
        "remote": result.get("remote_counts") or {},
    }
    save_sync_state(state)

    print("\nSync complete.")
    print(f"Account: {result['username']} (id {result['user_id']})")
    stats = result.get("stats") or {}
    if args.mode == "sync":
        print(
            "Merge:",
            f"only_local={stats.get('only_local', 0)}",
            f"only_remote={stats.get('only_remote', 0)}",
            f"same={stats.get('same', 0)}",
            f"kept_local={stats.get('conflict_local', 0)}",
            f"kept_remote={stats.get('conflict_remote', 0)}",
        )
    print("\nNext: python desktop\\launcher.py")
    print("Log in with the same app username/password.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
