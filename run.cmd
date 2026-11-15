@echo off
setlocal
cd /d "%~dp0"
if not exist "backend\.venv\Scripts\python.exe" (
  echo [ERROR] Missing backend\.venv\Scripts\python.exe. See README.md for setup.
  pause
  exit /b 1
)
"%~dp0backend\.venv\Scripts\python.exe" "%~dp0scripts\runtime_launcher.py"
set "AUTODUB_EXIT=%ERRORLEVEL%"
if not "%AUTODUB_EXIT%"=="0" pause
exit /b %AUTODUB_EXIT%
