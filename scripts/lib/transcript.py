"""Парсер Claude Code JSONL-транскриптов и форматтер в markdown.

Формат строки JSONL (Claude Code):
    {
      "type": "user" | "assistant" | "summary" | "system",
      "message": {"role": "...", "content": <str | list>, "usage": {...}},
      "timestamp": "ISO8601",
      "sessionId": "...",
      "cwd": "...",
      "uuid": "...",
      "parentUuid": "..." | null,
      ...
    }

content может быть строкой либо массивом блоков:
    - {"type": "text", "text": "..."}
    - {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
    - {"type": "tool_result", "tool_use_id": "...", "content": "..." | list, "is_error": bool}
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


MAX_TOOL_OUTPUT_CHARS = 2000
# Жёсткий лимит размера JSONL для защиты от OOM в хуке (~10 сек тайм-аут Claude).
# 200 МБ хватает для сессий в сотни тысяч ивентов; большее — маркер патологии.
MAX_JSONL_BYTES = 200 * 1024 * 1024


def iter_events(path: Path):
    """Генератор: yield по одному dict'у. Пропускает битые строки."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def read_jsonl(path: Path) -> list[dict]:
    """Читает JSONL построчно, пропускает битые строки. Для обратной совместимости."""
    return list(iter_events(path))


def collect_tool_results(events) -> dict[str, dict]:
    """Собирает tool_result-блоки в map: tool_use_id -> {content, is_error}.

    Принимает и list[dict], и итератор (будет исчерпан за один проход).
    """
    results: dict[str, dict] = {}
    for ev in events:
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tuid = block.get("tool_use_id")
                if not tuid:
                    continue
                results[tuid] = {
                    "content": block.get("content"),
                    "is_error": bool(block.get("is_error", False)),
                }
    return results


def _render_tool_content(content) -> str:
    """Нормализует tool_result.content (строка или массив блоков) в текст."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… [обрезано, всего {len(text)} символов]"


def _format_timestamp(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def _duration(first: str | None, last: str | None) -> str:
    if not (first and last):
        return "—"
    try:
        a = datetime.fromisoformat(first.replace("Z", "+00:00"))
        b = datetime.fromisoformat(last.replace("Z", "+00:00"))
        delta = b - a
        total_seconds = int(delta.total_seconds())
        mins, secs = divmod(total_seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours}ч {mins}м {secs}с"
        if mins:
            return f"{mins}м {secs}с"
        return f"{secs}с"
    except ValueError:
        return "—"


def _render_user_message(content, tool_results: dict[str, dict]) -> str | None:
    """Возвращает markdown для user-сообщения или None если это только tool_result."""
    if isinstance(content, str):
        text = content.strip()
        return text if text else None

    if not isinstance(content, list):
        return None

    text_parts: list[str] = []
    only_tool_results = True
    for block in content:
        if not isinstance(block, dict):
            only_tool_results = False
            text_parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            only_tool_results = False
            text_parts.append(block.get("text", ""))
        elif btype == "tool_result":
            continue
        else:
            only_tool_results = False
            text_parts.append(json.dumps(block, ensure_ascii=False))

    if only_tool_results:
        return None

    joined = "\n".join(p for p in text_parts if p).strip()
    return joined or None


def _render_assistant_message(content, tool_results: dict[str, dict]) -> str:
    """Рендерит assistant-сообщение: текст + tool_use как <details>."""
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue

        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif btype == "tool_use":
            tool_name = block.get("name", "unknown")
            tool_input = block.get("input", {})
            tool_id = block.get("id", "")
            result = tool_results.get(tool_id)

            input_str = json.dumps(tool_input, ensure_ascii=False, indent=2)
            summary_hint = _tool_summary_hint(tool_name, tool_input)
            summary = f"🔧 {tool_name}"
            if summary_hint:
                summary += f" — {summary_hint}"

            details = [f"<details>", f"<summary>{summary}</summary>", ""]
            details.append("**Input:**")
            details.append("```json")
            details.append(_truncate(input_str))
            details.append("```")

            if result is not None:
                result_text = _render_tool_content(result.get("content"))
                marker = "❌ Ошибка" if result.get("is_error") else "✅ Результат"
                details.append("")
                details.append(f"**{marker}:**")
                details.append("```")
                details.append(_truncate(result_text))
                details.append("```")

            details.append("</details>")
            parts.append("\n".join(details))
        else:
            parts.append(json.dumps(block, ensure_ascii=False))

    return "\n\n".join(p for p in parts if p).strip()


def _tool_summary_hint(name: str, tool_input: dict) -> str:
    """Краткий хинт для summary tool-блока (например имя файла для Read)."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "url"):
        value = tool_input.get(key)
        if value:
            value_str = str(value)
            if len(value_str) > 80:
                value_str = value_str[:77] + "..."
            return value_str
    return ""


