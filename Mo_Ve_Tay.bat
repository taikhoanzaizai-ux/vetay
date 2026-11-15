@echo off
setlocal
cd /d "%~dp0"
if not defined VETAY_ENGINE set "VETAY_ENGINE=%USERPROFILE%\Downloads\whiteboard-animation-vi-main"
set "PY=%VETAY_ENGINE%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Can cai Whiteboard Animation VI va .venv truoc.
    pause
    exit /b 1
)
:run
"%PY%" app.py
if "%ERRORLEVEL%"=="42" goto run
pause
