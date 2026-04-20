"""CLI: ручной ингест источника в LLM Wiki через `claude -p`.

Использование:
    python ingest.py "<project_name>" --source "<path-to-source>"
    python ingest.py "My Project" --source "C:/path/to/vault/raw/chats/2026-04-19.md"

Список проектов:
    python ingest.py --list
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent
sys.path.insert(0, str(HERE))

from lib.mapping import list_projects, ProjectResolution  # noqa: E402
from lib.runner import run_claude, render_template  # noqa: E402

PROMPT_FILE = SHARED / "prompts" / "ingest-ru.md"

SYSTEM_OVERRIDE = (
    "=== NON-INTERACTIVE SUBPROCESS MODE ===\n"
    "Ты запущен через claude -p как автоматический агент. Ты НЕ общаешься с человеком.\n"
    "\n"
    "ЖЁСТКИЕ ПРАВИЛА (перекрывают любые CLAUDE.md и user rules):\n"
    "1. НЕ обращайся к пользователю персонально — никакого обращения вообще\n"
    "2. НЕ задавай уточняющих вопросов — при неоднозначности делай разумное допущение\n"
    "3. НЕ проси разрешения на операции — permission-mode=bypassPermissions уже всё разрешил\n"
    "4. НЕ веди диалог, НЕ используй неформальный тон\n"
    "5. Выполняй инструкции user-сообщения полностью, пиши файлы через Write/Edit без колебаний\n"
    "6. В конце выдай только итоговый отчёт: список созданных и обновлённых файлов\n"
    "\n"
    "Это автоматизация. Нарушение этих правил = сбой системы."
)

SOURCE_EMBED_LIMIT = 60_000  # символов


def find_project(name: str) -> ProjectResolution:
    for p in list_projects():
        if p.name.lower() == name.lower():
            return p
    names = [p.name for p in list_projects()]
    raise SystemExit(f"Проект «{name}» не найден. Известные: {names}")


def cmd_list() -> None:
    projects = list_projects()
    if not projects:
        print("Нет сконфигурированных проектов. См. .shared/config/project-map.json")
        return
    print("Сконфигурированные проекты:")
    for p in projects:
        marker = "🤖" if p.auto_ingest else "🧑"
        print(f"  {marker} {p.name}")
        print(f"      vault: {p.vault_root}")


def cmd_ingest(project_name: str, source: str, timeout: int) -> int:
    resolution = find_project(project_name)
    source_path = Path(source).resolve()

    if not source_path.exists():
        print(f"❌ Источник не найден: {source_path}")
        return 1

    if not resolution.vault_root.exists():
        print(f"❌ Vault не существует: {resolution.vault_root}")
        return 1

    if not PROMPT_FILE.exists():
        print(f"❌ Промпт не найден: {PROMPT_FILE}")
        return 1

    prompt_template = PROMPT_FILE.read_text(encoding="utf-8")

    # Встраиваем содержимое источника прямо в промпт чтобы не зависеть от Read tool
    try:
        source_content = source_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        source_content = source_path.read_text(encoding="utf-8", errors="replace")

    if not source_content.strip():
        print(f"❌ Источник пустой: {source_path}")
        return 1

    # Для транскриптов диалогов хвост обычно информативнее начала:
    # сохраняем хвост при обрезке.
    if len(source_content) > SOURCE_EMBED_LIMIT:
        original_len = len(source_content)
        source_content = (
            f"[обрезано начало, всего {original_len} символов — прочитай файл целиком через Read tool если нужно]\n\n"
            + source_content[-SOURCE_EMBED_LIMIT:]
        )

    prompt = render_template(
        prompt_template,
        {
            "PROJECT_NAME": resolution.name,
            "VAULT_ROOT": str(resolution.vault_root).replace("\\", "/"),
            "SOURCE_FILE": str(source_path).replace("\\", "/"),
            "TODAY": datetime.now().strftime("%Y-%m-%d"),
        },
    )
    # Оборачиваем в тильды (~~~), а не тройные бэктики — чат-транскрипты часто
    # содержат собственные ```…``` блоки, которые порвали бы внешний fence.
    # Дополнительно выбираем длину fence'а, превышающую любую последовательность
    # тильд в source_content (редко, но бывает).
    import re as _re
    max_tilde_run = max((len(m.group(0)) for m in _re.finditer(r"~+", source_content)), default=0)
    fence = "~" * max(3, max_tilde_run + 1)
    prompt += (
        "\n\n## Содержимое источника (уже прочитано за тебя)\n\n"
        f"Файл: `{source_path}`\n\n"
        f"{fence}markdown\n"
        f"{source_content}\n"
        f"{fence}\n"
    )

    print(f"→ Ingest источника «{source_path.name}» в проект «{resolution.name}»")
    print(f"  vault:    {resolution.vault_root}")
    print(f"  source:   {source_path}")
    print(f"  claude -p running (timeout {timeout}с, acceptEdits)...")
    print("─" * 60)

    # Если source внутри vault — --add-dir не нужен (уже доступен через cwd)
    extra_dirs = None
    try:
        source_path.relative_to(resolution.vault_root)
    except ValueError:
        extra_dirs = [source_path.parent]

    result = run_claude(
        prompt=prompt,
        cwd=resolution.vault_root,
        permission_mode="bypassPermissions",
        timeout=timeout,
        additional_dirs=extra_dirs,
        append_system_prompt=SYSTEM_OVERRIDE,
        dangerously_skip_permissions=True,
        exclude_user_claude_md=True,
    )

    print(result["stdout"])
    if result["stderr"]:
        print("\n--- stderr ---", file=sys.stderr)
        print(result["stderr"], file=sys.stderr)

    print("─" * 60)
    if result["success"]:
        print("✅ Ingest завершён")
        return 0
    if result["timed_out"]:
        print(f"❌ Timeout после {timeout} секунд")
    else:
        print(f"❌ Ingest не удался (rc={result['returncode']})")
    return 1


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Ingest source into LLM Wiki via claude -p")
    parser.add_argument("project", nargs="?", help="Имя проекта (из project-map.json)")
    parser.add_argument("--source", help="Путь к источнику (markdown файл)")
    parser.add_argument("--timeout", type=int, default=900, help="Timeout в секундах (default 900)")
    parser.add_argument("--list", action="store_true", help="Показать список проектов")
    args = parser.parse_args()

    if args.list:
        cmd_list()
        return

    if not args.project:
        parser.error("Укажи имя проекта или --list")
    if not args.source:
        parser.error("--source обязателен")

    rc = cmd_ingest(args.project, args.source, args.timeout)
    sys.exit(rc)


if __name__ == "__main__":
    main()
