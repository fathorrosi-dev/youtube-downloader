@echo off
setlocal
set PYTHONUTF8=1
python "%~dp0maintenance.py" migrate %*
exit /b %ERRORLEVEL%
