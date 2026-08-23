"""
# Euroglass Hardware — Offline Windows App

Run the same Flask stock app on a PC **without internet**, using a local
SQLite database. Your live site on Render stays as-is; cloud sync is Phase 2.

## Quick start (development)

From the project root:

```bash
pip install -r requirements.txt -r requirements-desktop.txt
python desktop/launcher.py
```

- Database file: `%LOCALAPPDATA%\\EuroglassHardware\\euroglass_stock.db` (Windows)
  or `~/.euroglasshardware/euroglass_stock.db` (Linux/macOS)
- First launch creates tables from `schema_sqlite.sql`
- Register a local account (email **or** phone), then use the app offline

If `pywebview` cannot open a window (headless Linux), the server still starts —
open the printed `http://127.0.0.1:PORT/` URL in a browser.

## Build Windows `.exe`

On a **Windows** machine with Python 3.11+:

```bat
pip install -r requirements.txt -r requirements-desktop.txt
pyinstaller desktop\\euroglass.spec
```

Output: `dist\\EuroglassHardware.exe` (double-click to run).

Optional env vars:

| Variable | Purpose |
|----------|---------|
| `SYNC_REMOTE_URL` | Future sync target, e.g. `https://hardware-stock.onrender.com` |
| `DESKTOP_PORT` | Fixed local port (default: random free port) |
| `SECRET_KEY` | Flask session secret (set a unique value for installs you care about) |

## Online + offline (optimal plan)

| Mode | Database | Status |
|------|----------|--------|
| Render (online) | Postgres | Unchanged |
| Windows app | Local SQLite | Phase 1 (this branch) |
| Sync both ways | API + conflict rules | Phase 2 (stub in `desktop/sync.py`) |

Phase 2 will push/pull changes between the local DB and Render so both stay aligned.
"""
