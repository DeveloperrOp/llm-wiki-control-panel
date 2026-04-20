#!/usr/bin/env python3
"""Интерактивный установщик LLM Wiki Control Panel.

Делает 5 вещей:
1. Проверяет что нужные программы установлены (Python 3.12+, Claude CLI)
2. Устанавливает Python-зависимости из requirements.txt
3. Создаёт папку под vault первого проекта (если пользователь согласен)
4. Генерирует config/project-map.json с путями пользователя
5. Прописывает хуки в ~/.claude/settings.json (merge, не затирая существующее)

Запуск:
    python install.py          # Windows
    python3 install.py         # macOS / Linux

Можно запускать повторно — скрипт идемпотентен (не ломает то что уже настроено).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOKS_DIR = HERE / "hooks"
CONFIG_DIR = HERE / "config"
REQUIREMENTS = HERE / "requirements.txt"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# --- Цвета для терминала (ANSI) ---
if IS_WINDOWS:
    # Windows 10+ поддерживает ANSI, но нужно включить virtual terminal processing
    try:
        os.system("")  # включает ANSI
    except Exception:
        pass

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


def say(msg: str = "") -> None:
    print(msg)


def ok(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}⚠{RESET}  {msg}")


def err(msg: str) -> None:
    print(f"{RED}✗{RESET} {msg}")


def heading(msg: str) -> None:
    print()
    print(f"{BOLD}── {msg} ──{RESET}")


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f"{question}{suffix}: ").strip()
    return ans or default


def ask_yesno(question: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    ans = input(f"{question} {suffix}: ").strip().lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes", "да", "д")


# =====================================================================
# 1. Проверка зависимостей
# =====================================================================

def check_python() -> bool:
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 10):
        err(f"Нужен Python 3.10+, у тебя {major}.{minor}")
        return False
    ok(f"Python {major}.{minor}")
    return True


def check_claude_cli() -> bool:
    if shutil.which("claude") is None:
        warn("Claude CLI не найден в PATH")
        say("  Установи его:")
        say("    npm install -g @anthropic-ai/claude-code")
        say("  (нужен Node.js: https://nodejs.org)")
        return False
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        version = (result.stdout or result.stderr or "unknown").strip()
        ok(f"Claude CLI: {version}")
        return True
    except (OSError, subprocess.TimeoutExpired) as e:
        warn(f"claude установлен но не отвечает: {e}")
        return False


def check_git() -> bool:
    if shutil.which("git"):
        ok("Git доступен")
        return True
    warn("Git не найден (опционально, нужен только для обновлений)")
    return True  # не блокирует установку


# =====================================================================
# 2. Pip install
# =====================================================================

def pip_install() -> bool:
    heading("Установка Python-зависимостей")

    if not REQUIREMENTS.exists():
        err(f"Нет файла {REQUIREMENTS}")
        return False

    # Пробуем: python -m pip install -r requirements.txt
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    say(f"  → {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        err("pip install завис (>3 мин). Попробуй вручную.")
        return False

    if result.returncode != 0:
        # Возможно нет прав — пробуем --user
        say(f"  {RED}Ошибка{RESET}. Пробую с --user...")
        cmd = [sys.executable, "-m", "pip", "install", "--user", "-r", str(REQUIREMENTS)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    if result.returncode != 0:
        err("pip install не сработал. Последние строки:")
        for line in (result.stderr or result.stdout).splitlines()[-10:]:
            say(f"  {line}")
        return False

    ok("Зависимости установлены")
    return True


# =====================================================================
# 3. Создать vault folder
# =====================================================================

def create_vault_structure(vault_path: Path) -> bool:
    subdirs = [
        "raw/chats", "raw/articles", "raw/docs", "raw/assets",
        "wiki/entities", "wiki/concepts", "wiki/sources",
    ]
    try:
        for sub in subdirs:
            (vault_path / sub).mkdir(parents=True, exist_ok=True)
        ok(f"Структура vault создана: {vault_path}")
        return True
    except OSError as e:
        err(f"Не удалось создать папки: {e}")
        return False


# =====================================================================
# 4. project-map.json
# =====================================================================

def setup_project_map(vault_path: Path, project_name: str, unassigned_dir: Path) -> bool:
    heading("Настройка project-map.json")
    target = CONFIG_DIR / "project-map.json"
    example = CONFIG_DIR / "project-map.example.json"

    if target.exists():
        if not ask_yesno(f"  {target.name} уже существует. Перезаписать?", default_yes=False):
            say("  Пропущено — оставил как есть")
            return True

    def _norm(p: Path) -> str:
        return str(p).replace("\\", "/")

    config = {
        "version": 1,
        "vault_base": _norm(vault_path.parent),
        "mappings": [
            {
                "name": project_name,
                "cwd_patterns": [f"*{project_name}*"],
                "vault_root": _norm(vault_path),
                "auto_ingest": False,
                "lint_schedule": None,
                "context_limit": 10000,
            }
        ],
        "unassigned_root": _norm(unassigned_dir),
    }

    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        ok(f"Записан {target}")
        return True
    except OSError as e:
        err(f"Не удалось записать config: {e}")
        return False


# =====================================================================
# 5. Прописать хуки в ~/.claude/settings.json
# =====================================================================

def claude_settings_path() -> Path:
    home = Path.home()
    return home / ".claude" / "settings.json"


def install_hooks() -> bool:
    heading("Подключение хуков Claude Code")

    settings_path = claude_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Загружаем существующие настройки (если есть)
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            warn(f"{settings_path} битый — будет создан бэкап")
            backup = settings_path.with_suffix(".json.bak")
            try:
                shutil.copy2(settings_path, backup)
                say(f"  Бэкап: {backup}")
            except OSError:
                pass
            existing = {}

    # Команда Python для запуска хука
    python_exec = "python" if IS_WINDOWS else "python3"

    def hook_cmd(name: str) -> str:
        script = HOOKS_DIR / name
        return f"{python_exec} {str(script).replace(chr(92), '/')}"

    our_hooks = {
        "SessionStart": [{"hooks": [{"type": "command", "command": hook_cmd("session-start.py")}]}],
        "SessionEnd":   [{"hooks": [{"type": "command", "command": hook_cmd("session-end.py")}]}],
        "PreCompact":   [{"hooks": [{"type": "command", "command": hook_cmd("pre-compact.py")}]}],
    }

    # Merge: наши хуки добавляются/обновляются, остальные настройки сохраняются
    if "hooks" not in existing:
        existing["hooks"] = {}

    # Для каждого события: если наш хук уже есть (по пути скрипта) — не дублируем
    for event, new_entries in our_hooks.items():
        our_path = str(HOOKS_DIR / f"{event.lower().replace('_', '-')}.py").replace("\\", "/")
        # Нормализуем event name для наших файлов
        file_map = {
            "SessionStart": "session-start.py",
            "SessionEnd":   "session-end.py",
            "PreCompact":   "pre-compact.py",
        }
        our_path = str(HOOKS_DIR / file_map[event]).replace("\\", "/")

        existing_entries = existing["hooks"].get(event, [])
        # Фильтруем наши старые записи (по пути к скрипту) — и добавляем новые
        filtered = []
        for e in existing_entries:
            if not isinstance(e, dict):
                continue
            hooks_list = e.get("hooks") or []
            is_ours = any(
                isinstance(h, dict) and our_path in (h.get("command") or "")
                for h in hooks_list
            )
            if not is_ours:
                filtered.append(e)
        existing["hooks"][event] = filtered + new_entries

    try:
        # Атомарная запись через tempfile
        import tempfile
        fd, tmp = tempfile.mkstemp(
            prefix="settings.", suffix=".tmp", dir=str(settings_path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp, settings_path)
        ok(f"Хуки подключены: {settings_path}")
        return True
    except OSError as e:
        err(f"Не удалось записать settings: {e}")
        return False


# =====================================================================
# MAIN
# =====================================================================

def main() -> int:
    print()
    print(f"{BOLD}LLM Wiki Control Panel — установщик{RESET}")
    print("=" * 50)

    # 1. Проверяем зависимости
    heading("Проверка зависимостей")
    if not check_python():
        return 1
    has_claude = check_claude_cli()
    check_git()
    if not has_claude:
        if not ask_yesno("Продолжить без Claude CLI? (хуки не будут работать пока не установишь)", default_yes=False):
            return 1

    # 2. Pip install
    if not pip_install():
        if not ask_yesno("Продолжить без установки библиотек?", default_yes=False):
            return 1

    # 3. Путь до vault
    heading("Настройка проекта")
    say("Нужна папка, где будет лежать база знаний твоего первого проекта.")
    say("(Это ОТДЕЛЬНАЯ папка от кода системы.)")
    say("")

    if IS_WINDOWS:
        default_base = str(Path("C:/Obsidian")).replace("\\", "/")
    else:
        default_base = str(Path.home() / "Obsidian")

    base = ask("Корневая папка для всех vault'ов", default_base)
    project_name = ask("Имя первого проекта", "My Project")
    vault_path = Path(base) / project_name
    unassigned = Path(base) / ".unassigned"

    say("")
    say(f"Будет создано:")
    say(f"  {vault_path}/")
    say(f"    raw/chats, raw/articles, raw/docs, raw/assets")
    say(f"    wiki/entities, wiki/concepts, wiki/sources")
    say(f"  {unassigned}/")

    if ask_yesno("Создать структуру папок?"):
        if not create_vault_structure(vault_path):
            return 1
        unassigned.mkdir(parents=True, exist_ok=True)

    # 4. project-map.json
    if not setup_project_map(vault_path, project_name, unassigned):
        return 1

    # 5. Хуки
    if has_claude:
        if ask_yesno("Подключить хуки в ~/.claude/settings.json?"):
            if not install_hooks():
                warn("Хуки не прописаны — сделай это вручную по docs/GUIDE.md")
    else:
        warn("Claude CLI не установлен — хуки пропущены")

    # 6. Финал
    heading("Готово")
    ok("Установка завершена")
    say("")
    say("Дальнейшие шаги:")
    say(f"  1. Запусти дашборд:")
    if IS_WINDOWS:
        say(f"     python scripts\\dashboard.py")
        say(f"     или двойной клик по start-dashboard.bat")
        say(f"")
        say(f"     Для ярлыка на рабочий стол:")
        say(f"     powershell -ExecutionPolicy Bypass -File create-desktop-shortcut.ps1")
    else:
        say(f"     python3 scripts/dashboard.py")
        say(f"     или:  chmod +x start-dashboard.command && ./start-dashboard.command")
    say(f"")
    say(f"  2. Открой http://localhost:5757")
    say(f"  3. Запусти Claude в папке проекта:")
    say(f"     cd \"{vault_path}\"")
    say(f"     claude")
    say(f"")
    say(f"  4. Прочитай полный гайд: http://localhost:5757/help")
    say("")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("Установка прервана")
        sys.exit(130)
