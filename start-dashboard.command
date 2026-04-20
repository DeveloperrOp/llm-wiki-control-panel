#!/bin/bash
# LLM Wiki Control Panel — запуск Flask-дашборда на macOS.
# Двойной клик в Finder запускает сервер + открывает браузер.
#
# Первый раз: Finder может заблокировать выполнение. Выход:
#   1) Правый клик по файлу → Open → Confirm
#   2) Или в Терминале: chmod +x start-dashboard.command

set -e

cd "$(dirname "$0")"

echo "LLM Wiki Control Panel"
echo "----------------------"
echo "Starting Flask on http://localhost:5757 ..."
echo "Browser will open automatically in a moment."
echo "Close this Terminal window to stop the dashboard."
echo ""

# Python 3 может называться по-разному — ищем по PATH
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "❌ Python не найден. Установи через 'brew install python' или с python.org"
    read -p "Нажми Enter чтобы закрыть..."
    exit 1
fi

"$PYTHON" "$(pwd)/scripts/dashboard.py"

echo ""
read -p "Dashboard остановлен. Enter чтобы закрыть окно..."
