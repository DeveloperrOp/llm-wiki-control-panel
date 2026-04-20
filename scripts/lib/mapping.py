"""Маршрутизация: $CLAUDE_PROJECT_DIR → vault_root.

project-map.json формат:
    {
      "version": 1,
      "vault_base": "C:/path/to/OBSIDIAN",
      "mappings": [
        {
          "name": "My Project",
          "cwd_patterns": ["*my-project*"],
          "vault_root": "C:/path/to/OBSIDIAN/My Project",
          "auto_ingest": false
        }
      ],
      "unassigned_root": "C:/path/to/OBSIDIAN/.unassigned"
    }

cwd_patterns — список fnmatch-шаблонов.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from lib.state import locked, save_state


# Корень .shared/ (два уровня вверх от этого файла: lib → scripts → .shared)
SHARED_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MAP = SHARED_ROOT / "config" / "project-map.json"


@dataclass
class ProjectResolution:
    name: str
    vault_root: Path
    is_unassigned: bool
    auto_ingest: bool = False
    lint_schedule: str | None = None  # cron-выражение или None
    context_limit: int | None = None  # лимит символов инжекта в SessionStart (None = дефолт)


def _default_map() -> dict:
    return {
        "version": 1,
        "mappings": [],
        "unassigned_root": str(SHARED_ROOT.parent / ".unassigned"),
    }


def load_map(map_path: Path | None = None) -> dict:
    path = map_path or DEFAULT_MAP
    if not path.exists():
        return _default_map()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_map()
    if not isinstance(data, dict):
        return _default_map()
    return data


def _normalize(p: str) -> str:
    """Нормализует путь для fnmatch: слеши вперёд, lower-case."""
    return str(p).replace("\\", "/").lower()


def resolve_project(
    cwd: str | None,
    map_path: Path | None = None,
    session_id: str | None = None,
) -> ProjectResolution:
    """Определяет проект по cwd. Возвращает ProjectResolution.

    Если cwd не матчится ни на один проект и пустой — в качестве папки для
    unassigned используется session_id[:8] (если передан), чтобы разные сессии
    без cwd не складывались в одну общую папку 'unknown/'.
    """
    mapping = load_map(map_path)
    cwd_norm = _normalize(cwd) if cwd else ""

    for entry in mapping.get("mappings", []):
        if not isinstance(entry, dict):
            continue
        vr = entry.get("vault_root")
        if not vr:
            continue  # битая запись без vault_root — пропускаем вместо KeyError
        patterns = entry.get("cwd_patterns") or []
        for pat in patterns:
            if fnmatch.fnmatch(cwd_norm, _normalize(pat)):
                return ProjectResolution(
                    name=entry.get("name", "unnamed"),
                    vault_root=Path(vr),
                    is_unassigned=False,
                    auto_ingest=bool(entry.get("auto_ingest", False)),
                    lint_schedule=entry.get("lint_schedule") or None,
                    context_limit=entry.get("context_limit") or None,
                )

    unassigned_root = Path(
        mapping.get("unassigned_root")
        or str(SHARED_ROOT.parent / ".unassigned")
    )
    if cwd:
        folder_name = Path(cwd).name
    elif session_id:
        folder_name = f"unknown-{session_id[:8]}"
    else:
        folder_name = "unknown"
    return ProjectResolution(
        name=folder_name,
        vault_root=unassigned_root / folder_name,
        is_unassigned=True,
        auto_ingest=False,
    )


def list_projects(map_path: Path | None = None) -> list[ProjectResolution]:
    mapping = load_map(map_path)
    result = []
    for entry in mapping.get("mappings", []):
        if not isinstance(entry, dict):
            continue
        vr = entry.get("vault_root")
        if not vr:
            continue  # битая запись — пропускаем
        result.append(
            ProjectResolution(
                name=entry.get("name", "unnamed"),
                vault_root=Path(vr),
                is_unassigned=False,
                auto_ingest=bool(entry.get("auto_ingest", False)),
                lint_schedule=entry.get("lint_schedule") or None,
                context_limit=entry.get("context_limit") or None,
            )
        )
    return result


_EDITABLE_FIELDS = (
    "auto_ingest",
    "lint_schedule",
    "cwd_patterns",
    "vault_root",
    "name",
    "context_limit",
)


def _write_map(path: Path, mapping: dict) -> None:
    """Атомарная запись через общий save_state из lib/state.py.

    Раньше была локальная копия tempfile+os.replace. Заменена на save_state —
    единая точка для атомарной JSON-записи во всём проекте.
    """
    save_state(path, mapping)


def update_project_settings(
    project_name: str,
    updates: dict,
    map_path: Path | None = None,
) -> bool:
    """Изменяет запись проекта в project-map.json (read-modify-write под lock'ом)."""
    path = map_path or DEFAULT_MAP
    # locked() защищает межпроцессную RMW: dashboard PATCH + параллельный
    # create_project/delete_project не должны перетирать друг друга.
    with locked(path):
        if not path.exists():
            return False
        mapping = load_map(path)
        entries = mapping.get("mappings", [])
        found = False
        for entry in entries:
            if entry.get("name") == project_name:
                for key in _EDITABLE_FIELDS:
                    if key in updates:
                        entry[key] = updates[key]
                found = True
                break
        if not found:
            return False
        _write_map(path, mapping)
        return True


def create_project(entry: dict, map_path: Path | None = None) -> tuple[bool, str]:
    """Добавляет новый проект в project-map.json.

    entry — словарь {name, vault_root, cwd_patterns?, auto_ingest?, lint_schedule?}
    Возвращает (ok, error_message).
    """
    if not entry.get("name"):
        return False, "name required"
    if not entry.get("vault_root"):
        return False, "vault_root required"

    path = map_path or DEFAULT_MAP
    with locked(path):
        if not path.exists():
            return False, f"project-map.json not found: {path}"
        mapping = load_map(path)
        entries = mapping.setdefault("mappings", [])
        if any(e.get("name") == entry["name"] for e in entries):
            return False, f"project already exists: {entry['name']}"

        new_entry = {
            "name": entry["name"],
            "cwd_patterns": entry.get("cwd_patterns") or [],
            "vault_root": entry["vault_root"],
            "auto_ingest": bool(entry.get("auto_ingest", False)),
            "lint_schedule": entry.get("lint_schedule") or None,
        }
        entries.append(new_entry)
        _write_map(path, mapping)
        return True, ""


def delete_project(project_name: str, map_path: Path | None = None) -> bool:
    """Удаляет запись проекта из project-map.json (папки на диске НЕ трогает)."""
    path = map_path or DEFAULT_MAP
    with locked(path):
        if not path.exists():
            return False
        mapping = load_map(path)
        entries = mapping.get("mappings", [])
        new_entries = [e for e in entries if e.get("name") != project_name]
        if len(new_entries) == len(entries):
            return False
        mapping["mappings"] = new_entries
        _write_map(path, mapping)
        return True


def ensure_chats_dir(resolution: ProjectResolution) -> Path:
    """Создаёт raw/chats/ если нужно и возвращает путь."""
    chats_dir = resolution.vault_root / "raw" / "chats"
    chats_dir.mkdir(parents=True, exist_ok=True)
    return chats_dir


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    cwd = sys.argv[1] if len(sys.argv) > 1 else str(Path.cwd())
    r = resolve_project(cwd)
    print(f"cwd: {cwd}")
    print(f"name: {r.name}")
    print(f"vault_root: {r.vault_root}")
    print(f"is_unassigned: {r.is_unassigned}")
    print(f"auto_ingest: {r.auto_ingest}")
