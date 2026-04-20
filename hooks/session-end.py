#!/usr/bin/env python
"""SessionEnd-хук: дампит транскрипт в <vault>/raw/chats/ и чистит реестр
активных сессий.

Формат stdin (JSON):
    {
      "session_id": "...",
      "transcript_path": "/path/to/transcript.jsonl",
      "cwd": "...",
      "hook_event_name": "SessionEnd",
      "reason": "clear|resume|logout|..."
    }

Дедуп по session_id и вся логика дампа — в lib/session_dump.py.
Реестр активных сессий — в lib/active_sessions.py.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED_ROOT = HERE.parent
sys.path.insert(0, str(SHARED_ROOT / "scripts"))

from lib.session_dump import dump_transcript, log  # noqa: E402
from lib.active_sessions import unregister  # noqa: E402

HOOK_EVENT = "SessionEnd"
LOG_SOURCE = "sessionend"


def main() -> None:
    if os.environ.get("LLM_WIKI_SUBSESSION") == "1":
        log(LOG_SOURCE, "skip: LLM_WIKI_SUBSESSION=1 (сабсессия)")
        return
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log(LOG_SOURCE, f"stdin read failed: {e}")
        return

    session_id = payload.get("session_id") or "unknown"
    transcript_path = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or ""
    reason = payload.get("reason") or payload.get("trigger") or ""

    dumped = dump_transcript(
        session_id=session_id,
        transcript_path_str=transcript_path,
        cwd=cwd,
        hook_event=HOOK_EVENT,
        reason=reason,
        log_source=LOG_SOURCE,
    )

    # Убираем запись из реестра в двух случаях:
    #   1) dump успешен (нечего backfill'ить),
    #   2) это logout — сессия точно мертва, даже если dump упал
    #      (нет смысла держать запись, session-start её всё равно не найдёт
    #       как dead пока jsonl обновляется, а потом просто истечёт TTL).
    # При /clear + dump_failed — оставляем в реестре: backfill из session-start
    # следующей сессии подхватит осиротевший transcript.
    is_logout = (reason or "").lower() in ("logout", "exit")
    if dumped or is_logout:
        unregister(cwd, session_id)
    else:
        log(LOG_SOURCE, f"dump не удался, оставляем sid={session_id[:8]} в реестре для backfill")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log(LOG_SOURCE, f"UNHANDLED: {e}\n{traceback.format_exc()}")
        except Exception:
            pass
        sys.exit(0)
