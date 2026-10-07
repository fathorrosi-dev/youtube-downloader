@echo off
setlocal
set PYTHONUTF8=1
python "%~dp0maintenance.py" update %*
exit /b %ERRORLEVEL%