def format_session(
    transcript_path: Path,
    session_id: str | None = None,
    cwd: str | None = None,
    hook_event: str | None = None,
) -> str:
    """Главная точка входа: читает JSONL и возвращает markdown.

    Два прохода через генератор iter_events — файл не грузится в память целиком.

    ⚠ Ограничение при вызове на ЖИВУЮ сессию (Claude Code пишет в файл):
    между первым (сбор tool_results + stats) и вторым (рендер body) проходами
    Claude может дописать новые события. Тогда tool_use из второго прохода
    может не иметь match в tool_results из первого → в markdown будет
    `<details>🔧 …</details>` без секции `Результат:`.

    На практике это триггерится только при PreCompact (сессия ещё активна)
    или ручном импорте .jsonl открытой сессии из dashboard. Для session-end
    (сессия закрыта — файл не пишется) — эффект невозможен.

    Обходной путь: если нужна 100% консистентность для живой сессии —
    сначала `list(iter_events(path))` (один snapshot в памяти), потом
    передать список. Но это теряет смысл стриминга.
    """
    if not transcript_path.exists():
        return f"# Пустой транскрипт\n\nПуть: `{transcript_path}`\n"

    # Защита от патологически больших JSONL: режем работу до OOM.
    try:
        file_size = transcript_path.stat().st_size
    except OSError:
        file_size = 0
    if file_size > MAX_JSONL_BYTES:
        return (
            f"# Транскрипт слишком большой\n\n"
            f"Путь: `{transcript_path}`\n"
            f"Размер: {file_size / 1024 / 1024:.1f} МБ "
            f"(лимит: {MAX_JSONL_BYTES // 1024 // 1024} МБ).\n\n"
            "Дамп пропущен чтобы не исчерпать память хука. "
            "Если транскрипт нужен — сделай импорт через dashboard вручную.\n"
        )

    # Проход 1: tool_results, статистика, ts, sid, cwd.
    tool_results: dict[str, dict] = {}
    first_ts: str | None = None
    last_ts: str | None = None
    first_sid: str | None = None
    first_cwd: str | None = None

    user_turns = 0
    assistant_turns = 0
    tool_calls = 0
    any_events = False

    for ev in iter_events(transcript_path):
        any_events = True
        if first_sid is None:
            first_sid = ev.get("sessionId")
        if first_cwd is None:
            first_cwd = ev.get("cwd")

        ts = ev.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        msg = ev.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    tuid = block.get("tool_use_id")
                    if tuid:
                        tool_results[tuid] = {
                            "content": block.get("content"),
                            "is_error": bool(block.get("is_error", False)),
                        }

        t = ev.get("type")
        if t == "user":
            if _render_user_message(content, {}) is not None:
                user_turns += 1
        elif t == "assistant":
            assistant_turns += 1
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls += 1

    if not any_events:
        return f"# Пустой транскрипт\n\nПуть: `{transcript_path}`\n"

    sid = session_id or first_sid or "unknown"
    real_cwd = cwd or first_cwd or "—"

    header_lines = [
        f"# Сессия {_format_timestamp(first_ts)}",
        "",
        f"- **Session ID:** `{sid}`",
        f"- **CWD:** `{real_cwd}`",
        f"- **Начало:** {_format_timestamp(first_ts)}",
        f"- **Окончание:** {_format_timestamp(last_ts)}",
        f"- **Длительность:** {_duration(first_ts, last_ts)}",
        f"- **User-turns:** {user_turns}",
        f"- **Assistant-turns:** {assistant_turns}",
        f"- **Tool calls:** {tool_calls}",
    ]
    if hook_event:
        header_lines.append(f"- **Hook:** {hook_event}")
    header_lines.extend(["", "---", ""])

    # Проход 2: рендер body.
    body: list[str] = []
    for ev in iter_events(transcript_path):
        t = ev.get("type")
        msg = ev.get("message") or {}
        content = msg.get("content")

        if t == "user":
            rendered = _render_user_message(content, tool_results)
            if rendered is None:
                continue
            body.append("### 👤 Пользователь")
            body.append("")
            body.append(rendered)
            body.append("")
            body.append("---")
            body.append("")
        elif t == "assistant":
            rendered = _render_assistant_message(content, tool_results)
            if not rendered:
                continue
            body.append("### 🤖 Claude")
            body.append("")
            body.append(rendered)
            body.append("")
            body.append("---")
            body.append("")
        elif t == "summary":
            summary_text = ev.get("summary") or ""
            if summary_text:
                body.append("### 📋 Summary")
                body.append("")
                body.append(summary_text)
                body.append("")
                body.append("---")
                body.append("")

    return "\n".join(header_lines + body).rstrip() + "\n"


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python transcript.py <path-to-transcript.jsonl>")
        sys.exit(1)
    result = format_session(Path(sys.argv[1]))
    sys.stdout.reconfigure(encoding="utf-8")
    print(result)
