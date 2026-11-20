@echo off
rem Double-click launcher for the settings UI when running from source.
rem Uses the project virtual environment in .venv (see guide/eztable.md).
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0src"
if not exist "..\.venv\Scripts\python.exe" (
    echo [ERROR] 找不到 .venv，請先在專案資料夾執行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -r requirement.txt
    pause
    exit /b 1
)
"..\.venv\Scripts\python.exe" settings.py
pause
