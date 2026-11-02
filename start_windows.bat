@echo off
cd /d "%~dp0"
if not exist config.json copy /Y config.example.json config.json >nul
if not exist .venv\Scripts\python.exe py -3.11 -m venv .venv
if not exist .venv\Scripts\python.exe (
  echo Python 3.11 or newer is required.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install -q -e .
if errorlevel 1 pause & exit /b 1
.venv\Scripts\python.exe -m market_watcher serve --config config.json
if errorlevel 1 pause
