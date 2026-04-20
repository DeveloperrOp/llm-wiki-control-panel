"""Реестр активных Claude Code сессий — для backfill-механизма.

Мотивация: VSCode Extension Claude Code не вызывает SessionEnd-хук при /clear
(см. https://github.com/anthropics/claude-code/issues/50808). Чтобы не терять
транскрипты, session-start.py при каждом старте сессии сверяется с этим
реестром и дампит осиротевшие транскрипты за счёт .jsonl-файла предыдущей
сессии (Claude Code его не удаляет).

Формат state/active-sessions.json:
    {
      "<cwd_norm>": [
        {
          "sid": "...",
          "transcript_path": "...",
          "started_at": 1234567890,
          "cwd_raw": "..."
        },
        ...
      ]
    }

cwd_norm = lower-case + forward slashes. На один cwd возможны несколько
параллельных сессий (два терминала в одной папке), поэтому — список.
"""

from __future__ import annotations

import time
from pathlib import Path

from lib.state import load_state, locked, save_state

SHARED_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_FILE = SHARED_ROOT / "state" / "active-sessions.json"

TTL_SEC = 7 * 24 * 3600         # 7 дней — старше игнорируем и чистим
MTIME_ALIVE_SEC = 30            # mtime .jsonl свежее этого порога → сессия живая
MISSING_GRACE_SEC = 60          # transcript только что пропал — держим в реестре ещё N сек


def _normalize(cwd: str) -> str:
    return str(cwd).replace("\\", "/").lower()


def register(cwd_raw: str, sid: str, transcript_path: str) -> None:
    """Регистрирует активную сессию. Upsert по sid внутри cwd."""
    if not cwd_raw or not sid:
        return
    cwd_norm = _normalize(cwd_raw)
    with locked(STATE_FILE):
        state = load_state(STATE_FILE, default={})
        sessions = [s for s in state.get(cwd_norm, []) if s.get("sid") != sid]
        sessions.append({
            "sid": sid,
            "transcript_path": transcript_path or "",
            "started_at": int(time.time()),
            "cwd_raw": cwd_raw,
        })
        state[cwd_norm] = sessions
        save_state(STATE_FILE, state)


def unregister(cwd_raw: str, sid: str) -> None:
    """Убирает запись о сессии — вызывается из session-end.py после дампа."""
    if not cwd_raw or not sid:
        return
    cwd_norm = _normalize(cwd_raw)
    with locked(STATE_FILE):
        state = load_state(STATE_FILE, default={})
        sessions = [s for s in state.get(cwd_norm, []) if s.get("sid") != sid]
        if sessions:
            state[cwd_norm] = sessions
        else:
            state.pop(cwd_norm, None)
        save_state(STATE_FILE, state)


def pop_dead_others(cwd_raw: str, current_sid: str) -> list[dict]:
    """Находит «мёртвые» сессии в этом cwd, удаляет их из реестра и возвращает
    для дампа. Мёртвая = не равна current_sid + .jsonl mtime старше MTIME_ALIVE_SEC.

    Stale (started_at старше TTL_SEC) — удаляются молча, не возвращаются.
    Живые (mtime свежий) и текущая — остаются в реестре.
    """
    if not cwd_raw:
        return []
    cwd_norm = _normalize(cwd_raw)
    with locked(STATE_FILE):
        state = load_state(STATE_FILE, default={})
        sessions = state.get(cwd_norm, [])
        if not sessions:
            return []

        now = time.time()
        alive: list[dict] = []
        dead: list[dict] = []

        for s in sessions:
            if s.get("sid") == current_sid:
                alive.append(s)
                continue

            started_at = float(s.get("started_at") or 0)
            if now - started_at > TTL_SEC:
                continue  # stale — дропаем молча, не на дамп

            transcript_path = s.get("transcript_path") or ""
            try:
                p = Path(transcript_path)
                if not p.exists():
                    # Файл транскрипта пропал. Если сессия зарегистрирована только что —
                    # оставляем на grace-период (transcript мог ещё не быть создан).
                    if now - started_at < MISSING_GRACE_SEC:
                        alive.append(s)
                    # старше grace — молча дропаем (нечего дампить)
                    continue
                mtime = p.stat().st_mtime
                if now - mtime < MTIME_ALIVE_SEC:
                    alive.append(s)  # живая сессия — оставляем
                    continue
            except PermissionError:
                # Файл временно заблокирован (антивирус на Windows, другой процесс).
                # НЕ дропаем из реестра — даём шанс подхватить позже.
                alive.append(s)
                continue
            except Exception:
                continue

            dead.append(s)

        if alive:
            state[cwd_norm] = alive
        else:
            state.pop(cwd_norm, None)
        save_state(STATE_FILE, state)

    return dead
