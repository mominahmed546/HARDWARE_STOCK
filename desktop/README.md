# Euroglass Hardware — Offline Windows App

Run the same Flask stock app on a PC **without internet**, using a local
SQLite database. Your live site (Render + Supabase Postgres) stays as-is.

## Quick start

```bat
cd C:\Users\DELL\Desktop\HARDWARE_STOCK
pip install -r requirements.txt -r requirements-desktop.txt
python desktop\launcher.py
```

Local DB: `%LOCALAPPDATA%\EuroglassHardware\euroglass_stock.db`

## Automatic sync (required)

When you open `EuroglassHardware.exe` / `desktop/launcher.py`:

1. **On start** — syncs local ↔ cloud automatically  
2. **On close** — syncs again to upload any local changes  

First launch asks once for:
- Supabase Postgres URI  
- App username / password  

Saved under:
`%LOCALAPPDATA%\EuroglassHardware\sync_config.json`

You do **not** need to run CMD sync every time anymore.

Manual sync is still available if needed:

```bat
python desktop\sync_db.py --database-url "postgresql://..." --mode sync
```

## Download the Windows app from GitHub

After CI builds (or after a manual workflow run), get the installer from:

1. Repo → **Releases** (right side on GitHub)
2. Download **EuroglassHardware.exe**
3. Double-click to open

Or open:
`https://github.com/mominahmed546/HARDWARE_STOCK/releases`

To rebuild manually on GitHub: **Actions → Build Windows Offline App → Run workflow**.

## Notes

- Sync is **per app account** (your login).
- Keep your database URI private.
- If the live Render site uses this same Supabase DB, push/sync updates what the website shows.
