@echo off
setlocal
set PYTHONUTF8=1
python "%~dp0maintenance.py" setup %*
exit /b %ERRORLEVEL%
