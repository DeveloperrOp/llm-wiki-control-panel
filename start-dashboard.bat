@echo off
REM LLM Wiki Control Panel — запуск Flask-дашборда
REM Двойной клик по этому файлу запускает сервер + открывает браузер

chcp 65001 >nul
cd /d "%~dp0"

echo LLM Wiki Control Panel
echo ----------------------
echo Starting Flask on http://localhost:5757 ...
echo Browser will open automatically in a moment.
echo Close this window to stop the dashboard.
echo.

python "%~dp0scripts\dashboard.py"

pause
