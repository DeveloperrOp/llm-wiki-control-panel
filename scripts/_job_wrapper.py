"""Wrapper для detached jobs: запускает команду и обновляет jobs.json.

Usage:
    python _job_wrapper.py <job_id> <cmd> [<arg>...]

Вызывается из lib/jobs.py::run_job_detached(). Родительский процесс (хук)
может быть убит — этот wrapper продолжит работу, дождётся cmd, запишет
результат в state/jobs.json.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lib.jobs import kill_proc_tree, popen_posix_group_flags, tail_text, update_job  # noqa: E402

DEFAULT_TIMEOUT = 3600


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: _job_wrapper.py <job_id> <cmd> [<arg>...]", file=sys.stderr)
        return 1

    job_id = sys.argv[1]
    cmd = sys.argv[2:]

    try:
        timeout_sec = int(os.environ.get("LLM_WIKI_JOB_TIMEOUT", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout_sec = DEFAULT_TIMEOUT

    # Используем Popen вручную вместо subprocess.run(timeout=...),
    # чтобы при таймауте ГАРАНТИРОВАННО убить дочерний процесс (иначе он
    # продолжает работать zombie'ем, а wrapper уже завершился).
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_posix_group_flags(),
        )
    except Exception as exc:  # noqa: BLE001
        update_job(
            job_id,
            status="failed",
            exit_code=-1,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            stderr_tail=f"Wrapper spawn failed: {exc}",
        )
        return 1

    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        update_job(
            job_id,
            status="done" if proc.returncode == 0 else "failed",
            exit_code=proc.returncode,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            stdout_tail=tail_text(stdout),
            stderr_tail=tail_text(stderr),
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        # На Windows убиваем всё дерево процессов (taskkill /T /F),
        # иначе дочерние node.exe от Claude остаются зомби.
        kill_proc_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", "…[process did not exit after kill()]…"
        update_job(
            job_id,
            status="failed",
            exit_code=-2,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            stdout_tail=tail_text(stdout or ""),
            stderr_tail=f"Timeout after {timeout_sec}s — process killed\n{tail_text(stderr or '')}",
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        kill_proc_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        update_job(
            job_id,
            status="failed",
            exit_code=-1,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            stderr_tail=f"Wrapper exception: {exc}",
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
