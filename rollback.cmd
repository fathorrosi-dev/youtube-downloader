@echo off
setlocal
set PYTHONUTF8=1
python "%~dp0maintenance.py" rollback %*
exit /b %ERRORLEVEL%
