#!/usr/bin/env python
"""PreCompact-хук: дампит транскрипт перед компактификацией.

Такая же логика как session-end.py, но файл пишется с суффиксом `-precompact`.

Формат stdin (JSON):
    {
      "session_id": "...",
      "transcript_path": "...",
      "cwd": "...",
      "hook_event_name": "PreCompact",
      "trigger": "manual|auto"
    }
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

HOOK_EVENT = "PreCompact"
LOG_SOURCE = "precompact"
SUFFIX = "-precompact"


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
    reason = payload.get("trigger") or payload.get("reason") or ""

    dump_transcript(
        session_id=session_id,
        transcript_path_str=transcript_path,
        cwd=cwd,
        hook_event=HOOK_EVENT,
        reason=reason,
        suffix=SUFFIX,
        log_source=LOG_SOURCE,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log(LOG_SOURCE, f"UNHANDLED: {e}\n{traceback.format_exc()}")
        except Exception:
            pass
        sys.exit(0)
