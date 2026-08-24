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

## Bidirectional sync (PC ↔ cloud)

Uses your **Supabase / Postgres URI** and your **app username/password**.

```bat
cd C:\Users\DELL\Desktop\HARDWARE_STOCK
git pull

python desktop\sync_db.py --database-url "postgresql://postgres:PASSWORD@db.XXXX.supabase.co:5432/postgres" --mode sync
```

| Mode | What it does |
|------|----------------|
| `--mode sync` | **Both ways** (default). Merges local + cloud, then writes the result to **both** |
| `--mode pull` | Cloud → PC only (cloud wins) |
| `--mode push` | PC → cloud only (local wins) |

Conflict option for `--mode sync`:

- `--prefer local` (default) — if the same row changed on both sides, keep **PC**
- `--prefer remote` — keep **cloud**

Example (cloud wins on conflicts):

```bat
python desktop\sync_db.py --database-url "postgresql://..." --mode sync --prefer remote
```

After sync:

```bat
python desktop\launcher.py
```

Log in with the same app username/password.

## First-time: get data onto the PC

If the PC is empty, run sync once (or pull):

```bat
python desktop\sync_db.py --database-url "postgresql://..." --mode pull
```

Then work offline. When you have internet again:

```bat
python desktop\sync_db.py --database-url "postgresql://..." --mode sync
```

That uploads PC changes and downloads any new cloud changes.

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
