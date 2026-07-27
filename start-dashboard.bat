@echo off
REM Steam Dashboard launcher (Windows). Double-click to start.
REM Usage: start-dashboard.bat [--no-browser]
setlocal

REM Internal: delayed browser open, re-invoked as a background copy of this script.
if /i "%~1"=="--open" (
    timeout /t 3 /nobreak >nul
    start "" "http://localhost:%~2"
    exit /b 0
)

cd /d "%~dp0"

REM Find a Python interpreter.
set "PY="
for %%C in (py python python3) do if not defined PY (
    where %%C >nul 2>&1 && set "PY=%%C"
)
if not defined PY (
    echo [ERROR] Python was not found on PATH.
    echo         Install Python 3 from https://www.python.org/downloads/
    echo         and make sure "Add python.exe to PATH" is checked.
    pause
    exit /b 1
)

REM Read the configured port from the settings DB; fall back to 8081.
set "PORT=8081"
if exist "steam_dashboard.db" (
    for /f "usebackq tokens=*" %%P in (`%PY% -c "import sqlite3,json;r=sqlite3.connect('steam_dashboard.db').execute('SELECT value FROM settings WHERE key=?',('dashboard',)).fetchone();print((json.loads(r[0]) if r else {}).get('port',8081))" 2^>nul`) do set "PORT=%%P"
)

if /i "%~1"=="--no-browser" (
    echo [INFO] Browser auto-open disabled.
) else (
    start "" /min cmd /c call "%~f0" --open %PORT%
)

echo [INFO] Starting Steam Dashboard on port %PORT% ... (close this window or press Ctrl+C to stop)
%PY% dashboard.py
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [ERROR] Dashboard exited with code %RC%.
    pause
)
exit /b %RC%
