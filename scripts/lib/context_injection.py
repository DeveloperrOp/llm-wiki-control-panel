"""Общая логика формирования контекста для SessionStart-хука и Dashboard.

Должна использоваться и в hooks/session-start.py (для реального инжекта), и в
dashboard.py (для превью в UI) — чтобы точно совпадали размеры и содержимое.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_CONTEXT_LIMIT = 10_000
MAX_CONTEXT_CHARS = DEFAULT_CONTEXT_LIMIT  # Совместимость со старым кодом
LAST_LOG_LINES = 30

# Разрешённые per-project значения лимита (для dropdown в UI)
ALLOWED_CONTEXT_LIMITS = (5_000, 10_000, 15_000, 20_000, 25_000)

# warn-порог — 70% от лимита (для цветового индикатора в UI)
def _warn_threshold(limit: int) -> int:
    return int(limit * 0.7)

WARN_THRESHOLD = _warn_threshold(DEFAULT_CONTEXT_LIMIT)  # legacy


def _read_tail(path: Path, max_lines: int) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return ""


def _read_file(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_context(vault_root: Path, project_name: str) -> str:
    """Формирует текст, который хук session-start инжектирует в additionalContext.

    Результат может быть длиннее MAX_CONTEXT_CHARS — обрезание делает caller.
    """
    index_content = _read_file(vault_root / "index.md")
    log_tail = _read_tail(vault_root / "log.md", LAST_LOG_LINES)

    parts = [
        f"# 📚 Контекст LLM Wiki «{project_name}»",
        "",
        "Это автоматически инжектированный контекст из твоей персональной вики.",
        f"Vault: `{vault_root}`",
        "Читай `CLAUDE.md` в vault для правил работы с вики.",
        "",
        "---",
        "",
    ]
    if index_content:
        parts.extend(["## index.md — каталог вики", "", index_content.strip(), "", "---", ""])
    if log_tail.strip():
        parts.extend([
            f"## log.md — последние {LAST_LOG_LINES} строк",
            "",
            "```markdown",
            log_tail.rstrip(),
            "```",
        ])

    return "\n".join(parts)


def clip_context(text: str, limit: int = DEFAULT_CONTEXT_LIMIT) -> tuple[str, bool]:
    """Обрезает контекст до limit символов. Возвращает (text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n\n… [контекст обрезан по лимиту {limit // 1000}k]", True


def compute_injection(vault_root: Path, project_name: str, limit: int | None = None) -> dict:
    """Полная сводка для UI.

    limit — per-project лимит символов (из project-map.json). Если None —
    используется DEFAULT_CONTEXT_LIMIT.
    """
    effective_limit = limit if limit is not None else DEFAULT_CONTEXT_LIMIT
    warn = _warn_threshold(effective_limit)

    raw = build_context(vault_root, project_name)
    effective, truncated = clip_context(raw, effective_limit)

    raw_size = len(raw)
    # status='over' только при реальном превышении (обрезании), а не при точном равенстве
    if truncated:
        status = "over"
    elif raw_size >= warn:
        status = "warn"
    else:
        status = "ok"

    return {
        "raw_size": raw_size,
        "effective_size": len(effective),
        "limit": effective_limit,
        "warn_threshold": warn,
        "truncated": truncated,
        "status": status,
        "preview": effective,
        "raw": raw,
    }
