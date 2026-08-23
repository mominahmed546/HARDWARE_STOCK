# Euroglass Hardware — Offline Windows App

Run the same Flask stock app on a PC **without internet**, using a local
SQLite database. Your live site on Render stays as-is.

## Quick start (development)

From the project root:

```bash
pip install -r requirements.txt -r requirements-desktop.txt
python desktop/launcher.py
```

- Database file: `%LOCALAPPDATA%\EuroglassHardware\euroglass_stock.db` (Windows)
  or `~/.euroglasshardware/euroglass_stock.db` (Linux/macOS)
- First launch creates tables from `schema_sqlite.sql`

## Sync your Render (online) data into the offline app

**Option A — after this PR is deployed on Render** (uses your website login):

```bat
cd C:\Users\DELL\Desktop\HARDWARE_STOCK
git pull
python desktop\pull_from_render.py --url https://YOUR-APP.onrender.com
```

Enter your **Render website** username and password when asked. Type `YES` to replace local data.

**Option B — right now, without waiting for deploy** (uses Render database URL):

1. Open [Render Dashboard](https://dashboard.render.com) → your **Postgres** database
2. Copy **External Database URL**
3. Run:

```bat
python desktop\pull_from_render.py --database-url "postgresql://..."
```

Then:

```bat
python desktop\launcher.py
```

Log in with the **same** username/password as on the website.

## Build Windows `.exe`

On a **Windows** machine with Python 3.11+:

```bat
pip install -r requirements.txt -r requirements-desktop.txt
pyinstaller desktop\euroglass.spec
```

Output: `dist\EuroglassHardware.exe`

| Variable | Purpose |
|----------|---------|
| `SYNC_REMOTE_URL` | Render site URL for sync |
| `RENDER_DATABASE_URL` | External Postgres URL (optional pull method) |
| `DESKTOP_PORT` | Fixed local port (default: random free port) |
| `SECRET_KEY` | Flask session secret |

## Online + offline

| Mode | Database | Status |
|------|----------|--------|
| Render (online) | Postgres | Unchanged |
| Windows app | Local SQLite | Works offline |
| Pull Render → PC | `pull_from_render.py` | Supported |
| Push PC → Render | — | Next step |
