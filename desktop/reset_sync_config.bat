@echo off
cd /d "%~dp0\.."
echo Clearing broken cloud sync URL...
python desktop\reset_sync_config.py --clear-only
echo.
echo Done. Start the app again and paste ONLY the postgresql:// URI
echo (no python command, no --mode sync, no quotes).
pause
