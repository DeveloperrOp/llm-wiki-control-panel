"""Атомарная работа с JSON-state-файлами."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    from filelock import FileLock, Timeout  # type: ignore
    _HAS_FILELOCK = True
except ImportError:
    _HAS_FILELOCK = False


@contextmanager
def locked(path: Path, timeout: float = 10.0):
    """Межпроцессная блокировка файла на время read-modify-write.

    Создаёт `<path>.lock` рядом с файлом. Если filelock не установлен —
    работает как no-op (внутрипроцессную защиту должен обеспечить вызывающий).
    """
    if not _HAS_FILELOCK:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with FileLock(str(lock_path), timeout=timeout):
            yield
    except Timeout:
        # При таймауте всё равно продолжаем — лучше возможная гонка,
        # чем полное зависание хука/dashboard'а.
        yield


def load_state(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(default or {})


def save_state(path: Path, data: dict) -> None:
    """Атомарная запись: пишем во временный файл, потом replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_state(path: Path, key: str, value) -> None:
    data = load_state(path)
    data[key] = value
    save_state(path, data)
