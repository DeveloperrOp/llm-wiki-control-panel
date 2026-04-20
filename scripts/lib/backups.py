"""Универсальные бэкапы для деструктивных операций над vault'ами.

Используется перед: архивированием log.md, оптимизацией index.md через Claude,
разбиением index.md на под-индексы и т.п.

Формат: <vault_root>/.backups/<ISO-timestamp>-<operation>/<relative-path>
Например:
    .backups/
      2026-04-19-23-15-archive-log/
        log.md
        log-archive.md           (если создавался)
      2026-04-19-23-40-optimize-index/
        index.md
      2026-04-19-23-55-split-index/
        index.md

Каждый бэкап — отдельная папка, содержит файлы В ТОМ ВИДЕ В КАКОМ ОНИ БЫЛИ ДО
операции. Восстановление: копируем всё обратно в vault_root.

Метаданные бэкапа (meta.json в папке бэкапа):
    {
        "operation": "archive-log",
        "created_at": "2026-04-19 23:15:30",
        "description": "Архивирование log.md: оставлено 20 записей, 45 перемещено в log-archive.md",
        "files": ["log.md", "log-archive.md"]
    }
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path


BACKUPS_DIRNAME = ".backups"
META_FILE = "meta.json"


def _backups_root(vault_root: Path) -> Path:
    return vault_root / BACKUPS_DIRNAME


def _slugify(name: str) -> str:
    """Простая санитизация имени операции для пути: a-z0-9- только."""
    out = []
    for ch in name.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in ("-", "_"):
            out.append("-")
        else:
            out.append("-")
    return "".join(out).strip("-") or "op"


def create_backup(
    vault_root: Path,
    operation: str,
    files: list[Path],
    description: str = "",
) -> Path:
    """Создаёт снапшот указанных файлов перед операцией.

    Args:
        vault_root: корень vault
        operation: короткий идентификатор ("archive-log", "optimize-index")
        files: абсолютные пути к файлам, которые нужно сохранить.
               Файлы должны лежать ВНУТРИ vault_root.
        description: человекочитаемое описание для UI

    Returns:
        Path к созданной папке бэкапа.

    Если какой-то файл не существует — он просто не включается в бэкап
    (не ошибка, возможно файл ещё не создан).
    """
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    # uuid-суффикс защищает от коллизии при параллельных бэкапах в ту же секунду
    # (иначе второй вызов затирает meta.json первого).
    import uuid as _uuid
    uniq = _uuid.uuid4().hex[:6]
    backup_dir = _backups_root(vault_root) / f"{ts}-{_slugify(operation)}-{uniq}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    saved_files: list[str] = []
    for src in files:
        src = Path(src)
        if not src.exists():
            continue
        try:
            rel = src.resolve().relative_to(vault_root.resolve())
        except ValueError:
            # Файл вне vault_root — пропускаем ради безопасности
            continue
        dest = backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        saved_files.append(str(rel).replace("\\", "/"))

    meta = {
        "operation": operation,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "description": description,
        "files": saved_files,
    }
    (backup_dir / META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Автоочистка старых бэкапов (best-effort; не ломаем create при ошибке)
    try:
        cleanup_old_backups(vault_root)
    except Exception:
        pass
    return backup_dir


def list_backups(vault_root: Path) -> list[dict]:
    """Список всех бэкапов в vault, новые сверху.

    Каждый элемент:
        {
            "id": "2026-04-19-23-15-archive-log",  # имя папки — стабильный id
            "path": "d:/.../.backups/2026-04-19-23-15-archive-log",
            "operation": "archive-log",
            "created_at": "2026-04-19 23:15:30",
            "description": "...",
            "files": ["log.md", ...],
            "size": 12345  # сумма байт
        }
    """
    root = _backups_root(vault_root)
    if not root.exists():
        return []
    result: list[dict] = []
    for d in sorted(root.iterdir(), reverse=True):
        # Симлинки пропускаем — кто-то мог создать симлинк наружу vault'а
        # (например, на C:/Users/xxx/Documents). cleanup_old_backups
        # потом не должен их удалять.
        if d.is_symlink():
            continue
        if not d.is_dir():
            continue
        meta_path = d / META_FILE
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        else:
            meta = {}
        # Считаем размер
        size = 0
        for p in d.rglob("*"):
            if p.is_file() and p.name != META_FILE:
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
        result.append({
            "id": d.name,
            "path": str(d).replace("\\", "/"),
            "operation": meta.get("operation") or "unknown",
            "created_at": meta.get("created_at") or "",
            "description": meta.get("description") or "",
            "files": meta.get("files") or [],
            "size": size,
        })
    return result


def _safe_backup_dir(vault_root: Path, backup_id: str) -> Path | None:
    """Резолвит путь к папке бэкапа и проверяет что она внутри .backups/.

    Защита от path traversal: backup_id приходит из HTTP-запроса, и без
    проверки relative_to атакующий мог бы передать '../../etc'.
    Возвращает None если путь выходит за пределы .backups/.
    """
    root = _backups_root(vault_root)
    bdir = (root / backup_id)
    try:
        bdir_resolved = bdir.resolve()
        bdir_resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return bdir


def restore_backup(vault_root: Path, backup_id: str) -> dict:
    """Восстанавливает файлы из бэкапа обратно в vault_root.

    Перед восстановлением создаёт новый бэкап с operation="pre-restore"
    от текущего состояния (чтобы можно было откатить откат).

    Returns:
        {"ok": True/False, "restored": [...], "error": "..."}
    """
    bdir = _safe_backup_dir(vault_root, backup_id)
    if bdir is None:
        return {"ok": False, "error": "invalid backup_id"}
    if not bdir.exists() or not bdir.is_dir():
        return {"ok": False, "error": f"backup not found: {backup_id}"}

    meta_path = bdir / META_FILE
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    files = meta.get("files") or []
    if not files:
        # Если meta пустой — берём все файлы в папке кроме meta.json
        files = []
        for p in bdir.rglob("*"):
            if p.is_file() and p.name != META_FILE:
                rel = p.relative_to(bdir)
                files.append(str(rel).replace("\\", "/"))

    # Сохраняем текущее состояние этих же файлов — чтобы можно было откатить
    current_files = [vault_root / f for f in files]
    try:
        create_backup(
            vault_root,
            operation="pre-restore-" + (meta.get("operation") or "unknown"),
            files=current_files,
            description=f"Автосохранение перед восстановлением «{backup_id}»",
        )
    except Exception:
        # Не ломаем восстановление если pre-restore не удался
        pass

    restored: list[str] = []
    skipped_unsafe: list[str] = []
    vault_resolved = vault_root.resolve()
    bdir_resolved = bdir.resolve()
    for rel in files:
        src = bdir / rel
        dest = vault_root / rel
        # Защита от вредоносного meta.json["files"] с '../' — файлы и src, и dest
        # должны быть строго внутри соответствующих корней. Иначе pre-restore
        # бэкап уже создан, но сам restore не должен писать наружу vault'а.
        try:
            src.resolve().relative_to(bdir_resolved)
            dest.resolve().relative_to(vault_resolved)
        except (ValueError, OSError):
            skipped_unsafe.append(rel)
            continue
        if not src.exists():
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored.append(rel)
        except OSError as exc:
            return {"ok": False, "error": f"failed to restore {rel}: {exc}"}

    result: dict = {"ok": True, "restored": restored}
    if skipped_unsafe:
        result["skipped_unsafe"] = skipped_unsafe
    return result


def delete_backup(vault_root: Path, backup_id: str) -> bool:
    """Удаляет конкретный бэкап."""
    bdir = _safe_backup_dir(vault_root, backup_id)
    if bdir is None:
        return False
    if not bdir.exists() or not bdir.is_dir():
        return False
    try:
        shutil.rmtree(bdir)
        return True
    except OSError:
        return False


def cleanup_old_backups(
    vault_root: Path,
    *,
    max_age_days: int = 30,
    keep_per_operation: int = 10,
) -> dict:
    """Удаляет бэкапы, соответствующие ОБА условия:
      - старше max_age_days
      - больше чем keep_per_operation самых свежих на эту операцию

    Проще говоря: в каждой группе (по operation) держим минимум
    keep_per_operation свежих; всё что старше max_age_days И не входит в эти
    top-N — удаляем.

    Returns: {"deleted": [...], "kept": N, "total_before": N}.
    """
    # Защита от опасных значений: держим минимум 1 свежий бэкап на операцию
    keep_per_operation = max(1, int(keep_per_operation))
    max_age_days = max(1, int(max_age_days))

    root = _backups_root(vault_root)
    if not root.exists():
        return {"deleted": [], "kept": 0, "total_before": 0}

    import time
    now = time.time()
    age_threshold = now - max_age_days * 86400

    # Группируем бэкапы по operation
    groups: dict[str, list[tuple[float, Path]]] = {}
    total = 0
    for d in root.iterdir():
        # Симлинки игнорируем целиком (см. list_backups): shutil.rmtree
        # на симлинк-на-директорию удалил бы содержимое целевой папки.
        if d.is_symlink():
            continue
        if not d.is_dir():
            continue
        total += 1
        meta_path = d / META_FILE
        op = "unknown"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                op = meta.get("operation") or "unknown"
            except (json.JSONDecodeError, OSError):
                pass
        try:
            mtime = d.stat().st_mtime
        except OSError:
            continue
        groups.setdefault(op, []).append((mtime, d))

    deleted: list[str] = []
    for op, entries in groups.items():
        # Самые свежие сверху
        entries.sort(key=lambda x: -x[0])
        # keep_per_operation свежих неприкосновенны
        candidates_for_delete = entries[keep_per_operation:]
        for mtime, d in candidates_for_delete:
            if mtime < age_threshold:
                try:
                    shutil.rmtree(d)
                    deleted.append(d.name)
                except OSError:
                    pass

    return {
        "deleted": deleted,
        "kept": total - len(deleted),
        "total_before": total,
    }
