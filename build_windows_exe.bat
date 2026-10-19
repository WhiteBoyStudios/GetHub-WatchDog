@echo off
REM Builds GetHubWatchDog.exe from gethub_watchdog.py (needs Python installed)
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --windowed --name GetHubWatchDog gethub_watchdog.py
echo.
echo Done. Your app is in the "dist" folder: dist\GetHubWatchDog.exe
pause
