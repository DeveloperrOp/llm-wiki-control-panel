#!/usr/bin/env python
"""SessionStart-хук: инжектит index.md + последние записи log.md в контекст сессии.

Формат stdin (JSON):
    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "...",
      "hook_event_name": "SessionStart",
      "source": "startup|resume|clear|compact",
      "model": "..."
    }

Формат stdout (JSON) — читается Claude Code:
    {
      "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "..."
      }
    }

Ошибки не ломают запуск: при любом сбое — выход 0, пустой stdout.
Диагностика пишется в .shared/state/hook-log.txt.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED_ROOT = HERE.parent
sys.path.insert(0, str(SHARED_ROOT / "scripts"))

from lib.mapping import resolve_project  # noqa: E402
from lib.context_injection import build_context, clip_context  # noqa: E402
from lib.session_dump import dump_transcript  # noqa: E402
from lib.active_sessions import register, pop_dead_others  # noqa: E402

HOOK_LOG = SHARED_ROOT / "state" / "hook-log.txt"


def _log(message: str) -> None:
    try:
        HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with HOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] session-start: {message}\n")
    except Exception:
        pass


# build_context() и clip_context() — в lib/context_injection.py


def main() -> None:
    import os
    if os.environ.get("LLM_WIKI_SUBSESSION") == "1":
        _log("skip: LLM_WIKI_SUBSESSION=1 (сабсессия)")
        return
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _log(f"не удалось прочитать stdin: {e}")
        return

    cwd = payload.get("cwd") or ""
    source = payload.get("source") or "unknown"
    session_id = payload.get("session_id") or "unknown"
    transcript_path = payload.get("transcript_path") or ""

    # Backfill: дампим осиротевшие сессии в этом cwd (обход VSCode /clear бага,
    # см. https://github.com/anthropics/claude-code/issues/50808).
    try:
        for dead in pop_dead_others(cwd, session_id):
            dump_transcript(
                session_id=dead.get("sid") or "unknown",
                transcript_path_str=dead.get("transcript_path") or "",
                cwd=dead.get("cwd_raw") or cwd,
                hook_event="SessionStart",
                reason="backfill",
                log_source="backfill",
            )
    except Exception as e:
        _log(f"backfill failed: {e}\n{traceback.format_exc()}")

    # Регистрируем текущую сессию — чтобы будущий SessionStart мог её бэкфиллить,
    # если SessionEnd не придёт (например, /clear в VSCode).
    try:
        register(cwd, session_id, transcript_path)
    except Exception as e:
        _log(f"register failed: {e}\n{traceback.format_exc()}")

    try:
        resolution = resolve_project(cwd)
    except Exception as e:
        _log(f"resolve_project failed: {e}\n{traceback.format_exc()}")
        return

    if resolution.is_unassigned:
        _log(f"unassigned cwd={cwd} (sid={session_id[:8]}, source={source}) — контекст не инжектится")
        return

    if not resolution.vault_root.exists():
        _log(f"vault не существует: {resolution.vault_root}")
        return

    try:
        from lib.context_injection import DEFAULT_CONTEXT_LIMIT  # noqa: E402
        raw = build_context(resolution.vault_root, resolution.name)
        limit = resolution.context_limit or DEFAULT_CONTEXT_LIMIT
        context, _ = clip_context(raw, limit=limit)
    except Exception as e:
        _log(f"build_context failed: {e}\n{traceback.format_exc()}")
        return

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(output, sys.stdout, ensure_ascii=False)
    sys.stdout.flush()
    _log(
        f"OK project={resolution.name} chars={len(context)} "
        f"source={source} sid={session_id[:8]}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log(f"UNHANDLED: {e}\n{traceback.format_exc()}")
        sys.exit(0)
