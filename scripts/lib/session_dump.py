"""Общая логика дампа Claude Code транскрипта в <vault>/raw/chats/.

Используется обоими хуками:
- session-end.py  — нормальный триггер (reason=clear|logout|resume|…)
- session-start.py — backfill для осиротевших сессий (обход VSCode бага /clear)

Ранее эта логика жила прямо в session-end.py. Вынесена в lib/, чтобы её мог
импортировать и session-start.py (имя session-end.py с дефисом непригодно для
import как модуль).
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from lib.mapping import resolve_project, ensure_chats_dir
from lib.state import load_state, locked, save_state
from lib.transcript import format_session
from lib.jobs import make_job, run_job_detached

SHARED_ROOT = Path(__file__).resolve().parent.parent.parent
DEDUP_WINDOW_SEC = 300
DEDUP_STATE = SHARED_ROOT / "state" / "session-dumps.json"
HOOK_LOG = SHARED_ROOT / "state" / "hook-log.txt"


def log(source: str, message: str) -> None:
    """Пишет в общий hook-log. `source` — короткий ярлык (sessionend / session-start / backfill)."""
    try:
        HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with HOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {source}: {message}\n")
    except Exception:
        pass


def _reserve_dedup_slot(session_id: str, suffix: str) -> bool:
    """Atomic check-and-set: резервирует дедуп-слот под межпроцессным lock'ом.

    Возвращает True если слот свободен (мы первые) — вызывающий может писать файл.
    Возвращает False если слот занят (другой хук уже дампит / только что сдампил).

    Предварительная запись tentative в state устраняет race между двумя
    параллельными хуками (session-end + pre-compact + backfill).
    """
    key = f"{session_id}{suffix}"
    with locked(DEDUP_STATE):
        state = load_state(DEDUP_STATE, default={})
        entry = state.get(key)
        now = time.time()
        if entry and (now - float(entry.get("ts", 0))) < DEDUP_WINDOW_SEC:
            return False
        # Сначала обрезаем старое (если >500), потом добавляем новый слот.
        # Иначе новый ключ с ts=now может попасть в топ-500 но всё равно
        # потеряться из-за коллизии сортировки с битыми записями (ts=0).
        if len(state) >= 500:
            sorted_items = sorted(state.items(), key=lambda kv: kv[1].get("ts", 0))
            state = dict(sorted_items[-499:])
        state[key] = {"ts": now, "file": ""}  # placeholder, будет обновлён
        save_state(DEDUP_STATE, state)
    return True


def _finalize_dump_slot(session_id: str, suffix: str, file_path: Path) -> None:
    """Обновляет tentative-запись реальным путём файла (после успешной записи)."""
    key = f"{session_id}{suffix}"
    with locked(DEDUP_STATE):
        state = load_state(DEDUP_STATE, default={})
        if key in state:
            state[key]["file"] = str(file_path)
            save_state(DEDUP_STATE, state)


def _release_dedup_slot(session_id: str, suffix: str) -> None:
    """Откатывает зарезервированный но не завершённый слот.

    Вызывается из error-веток dump_transcript (resolve_project/format_session/
    write_text упали) чтобы placeholder не блокировал повторные попытки дампа
    на 5 минут впустую. Освобождает только если file ещё пустой — иначе значит
    финализация уже прошла и чужую запись трогать нельзя.
    """
    key = f"{session_id}{suffix}"
    with locked(DEDUP_STATE):
        state = load_state(DEDUP_STATE, default={})
        entry = state.get(key)
        if entry and entry.get("file") == "":
            state.pop(key, None)
            save_state(DEDUP_STATE, state)


def _spawn_auto_ingest(project_name: str, source_path: Path, log_source: str) -> None:
    try:
        ingest_script = SHARED_ROOT / "scripts" / "ingest.py"
        if not ingest_script.exists():
            log(log_source, "auto-ingest: ingest.py не найден")
            return
        job = make_job(
            job_type="ingest",
            project=project_name,
            trigger="auto",
            source=str(source_path).replace("\\", "/"),
            options={"timeout": 900, "origin": f"{log_source}-auto"},
        )
        cmd = [
            sys.executable, str(ingest_script),
            project_name,
            "--source", str(source_path),
            "--timeout", "900",
        ]
        run_job_detached(job, cmd)
        log(log_source, f"auto-ingest spawned: project={project_name} source={source_path.name} job={job.id[:8]}")
    except Exception as e:
        log(log_source, f"auto-ingest failed to spawn: {e}\n{traceback.format_exc()}")


def dump_transcript(
    session_id: str,
    transcript_path_str: str,
    cwd: str,
    hook_event: str,
    reason: str = "",
    suffix: str = "",
    log_source: str = "sessionend",
    forced_project: "str | None" = None,
) -> bool:
    """Дампит транскрипт в raw/chats/ с дедупом и опциональным auto-ingest.

    Если передан `forced_project` — обходим resolve_project по cwd и кладём
    файл напрямую в vault этого проекта (используется в force-dump UI, где
    пользователь явно выбрал целевой проект и его cwd_patterns могут не
    покрывать текущий cwd).

    Возвращает True если файл реально записан, False — пропуск/ошибка.
    """
    if not transcript_path_str:
        log(log_source, f"нет transcript_path (sid={session_id[:8]})")
        return False

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        log(log_source, f"transcript не найден: {transcript_path}")
        return False

    # Atomic reserve: первый прошедший под lock'ом пишет placeholder,
    # остальные получают False и пропускают дамп.
    if not _reserve_dedup_slot(session_id, suffix):
        log(log_source, f"дедуп: пропускаем sid={session_id[:8]}{suffix}")
        return False

    try:
        if forced_project:
            from lib.mapping import list_projects
            resolution = next(
                (p for p in list_projects() if p.name == forced_project),
                None,
            )
            if resolution is None:
                log(log_source, f"forced_project не найден в маппинге: {forced_project}")
                _release_dedup_slot(session_id, suffix)
                return False
        else:
            resolution = resolve_project(cwd, session_id=session_id)
    except Exception as e:
        log(log_source, f"resolve_project failed: {e}\n{traceback.format_exc()}")
        _release_dedup_slot(session_id, suffix)
        return False

    chats_dir = ensure_chats_dir(resolution)
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    sid_short = session_id[:8] if session_id else "unknown"
    filename = f"{ts}-{sid_short}{suffix}.md"
    out_path = chats_dir / filename

    try:
        markdown = format_session(
            transcript_path,
            session_id=session_id,
            cwd=cwd,
            hook_event=f"{hook_event}{f' ({reason})' if reason else ''}",
        )
    except Exception as e:
        log(log_source, f"format_session failed: {e}\n{traceback.format_exc()}")
        _release_dedup_slot(session_id, suffix)
        return False

    try:
        out_path.write_text(markdown, encoding="utf-8")
    except Exception as e:
        log(log_source, f"write failed {out_path}: {e}")
        _release_dedup_slot(session_id, suffix)
        return False

    _finalize_dump_slot(session_id, suffix, out_path)

    tag = "unassigned" if resolution.is_unassigned else "project"
    log(
        log_source,
        f"OK {tag}={resolution.name} sid={sid_short}{suffix} "
        f"file={out_path.name} size={len(markdown)}",
    )

    if resolution.auto_ingest and not resolution.is_unassigned:
        _spawn_auto_ingest(resolution.name, out_path, log_source)

    return True
