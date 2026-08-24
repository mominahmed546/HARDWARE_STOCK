# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Euroglass Hardware (Windows offline app).

Build on Windows (recommended):
  pip install -r requirements.txt -r requirements-desktop.txt
  pyinstaller desktop/euroglass.spec
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
root = Path(SPECPATH).resolve().parents[0]

datas = [
    (str(root / "templates"), "templates"),
    (str(root / "static"), "static"),
    (str(root / "schema_sqlite.sql"), "."),
    (str(root / "desktop" / "euroglass.ico"), "desktop"),
]

hiddenimports = [
    "app",
    "app.auth",
    "app.cash",
    "app.customers",
    "app.dashboard",
    "app.invoices",
    "app.items",
    "app.ledger",
    "app.payments",
    "app.profit",
    "app.purchases",
    "app.quotations",
    "app.suppliers",
    "app.categories",
    "app.reports",
    "app.sync_api",
    "app.tenancy",
    "config",
    "desktop.paths",
    "desktop.sync",
    "desktop.sync_engine",
    "desktop.sync_config",
    "desktop.auto_sync",
    "webview",
    "openpyxl",
    "psycopg",
    "psycopg_binary",
    "psycopg_pool",
    "psycopg_pool.pool",
]

binaries = []
tmp_ret = collect_all("webview")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Cloud sync needs the Postgres driver inside the .exe.
for pkg in ("psycopg", "psycopg_binary", "psycopg_pool"):
    try:
        pkg_ret = collect_all(pkg)
        datas += pkg_ret[0]
        binaries += pkg_ret[1]
        hiddenimports += pkg_ret[2]
    except Exception:
        pass

a = Analysis(
    [str(root / "desktop" / "launcher.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="EuroglassHardware",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "desktop" / "euroglass.ico"),
)
