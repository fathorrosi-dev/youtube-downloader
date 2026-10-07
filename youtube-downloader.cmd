@echo off
setlocal
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Jalankan setup.cmd terlebih dahulu. 1>&2
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0app.py" %*
exit /b %ERRORLEVEL%
