"""Clear or rewrite broken desktop sync settings.

Double-click / run when Sync fails with sslmode / --mode sync paste junk:

  python desktop\\reset_sync_config.py

  python desktop\\reset_sync_config.py --database-url "postgresql://..." --username YOUR_USER
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from desktop.sync_config import clear_database_url, load_sync_config, save_sync_config, sync_config_path
from desktop.sync_engine import is_valid_cloud_database_url, normalize_database_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset Euroglass desktop sync_config.json")
    parser.add_argument("--database-url", default="", help="Clean Supabase postgresql:// URI only")
    parser.add_argument("--username", default="", help="App username (same as website)")
    parser.add_argument("--password", default="", help="App password (prompted if omitted with --username)")
    parser.add_argument(
        "--clear-only",
        action="store_true",
        help="Only delete the saved database_url (prompt on next app start)",
    )
    args = parser.parse_args(argv)

    path = sync_config_path()
    print(f"Config file: {path}")

    if args.clear_only or not (args.database_url or "").strip():
        clear_database_url()
        print("Cleared database_url.")
        print("Next app start will ask you to paste the URI again (URI only, no python/--mode).")
        return 0

    url = normalize_database_url(args.database_url)
    if not is_valid_cloud_database_url(url):
        print("Invalid database URL after cleanup.", file=sys.stderr)
        print("Paste only: postgresql://user:pass@host:5432/postgres?sslmode=require", file=sys.stderr)
        return 2

    cfg = load_sync_config()
    cfg["database_url"] = url
    if args.username.strip():
        cfg["username"] = args.username.strip()
    password = args.password
    if args.username.strip() and not password:
        password = getpass.getpass("App password: ")
    if password:
        cfg["password"] = password
    save_sync_config(cfg)
    print("Saved clean database_url.")
    print(url.split("@")[-1] if "@" in url else url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
