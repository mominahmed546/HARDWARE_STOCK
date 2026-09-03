@echo off
REM Build EuroglassHardware.exe on Windows.
REM Run from the project root in "x64 Native Tools" or a normal cmd with Python on PATH.

cd /d "%~dp0\.."
python -m pip install -r requirements.txt -r requirements-desktop.txt
python -m PyInstaller desktop\euroglass.spec
echo.
echo Built: dist\EuroglassHardware.exe
pause
