"""Общий jobs-трекер для dashboard и hooks.

Dashboard (Flask) и хуки (session-end auto-ingest, APScheduler lint) должны
писать в один и тот же `state/jobs.json`, чтобы все задачи были видны в UI.

Два способа запуска:
- `run_job_thread(job, cmd)` — запуск в треде текущего процесса (dashboard).
- `run_job_detached(job, cmd)` — запуск в отдельном detached subprocess
  (хук session-end — чтобы ingest пережил окончание хука).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from lib.state import load_state, locked, save_state

HERE = Path(__file__).resolve().parent
SHARED_ROOT = HERE.parent.parent
JOBS_STATE = SHARED_ROOT / "state" / "jobs.json"
MAX_JOBS = 200
TAIL_LIMIT = 4000

_lock = threading.Lock()


@dataclass
class Job:
    id: str
    type: str  # "ingest" | "lint"
    project: str
    status: str  # "running" | "done" | "failed"
    started_at: str
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    source: Optional[str] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    options: dict = field(default_factory=dict)
    trigger: str = "manual"  # "manual" | "auto" | "schedule"


def make_job(
    *,
    job_type: str,
    project: str,
    trigger: str = "manual",
    source: Optional[str] = None,
    options: Optional[dict] = None,
) -> Job:
    return Job(
        id=uuid.uuid4().hex,
        type=job_type,
        project=project,
        status="running",
        started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        source=source,
        options=options or {},
        trigger=trigger,
    )


def load_jobs() -> list[dict]:
    data = load_state(JOBS_STATE, default={"jobs": []})
    return data.get("jobs", [])


def save_jobs(jobs: list[dict]) -> None:
    save_state(JOBS_STATE, {"jobs": jobs[-MAX_JOBS:]})


def append_job(job: Job) -> None:
    # _lock — внутрипроцессный; locked() — межпроцессный (filelock).
    # Dashboard и detached wrapper работают в разных процессах.
    with _lock, locked(JOBS_STATE):
        jobs = load_jobs()
        jobs.append(asdict(job))
        save_jobs(jobs)


def update_job(job_id: str, **fields) -> None:
    # Инвариант JOB_LIFECYCLE: терминальный status всегда имеет finished_at.
    # Если caller передал status=done/failed но забыл finished_at — auto-add.
    if fields.get("status") in ("done", "failed") and "finished_at" not in fields:
        fields["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock, locked(JOBS_STATE):
        jobs = load_jobs()
        for j in jobs:
            if j.get("id") == job_id:
                j.update(fields)
                break
        save_jobs(jobs)


def tail_text(text: str, n: int = TAIL_LIMIT) -> str:
    if not text or len(text) <= n:
        return text or ""
    return "… [обрезано] …\n" + text[-n:]


DEFAULT_JOB_TIMEOUT_SEC = 3600


def kill_proc_tree(proc: "subprocess.Popen | None") -> None:
    """Убивает процесс и всех его детей.

    На Windows `proc.kill()` шлёт TerminateProcess и гасит только родителя.
    Claude CLI запускает node.exe и его дети переживут kill. Используем
    `taskkill /T /F` чтобы захватить всё дерево.

    На POSIX (Linux/macOS) `proc.kill()` шлёт SIGKILL родителю — то же самое.
    Поэтому запускаем детей в новой process group (через start_new_session
    в Popen — см. popen_posix_group_flags()), и здесь шлём SIGKILL всей группе
    через `os.killpg`.
    """
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
    else:
        # POSIX: если Popen создал новую process group (start_new_session=True),
        # os.getpgid(proc.pid) == proc.pid, и SIGKILL на группу убьёт всё дерево.
        import signal as _signal
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, _signal.SIGKILL)
        except (OSError, ProcessLookupError):
            # fallback: одиночный kill (если Popen не создал группу)
            try:
                proc.kill()
            except OSError:
                pass


def popen_posix_group_flags() -> dict:
    """Возвращает kwargs для Popen, чтобы на POSIX создать новую process group.

    Использовать: subprocess.Popen(cmd, ..., **popen_posix_group_flags()).
    Без этого os.killpg в kill_proc_tree не убьёт дочерние процессы
    (они унаследуют process group родителя = Python-скрипта).

    На Windows возвращает пустой dict — там дерево убивается через taskkill /T.
    """
    if os.name == "nt":
        return {}
    return {"start_new_session": True}


def run_job_thread(
    job: Job,
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout_sec: int = DEFAULT_JOB_TIMEOUT_SEC,
) -> None:
    """Запускает subprocess в отдельном thread (для dashboard)."""
    append_job(job)

    def runner():
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd) if cwd else None,
                **popen_posix_group_flags(),
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_sec)
                update_job(
                    job.id,
                    status="done" if proc.returncode == 0 else "failed",
                    exit_code=proc.returncode,
                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    stdout_tail=tail_text(stdout),
                    stderr_tail=tail_text(stderr),
                )
            except subprocess.TimeoutExpired:
                kill_proc_tree(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", "…[process did not exit after kill()]…"
                update_job(
                    job.id,
                    status="failed",
                    exit_code=-2,
                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    stdout_tail=tail_text(stdout or ""),
                    stderr_tail=f"Timeout after {timeout_sec}s — process killed\n{tail_text(stderr or '')}",
                )
        except Exception as exc:  # noqa: BLE001
            stdout, stderr = "", ""
            if proc is not None:
                kill_proc_tree(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except Exception:
                    pass
            update_job(
                job.id,
                status="failed",
                exit_code=-1,
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                stdout_tail=tail_text(stdout or ""),
                stderr_tail=f"Exception: {exc}\n{tail_text(stderr or '')}",
            )

    threading.Thread(target=runner, daemon=True, name=f"job-{job.id[:8]}").start()


def run_job_detached(
    job: Job,
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout_sec: int = DEFAULT_JOB_TIMEOUT_SEC,
) -> None:
    """Запускает subprocess detached — процесс переживёт окончание родителя.

    Используется в хуке session-end: сам хук должен вернуться за 10 сек,
    а ingest занимает 30-90 сек. Wrapper-скрипт `_job_wrapper.py` обновит
    jobs.json когда cmd завершится.
    """
    append_job(job)

    wrapper = SHARED_ROOT / "scripts" / "_job_wrapper.py"
    full_cmd = [sys.executable, str(wrapper), job.id] + cmd

    kwargs: dict = {}
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    env = {**os.environ, "LLM_WIKI_JOB_TIMEOUT": str(timeout_sec)}

    try:
        subprocess.Popen(
            full_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            env=env,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        update_job(
            job.id,
            status="failed",
            exit_code=-1,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            stderr_tail=f"Popen failed: {exc}",
        )
