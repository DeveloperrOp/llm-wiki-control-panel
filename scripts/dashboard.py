"""Flask-дашборд Control Panel — Этапы 3 + 4.

Endpoints:
    GET    /                       → главная (HTML)
    GET    /api/health
    GET    /api/projects           → список проектов со статистикой
    GET    /api/unassigned
    GET    /api/jobs
    GET    /api/schedules          → текущие запланированные lint-джобы
    POST   /api/ingest             → запустить ingest (body: project, source)
    POST   /api/lint               → запустить lint  (body: project, semantic?)
    POST   /api/assign             → привязать unassigned-чат к проекту
    DELETE /api/chat               → удалить чат
    PATCH  /api/settings/<project> → изменить auto_ingest / lint_schedule
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request, send_file

HERE = Path(__file__).resolve().parent
SHARED_ROOT = HERE.parent
VAULT_BASE = SHARED_ROOT.parent

sys.path.insert(0, str(HERE))

from lib.mapping import (  # noqa: E402
    ProjectResolution,
    create_project,
    delete_project,
    list_projects,
    load_map,
    update_project_settings,
)
from lib.state import load_state  # noqa: E402
from lib.jobs import (  # noqa: E402
    append_job,
    load_jobs,
    make_job,
    run_job_thread,
    tail_text,
    update_job,
)
from lib.context_injection import (  # noqa: E402
    MAX_CONTEXT_CHARS,
    WARN_THRESHOLD,
    compute_injection,
)
from lib.active_sessions import (  # noqa: E402
    STATE_FILE as ACTIVE_SESSIONS_STATE,
    MTIME_ALIVE_SEC,
    unregister as active_unregister,
)
from lib.session_dump import dump_transcript as _dump_transcript  # noqa: E402
from lib.mapping import resolve_project  # noqa: E402

# APScheduler
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

LINT_HISTORY = SHARED_ROOT / "state" / "lint-history.json"
INGEST_SCRIPT = HERE / "ingest.py"
LINT_SCRIPT = HERE / "lint.py"

WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
CHAT_PREVIEW_CHARS = 2000

PROMPTS_DIR = SHARED_ROOT / "prompts"
ALLOWED_PROMPTS = {"ingest-ru", "lint-semantic-ru", "optimize-index-ru"}
MAX_PROMPT_SIZE = 64 * 1024  # 64 KB

app = Flask(
    __name__,
    template_folder=str(SHARED_ROOT / "dashboard" / "templates"),
    static_folder=str(SHARED_ROOT / "dashboard" / "static"),
)
# Jinja перечитывает шаблон при каждом рендере, если его mtime изменился.
# Так правки HTML подхватываются без рестарта процесса (F5 в браузере достаточно).
app.config["TEMPLATES_AUTO_RELOAD"] = True


# =====================================================================
#  Статистика по проектам
# =====================================================================


def _count_files(path: Path, pattern: str = "*.md") -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return sum(1 for _ in path.rglob(pattern))


def _last_lint(project_name: str) -> Optional[str]:
    history = load_state(LINT_HISTORY, default={})
    entry = history.get(project_name)
    return entry.get("finished_at") if entry else None


def _project_stats(res: ProjectResolution) -> dict:
    vault = res.vault_root
    # Быстрая оценка инжект-контекста (лёгкая операция — чтение 2 файлов)
    try:
        inj = compute_injection(vault, res.name, limit=res.context_limit)
        context = {
            "raw_size": inj["raw_size"],
            "effective_size": inj["effective_size"],
            "limit": inj["limit"],
            "warn_threshold": inj["warn_threshold"],
            "status": inj["status"],
            "truncated": inj["truncated"],
        }
    except Exception:
        context = None
    # cwd_patterns — подтягиваем из project-map.json (их нет в ProjectResolution)
    cwd_patterns: list[str] = []
    try:
        mp = load_map()
        for entry in mp.get("mappings", []):
            if entry.get("name") == res.name:
                cwd_patterns = list(entry.get("cwd_patterns") or [])
                break
    except Exception:
        pass
    return {
        "name": res.name,
        "vault_root": str(vault).replace("\\", "/"),
        "cwd_patterns": cwd_patterns,
        "auto_ingest": res.auto_ingest,
        "lint_schedule": res.lint_schedule,
        "context_limit": res.context_limit,
        "chats_count": _count_files(vault / "raw" / "chats"),
        "wiki_pages_count": _count_files(vault / "wiki"),
        "last_lint_at": _last_lint(res.name),
        "exists": vault.exists(),
        "context_injection": context,
    }


def _gather_unassigned() -> list[dict]:
    mapping = load_map()
    unassigned_root = Path(mapping.get("unassigned_root") or (VAULT_BASE / ".unassigned"))
    if not unassigned_root.exists():
        return []
    result: list[dict] = []
    for chat_file in sorted(unassigned_root.rglob("raw/chats/*.md")):
        try:
            stat = chat_file.stat()
        except OSError:
            continue
        folder = chat_file.parents[2].name
        result.append({
            "path": str(chat_file).replace("\\", "/"),
            "folder": folder,
            "name": chat_file.name,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return result


# =====================================================================
#  APScheduler: lint по cron-расписанию
# =====================================================================

scheduler = BackgroundScheduler(daemon=True)
_scheduler_lock = threading.Lock()


def _scheduled_lint(project_name: str) -> None:
    """Вызывается APScheduler'ом когда срабатывает cron-триггер."""
    # Guard: если для этого проекта уже идёт lint (manual или предыдущий
    # scheduled который ещё не завершился) — не запускаем второй параллельно.
    # Иначе два процесса пишут один и тот же lint-report и lint-history.
    try:
        existing = load_jobs()
        for j in existing[-30:]:  # достаточно взглянуть на последние
            if (j.get("project") == project_name
                and j.get("type") == "lint"
                and j.get("status") == "running"):
                log_app = app.logger if hasattr(app, "logger") else None
                msg = f"scheduled lint skipped: уже running manual/prior scheduled для {project_name}"
                if log_app:
                    log_app.info(msg)
                else:
                    print(msg)
                return
    except Exception:
        pass  # не блокируем расписание если load_jobs упал

    job = make_job(
        job_type="lint",
        project=project_name,
        trigger="schedule",
        options={"semantic": False, "save": True},
    )
    cmd = [sys.executable, str(LINT_SCRIPT), project_name, "--save"]
    run_job_thread(job, cmd, cwd=SHARED_ROOT)
    # Обновим lint-history сразу — даже если job ещё running, отметим попытку
    # (финальный finished_at проставит run_job_thread в jobs.json)


def _job_id_for(project_name: str) -> str:
    return f"lint:{project_name}"


def register_scheduled_lints() -> None:
    """Читает project-map.json и регистрирует все lint_schedule в APScheduler."""
    with _scheduler_lock:
        # Убираем все старые lint-джобы
        for job in scheduler.get_jobs():
            if job.id.startswith("lint:"):
                scheduler.remove_job(job.id)
        # Регистрируем заново
        for p in list_projects():
            if not p.lint_schedule:
                continue
            try:
                trigger = CronTrigger.from_crontab(p.lint_schedule)
            except ValueError as e:
                app.logger.warning(
                    "Невалидный cron для %s: %r (%s)", p.name, p.lint_schedule, e
                )
                continue
            scheduler.add_job(
                _scheduled_lint,
                trigger=trigger,
                args=[p.name],
                id=_job_id_for(p.name),
                replace_existing=True,
                name=f"lint {p.name}",
            )


def current_schedules() -> list[dict]:
    """Список запланированных lint-джобов — для UI/отладки."""
    result = []
    for job in scheduler.get_jobs():
        if not job.id.startswith("lint:"):
            continue
        result.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else None,
        })
    return result


# =====================================================================
#  API
# =====================================================================


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/project/<path:name>")
def project_page(name: str):
    res = _find_project(name)
    if res is None:
        abort(404)
    return render_template("project.html", project_name=name)


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/help")
def help_page():
    return render_template("help.html")


def _pick_folder_windows(initial: str, title: str) -> tuple[int, str, str]:
    """Windows: PowerShell + WinForms FolderBrowserDialog."""
    def _ps_sanitize(s: str, limit: int = 200) -> str:
        # Жёсткая санитация для PS: убираем управляющие символы (разрывают
        # single-quoted строку), ограничиваем длину, дублируем одинарные кавычки.
        s = "".join(ch for ch in s if ch.isprintable() and ch not in ("\r", "\n"))
        return s[:limit].replace("'", "''")

    safe_title = _ps_sanitize(title)
    safe_initial = _ps_sanitize(initial, limit=500) if initial else ""

    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f = New-Object System.Windows.Forms.Form;"
        "$f.TopMost = $true; $f.Width=1; $f.Height=1; $f.StartPosition='CenterScreen';"
        "$f.Show() | Out-Null; [void]$f.Focus(); [System.Windows.Forms.Application]::DoEvents();"
        "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$dlg.Description = '{safe_title}';"
        "$dlg.ShowNewFolderButton = $true;"
    )
    if safe_initial:
        ps_script += f"$dlg.SelectedPath = '{safe_initial}';"
    ps_script += (
        "$res = $dlg.ShowDialog($f);"
        "$f.Close();"
        "if ($res -eq 'OK') { Write-Output $dlg.SelectedPath } else { exit 2 }"
    )

    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _pick_folder_macos(initial: str, title: str) -> tuple[int, str, str]:
    """macOS: AppleScript `choose folder` через osascript."""
    # AppleScript escape: обратный слеш и двойная кавычка в double-quoted строке
    def _as_escape(s: str, limit: int = 200) -> str:
        s = "".join(ch for ch in s if ch.isprintable() and ch not in ("\r", "\n"))
        return s[:limit].replace("\\", "\\\\").replace('"', '\\"')

    safe_title = _as_escape(title)
    script = f'with prompt "{safe_title}"'
    if initial:
        safe_initial = _as_escape(initial, limit=500)
        script += f' default location (POSIX file "{safe_initial}")'
    # try/on error: при отмене AppleScript возвращает non-zero — у нас exit 2
    full = (
        f'try\n'
        f'  set f to choose folder {script}\n'
        f'  return POSIX path of f\n'
        f'on error number -128\n'  # пользователь отменил
        f'  do shell script "exit 2"\n'
        f'end try'
    )
    proc = subprocess.run(
        ["osascript", "-e", full],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@app.post("/api/pick-folder")
def api_pick_folder():
    """Открывает нативный диалог выбора папки.

    Windows: PowerShell + WinForms FolderBrowserDialog.
    macOS: AppleScript `choose folder` через osascript.
    Linux: не поддерживается (нет единого native dialog).

    Диалог всплывёт у пользователя (дашборд запущен локально). Блокирующий.

    Body (optional):
        initial_path: стартовая папка
        title: заголовок окна

    Returns:
        {ok: true, path: "..."} при выборе
        {ok: false, cancelled: true} при отмене
    """
    payload = request.get_json(silent=True) or {}
    initial = (payload.get("initial_path") or "").strip()
    title = (payload.get("title") or "Выберите папку").strip()

    try:
        if os.name == "nt":
            rc, stdout, stderr = _pick_folder_windows(initial, title)
        elif sys.platform == "darwin":
            rc, stdout, stderr = _pick_folder_macos(initial, title)
        else:
            return jsonify(
                error="pick-folder поддерживается только на Windows и macOS",
                hint="введи путь вручную в поле vault_root",
            ), 400
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="timeout (5 min)"), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)), 500

    if rc == 2:
        return jsonify(ok=False, cancelled=True)
    if rc != 0:
        return jsonify(ok=False, error=f"dialog exit={rc}, stderr={stderr[:200]}"), 500

    chosen = stdout.strip()
    if not chosen:
        return jsonify(ok=False, cancelled=True)
    return jsonify(ok=True, path=chosen.replace("\\", "/"))


@app.get("/vault-asset/<project>/<path:asset_path>")
def vault_asset(project: str, asset_path: str):
    """Отдаёт файл из vault — для картинок, встроенных в GUIDE.md.
    Безопасность: файл должен быть внутри vault_root проекта."""
    res = _find_project(project)
    if res is None:
        abort(404)
    try:
        target = (res.vault_root / asset_path).resolve()
        target.relative_to(res.vault_root.resolve())  # защита от path traversal
    except (ValueError, OSError):
        abort(403)
    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target)


@app.get("/api/guide")
def api_guide():
    """Возвращает содержимое GUIDE.md из любого vault'а где он найдётся.

    Относительные пути к картинкам (`raw/assets/...`) переписываются в
    абсолютные URL `/vault-asset/<project>/<path>` — чтобы <img> работал в браузере.
    В Obsidian исходный markdown всё равно видит относительные пути (он берёт
    файл напрямую, а не через этот endpoint).
    """
    for p in list_projects():
        for name in ("GUIDE.md", "guide.md"):
            candidate = p.vault_root / name
            if candidate.exists():
                try:
                    content = candidate.read_text(encoding="utf-8")
                except OSError:
                    continue
                # Переписываем ![alt](raw/...) и ![alt](./raw/...) → /vault-asset/...
                from urllib.parse import quote as _url_quote
                project_q = _url_quote(p.name)
                def _rewrite(m):
                    alt = m.group(1)
                    path = m.group(2).lstrip("./")
                    return f"![{alt}](/vault-asset/{project_q}/{path})"
                content = re.sub(
                    r"!\[([^\]]*)\]\(((?:\./)?raw/[^)\s]+)\)",
                    _rewrite,
                    content,
                )
                return jsonify(
                    content=content,
                    source_vault=p.name,
                    source_path=str(candidate).replace("\\", "/"),
                )
    return jsonify(
        content="# Гайд не найден\n\nНе нашёл `GUIDE.md` ни в одном vault.",
        source_vault=None,
        source_path=None,
    ), 404


# =====================================================================
#  API: Prompts (редактор промптов)
# =====================================================================


@app.get("/api/prompts")
def api_prompts_list():
    """Список промптов (имя, размер, mtime)."""
    items = []
    for name in sorted(ALLOWED_PROMPTS):
        path = PROMPTS_DIR / f"{name}.md"
        if not path.exists():
            continue
        stat = path.stat()
        items.append({
            "name": name,
            "filename": f"{name}.md",
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return jsonify(prompts=items)


@app.get("/api/prompts/<name>")
def api_prompt_get(name: str):
    if name not in ALLOWED_PROMPTS:
        return jsonify(error=f"unknown prompt: {name}"), 404
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        return jsonify(error="not found"), 404
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(
        name=name,
        filename=f"{name}.md",
        content=content,
        size=len(content),
        mtime=datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    )


@app.put("/api/prompts/<name>")
def api_prompt_save(name: str):
    if name not in ALLOWED_PROMPTS:
        return jsonify(error=f"unknown prompt: {name}"), 404
    payload = request.get_json(silent=True) or {}
    content = payload.get("content")
    if content is None:
        return jsonify(error="content required"), 400
    # len() считает символы, не байты. Для лимита на размер файла нужны байты —
    # иначе кириллица/эмодзи обходят лимит (1 символ = 2-4 байта UTF-8).
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_PROMPT_SIZE:
        return jsonify(error=f"too large (>{MAX_PROMPT_SIZE} bytes UTF-8)"), 400

    path = PROMPTS_DIR / f"{name}.md"
    # backup перед записью
    if path.exists():
        backup = PROMPTS_DIR / f"{name}.md.bak"
        try:
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return jsonify(error=str(exc)), 500
    return jsonify(
        ok=True,
        size=len(content),
        mtime=datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    )


# =====================================================================
#  API: Projects CRUD (создать / удалить / расширенный patch)
# =====================================================================


@app.post("/api/projects")
def api_projects_create():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    vault_root = (payload.get("vault_root") or "").strip()
    cwd_patterns = payload.get("cwd_patterns") or []

    if not name:
        return jsonify(error="name required"), 400
    if not vault_root:
        return jsonify(error="vault_root required"), 400
    if not isinstance(cwd_patterns, list):
        return jsonify(error="cwd_patterns must be list"), 400
    # Валидация vault_root: должен быть абсолютным путём
    try:
        vault_path = Path(vault_root)
        if not vault_path.is_absolute():
            return jsonify(error="vault_root must be absolute path"), 400
        vault_root = str(vault_path).replace("\\", "/")
    except (ValueError, OSError) as exc:
        return jsonify(error=f"invalid vault_root: {exc}"), 400

    entry = {
        "name": name,
        "vault_root": vault_root,
        "cwd_patterns": [str(p).strip() for p in cwd_patterns if str(p).strip()],
        "auto_ingest": bool(payload.get("auto_ingest", False)),
        "lint_schedule": payload.get("lint_schedule") or None,
    }
    ok, err = create_project(entry)
    if not ok:
        return jsonify(error=err), 400

    # Создадим базовую структуру vault если просят
    if payload.get("init_structure"):
        base = Path(vault_root)
        for sub in ("raw/articles", "raw/chats", "raw/docs", "raw/assets",
                    "wiki/entities", "wiki/concepts", "wiki/sources"):
            (base / sub).mkdir(parents=True, exist_ok=True)

    register_scheduled_lints()
    created = _find_project(name)
    return jsonify(
        ok=True,
        project=_project_stats(created) if created else None,
    )


@app.delete("/api/projects/<path:name>")
def api_projects_delete(name: str):
    ok = delete_project(name)
    if not ok:
        return jsonify(error=f"project not found: {name}"), 404
    register_scheduled_lints()  # удалит и запланированные job'ы этого проекта
    return jsonify(ok=True)


@app.get("/api/health")
def api_health():
    return jsonify(
        ok=True,
        version="4.0-auto",
        shared_root=str(SHARED_ROOT).replace("\\", "/"),
        time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        scheduler_running=scheduler.running,
    )


@app.get("/api/projects")
def api_projects():
    return jsonify(projects=[_project_stats(p) for p in list_projects()])


@app.get("/api/unassigned")
def api_unassigned():
    return jsonify(items=_gather_unassigned())


@app.get("/api/jobs")
def api_jobs():
    limit = request.args.get("limit", default=30, type=int)
    limit = max(1, min(200, limit))
    jobs = load_jobs()[-limit:]
    jobs.reverse()
    return jsonify(jobs=jobs)


@app.get("/api/schedules")
def api_schedules():
    return jsonify(schedules=current_schedules())


@app.get("/api/raw-map")
def api_raw_map():
    """Возвращает сырой project-map.json — нужно settings.html для cwd_patterns."""
    return jsonify(load_map())


@app.post("/api/ingest")
def api_ingest():
    payload = request.get_json(silent=True) or {}
    project = payload.get("project") or ""
    source = payload.get("source") or ""
    try:
        timeout = int(payload.get("timeout", 900))
    except (TypeError, ValueError):
        return jsonify(error="timeout must be integer"), 400
    timeout = max(30, min(3600, timeout))  # clamp от безумных значений

    if not project:
        return jsonify(error="project required"), 400
    if not source:
        return jsonify(error="source required"), 400
    if _find_project(project) is None:
        return jsonify(error=f"project not found: {project}"), 404

    job = make_job(
        job_type="ingest",
        project=project,
        trigger="manual",
        source=source,
        options={"timeout": timeout},
    )
    cmd = [
        sys.executable, str(INGEST_SCRIPT),
        project,
        "--source", source,
        "--timeout", str(timeout),
    ]
    run_job_thread(job, cmd, cwd=SHARED_ROOT, timeout_sec=timeout + 60)
    return jsonify(job=asdict(job))


@app.post("/api/lint")
def api_lint():
    payload = request.get_json(silent=True) or {}
    project = payload.get("project") or ""
    semantic = bool(payload.get("semantic", False))
    save_report = bool(payload.get("save", True))

    if not project:
        return jsonify(error="project required"), 400
    if _find_project(project) is None:
        return jsonify(error=f"project not found: {project}"), 404

    job = make_job(
        job_type="lint",
        project=project,
        trigger="manual",
        options={"semantic": semantic, "save": save_report},
    )
    cmd = [sys.executable, str(LINT_SCRIPT), project]
    if semantic:
        cmd.append("--semantic")
    if save_report:
        cmd.append("--save")
    run_job_thread(job, cmd, cwd=SHARED_ROOT)

    # После успешного lint добавим finished_at в lint-history —
    # это делает сам lint.py, но пусть и UI обновляется быстрее
    if not semantic:
        # не форсим, lint отрабатывает быстро, UI подхватит через polling
        pass
    return jsonify(job=asdict(job))


@app.post("/api/assign")
def api_assign():
    payload = request.get_json(silent=True) or {}
    chat_path = payload.get("chat_path") or ""
    project_name = payload.get("project") or ""

    if not chat_path or not project_name:
        return jsonify(error="chat_path and project required"), 400

    src = Path(chat_path)
    if not src.exists() or not src.is_file():
        return jsonify(error=f"chat not found: {chat_path}"), 404
    try:
        src.resolve().relative_to(VAULT_BASE.resolve())
    except (ValueError, OSError):
        return jsonify(error="outside vault"), 403

    target = None
    for p in list_projects():
        if p.name == project_name:
            target = p
            break
    if target is None:
        return jsonify(error=f"project not found: {project_name}"), 404

    dest_dir = target.vault_root / "raw" / "chats"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    try:
        src.rename(dest)
    except OSError as exc:
        return jsonify(error=f"move failed: {exc}"), 500

    return jsonify(ok=True, moved_to=str(dest).replace("\\", "/"))


@app.delete("/api/chat")
def api_chat_delete():
    payload = request.get_json(silent=True) or {}
    chat_path = payload.get("chat_path") or ""
    if not chat_path:
        return jsonify(error="chat_path required"), 400
    p = Path(chat_path)
    if not p.exists() or not p.is_file():
        return jsonify(error="not found"), 404
    try:
        p.resolve().relative_to(VAULT_BASE.resolve())
    except (ValueError, OSError):
        return jsonify(error="outside vault"), 403
    try:
        p.unlink()
    except OSError as exc:
        return jsonify(error=f"delete failed: {exc}"), 500
    return jsonify(ok=True)


@app.get("/api/project/<path:name>")
def api_project_details(name: str):
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    return jsonify(
        project=_project_stats(res),
        chats=_gather_chats(res.vault_root),
        wiki_pages=_gather_wiki_pages(res.vault_root),
    )


@app.get("/api/graph/<path:name>")
def api_graph(name: str):
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    wiki_pages = _gather_wiki_pages(res.vault_root)
    graph = _build_graph(wiki_pages, res.vault_root)
    return jsonify(**graph)


@app.get("/api/hook-log")
def api_hook_log():
    """Последние N строк hook-log.txt для просмотра в UI (диагностика)."""
    try:
        n = int(request.args.get("lines") or 50)
    except ValueError:
        n = 50
    n = max(1, min(500, n))
    hook_log = SHARED_ROOT / "state" / "hook-log.txt"
    if not hook_log.exists():
        return jsonify(lines=[], total=0, exists=False)
    try:
        text = hook_log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return jsonify(error=str(exc)), 500
    all_lines = [ln for ln in text.splitlines() if ln.strip()]
    tail = all_lines[-n:]
    return jsonify(lines=tail, total=len(all_lines), exists=True)


@app.get("/api/system-status")
def api_system_status():
    """Статус здоровья системы: хуки зарегистрированы? сессии ловятся?
    Возвращает { overall: 'ok'|'warn'|'error', checks: [{id, label, status, detail}] }."""
    from pathlib import Path as _Path
    checks: list[dict] = []

    # 1) Хуки зарегистрированы в ~/.claude/settings.json?
    claude_settings = _Path.home() / ".claude" / "settings.json"
    try:
        import json as _json
        data = _json.loads(claude_settings.read_text(encoding="utf-8")) if claude_settings.exists() else {}
        hooks = (data or {}).get("hooks", {}) or {}
        required = ("SessionStart", "SessionEnd")
        missing = [h for h in required if not hooks.get(h)]
        if not claude_settings.exists():
            checks.append({
                "id": "hooks",
                "label": "Хуки Claude Code зарегистрированы",
                "status": "error",
                "detail": f"Файл ~/.claude/settings.json не найден. Система не будет ловить сессии.",
            })
        elif missing:
            checks.append({
                "id": "hooks",
                "label": "Хуки Claude Code зарегистрированы",
                "status": "error",
                "detail": f"Не хватает: {', '.join(missing)}. Добавь через skill update-config.",
            })
        else:
            checks.append({
                "id": "hooks",
                "label": "Хуки Claude Code зарегистрированы",
                "status": "ok",
                "detail": "SessionStart и SessionEnd настроены.",
            })
    except Exception as exc:
        checks.append({
            "id": "hooks", "label": "Хуки Claude Code зарегистрированы",
            "status": "error", "detail": f"Не смог прочитать settings.json: {exc}",
        })

    # 2) Хуки реально работают? Есть записи за последние 24 часа в hook-log.
    hook_log = SHARED_ROOT / "state" / "hook-log.txt"
    try:
        if not hook_log.exists():
            checks.append({
                "id": "hook_activity", "label": "Хуки срабатывают",
                "status": "warn",
                "detail": "Файл hook-log.txt ещё не создан. Запусти Claude Code хотя бы раз в проекте.",
            })
        else:
            mtime = hook_log.stat().st_mtime
            age_hours = (time.time() - mtime) / 3600
            if age_hours < 24:
                checks.append({
                    "id": "hook_activity", "label": "Хуки срабатывают",
                    "status": "ok",
                    "detail": f"Последнее срабатывание {age_hours:.1f} ч назад.",
                })
            else:
                checks.append({
                    "id": "hook_activity", "label": "Хуки срабатывают",
                    "status": "warn",
                    "detail": f"Последнее срабатывание {age_hours:.1f} ч назад. Возможно, Claude Code давно не запускался.",
                })
    except OSError as exc:
        checks.append({
            "id": "hook_activity", "label": "Хуки срабатывают",
            "status": "warn", "detail": f"Не смог прочитать hook-log: {exc}",
        })

    # 3) project-map.json существует и валиден
    from lib.mapping import DEFAULT_MAP
    try:
        mp = load_map()
        n_projects = len(mp.get("mappings", []))
        checks.append({
            "id": "project_map",
            "label": "Карта проектов загружена",
            "status": "ok" if n_projects > 0 else "warn",
            "detail": f"Проектов: {n_projects}" if n_projects > 0 else "Нет проектов. Создай через «+ Новый проект».",
        })
    except Exception as exc:
        checks.append({
            "id": "project_map", "label": "Карта проектов загружена",
            "status": "error", "detail": f"Не смог прочитать project-map.json: {exc}",
        })

    # 4) Падения хуков за последние 24 часа — анализируем хвост hook-log
    try:
        recent_failures: list[str] = []
        if hook_log.exists():
            text = hook_log.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            # Смотрим последние 300 строк
            for ln in lines[-300:]:
                # Формат: "[YYYY-MM-DD HH:MM:SS] source: message"
                # Падения: "failed", "UNHANDLED", "stdin read failed", "format_session failed", "write failed"
                low = ln.lower()
                if any(k in low for k in ("unhandled:", "failed:", " fail ", "error:")):
                    # Извлечь timestamp
                    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", ln)
                    if m:
                        from datetime import datetime as _dt
                        try:
                            ts = _dt.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                            age_h = (time.time() - ts.timestamp()) / 3600
                            if age_h < 24:
                                recent_failures.append(ln)
                        except ValueError:
                            pass
        if not recent_failures:
            checks.append({
                "id": "hook_failures",
                "label": "Нет падений хуков за 24 часа",
                "status": "ok",
                "detail": "В hook-log нет записей с ошибками за последние сутки.",
            })
        else:
            last = recent_failures[-1]
            checks.append({
                "id": "hook_failures",
                "label": f"Падения хуков за 24ч: {len(recent_failures)}",
                "status": "warn" if len(recent_failures) < 5 else "error",
                "detail": f"Последнее: {last[:200]}",
            })
    except OSError as exc:
        checks.append({
            "id": "hook_failures", "label": "Анализ падений хуков",
            "status": "warn", "detail": f"Не смог прочитать hook-log: {exc}",
        })

    # Overall = худший из статусов
    order = {"ok": 0, "warn": 1, "error": 2}
    overall = max((c["status"] for c in checks), key=lambda s: order.get(s, 0), default="ok")
    return jsonify(overall=overall, checks=checks)


@app.get("/api/today-stats")
def api_today_stats():
    """Статистика за сегодня (по локальному времени сервера)."""
    from datetime import datetime as _dt, time as _time
    today_start = _dt.combine(_dt.now().date(), _time.min).timestamp()

    # Сохранённые диалоги сегодня — по всем проектам + .unassigned
    sessions_saved = 0
    wiki_created = 0

    mapping = load_map()
    # Папки, где ищем
    roots_for_chats: list[Path] = []
    roots_for_wiki: list[Path] = []
    for entry in mapping.get("mappings", []):
        v = Path(entry["vault_root"])
        roots_for_chats.append(v / "raw" / "chats")
        roots_for_wiki.append(v / "wiki")
    unassigned_root = Path(mapping.get("unassigned_root") or (VAULT_BASE / ".unassigned"))
    if unassigned_root.exists():
        roots_for_chats.extend(unassigned_root.glob("*/raw/chats"))

    for root in roots_for_chats:
        if not root.exists():
            continue
        for f in root.glob("*.md"):
            try:
                if f.stat().st_mtime >= today_start:
                    sessions_saved += 1
            except OSError:
                pass

    for root in roots_for_wiki:
        if not root.exists():
            continue
        for f in root.rglob("*.md"):
            try:
                if f.stat().st_mtime >= today_start:
                    wiki_created += 1
            except OSError:
                pass

    # Операции сегодня — читаем jobs.json
    jobs = load_jobs()
    jobs_today = 0
    for j in jobs:
        ts = j.get("started_at") or ""
        # started_at формат "YYYY-MM-DD HH:MM:SS"
        if ts and ts.startswith(_dt.now().strftime("%Y-%m-%d")):
            jobs_today += 1

    return jsonify(
        sessions_saved=sessions_saved,
        wiki_pages_created=wiki_created,
        jobs_today=jobs_today,
    )


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
TRANSCRIPT_SCAN_WINDOW_SEC = 60  # файлы с mtime свежее — считаем живыми
SUBSESSION_MARKERS = ("# INGEST", "# LINT")  # префиксы первого user-промпта у наших subsessions


def _is_subsession_transcript(jsonl_path: Path) -> bool:
    """Heuristic: auto-ingest / lint subsessions начинаются с фиксированных промптов.
    Читает первые ~10 строк и ищет маркер в user-message content."""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 10:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                # queue-operation может нести content с первым промптом
                content = d.get("content")
                if isinstance(content, str) and content.lstrip().startswith(SUBSESSION_MARKERS):
                    return True
                msg = d.get("message") or {}
                msg_content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(msg_content, str) and msg_content.lstrip().startswith(SUBSESSION_MARKERS):
                    return True
    except OSError:
        return False
    return False


def _extract_cwd_from_transcript(jsonl_path: Path) -> str:
    """Читает первые 10 строк и возвращает первое встреченное поле cwd."""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 10:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                cwd = d.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


def _scan_live_transcripts() -> list[dict]:
    """Сканирует ~/.claude/projects/**/*.jsonl с mtime свежее TRANSCRIPT_SCAN_WINDOW_SEC.
    Возвращает записи в том же формате, что и active-sessions.json."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    now = time.time()
    threshold = now - TRANSCRIPT_SCAN_WINDOW_SEC
    results: list[dict] = []
    for jsonl in CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        if mtime < threshold:
            continue
        # Пропустить subagent-транскрипты (лежат в <sid>/subagents/...)
        if "subagents" in jsonl.parts:
            continue
        # Пропустить наши служебные subsessions (ingest/lint)
        if _is_subsession_transcript(jsonl):
            continue
        sid = jsonl.stem
        cwd_raw = _extract_cwd_from_transcript(jsonl)
        if not cwd_raw:
            continue
        results.append({
            "sid": sid,
            "transcript_path": str(jsonl).replace("\\", "/"),
            "started_at": int(mtime),  # приблизительно; точнее нет
            "cwd_raw": cwd_raw,
            "_mtime": mtime,
        })
    return results


def _build_session_item(sid: str, cwd_raw: str, transcript_path: str,
                       started_at: int, now: float) -> dict | None:
    """Обогащает сессию данными о проекте и состоянии."""
    try:
        resolution = resolve_project(cwd_raw)
    except Exception:
        return None

    mtime_ts = 0.0
    alive = False
    try:
        p = Path(transcript_path)
        if p.exists():
            mtime_ts = p.stat().st_mtime
            alive = (now - mtime_ts) < MTIME_ALIVE_SEC
    except OSError:
        pass

    return {
        "sid": sid,
        "sid_short": sid[:8],
        "project": resolution.name if not resolution.is_unassigned else None,
        "is_unassigned": resolution.is_unassigned,
        "cwd_raw": cwd_raw,
        "transcript_path": transcript_path,
        "started_at": datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M:%S") if started_at else "",
        "started_at_ts": started_at,
        "age_sec": int(now - started_at) if started_at else 0,
        "alive": alive,
        "last_activity_sec": int(now - mtime_ts) if mtime_ts else None,
    }


def _collect_all_sessions() -> list[dict]:
    """Объединяет источники: active-sessions.json (данные хука) + скан .jsonl-ов.
    Дедуп по sid. Скан даёт более актуальные started_at, реестр — точный cwd."""
    state = load_state(ACTIVE_SESSIONS_STATE, default={})
    now = time.time()
    by_sid: dict[str, dict] = {}

    # Источник 1: реестр (хук)
    for _cwd_norm, sessions in state.items():
        for s in sessions:
            sid = s.get("sid") or ""
            if not sid:
                continue
            item = _build_session_item(
                sid=sid,
                cwd_raw=s.get("cwd_raw") or _cwd_norm,
                transcript_path=s.get("transcript_path") or "",
                started_at=int(s.get("started_at") or 0),
                now=now,
            )
            if item:
                by_sid[sid] = item

    # Источник 2: скан живых .jsonl (покрывает сессии до register() и VSCode /clear баг)
    for s in _scan_live_transcripts():
        sid = s["sid"]
        if sid in by_sid:
            # Реестр знает о ней — пропускаем (у реестра точнее started_at)
            continue
        item = _build_session_item(
            sid=sid,
            cwd_raw=s["cwd_raw"],
            transcript_path=s["transcript_path"],
            started_at=s["started_at"],
            now=now,
        )
        if item:
            by_sid[sid] = item

    items = list(by_sid.values())
    items.sort(key=lambda x: (not x["alive"], -x["started_at_ts"]))
    return items


@app.get("/api/active-sessions")
def api_all_active_sessions():
    """Все живые/осиротевшие сессии по всем проектам + непривязанные."""
    return jsonify(items=_collect_all_sessions())


@app.get("/api/project/<path:name>/active-sessions")
def api_project_active_sessions(name: str):
    """Живые и «осиротевшие» сессии этого проекта.
    Источники: реестр хуков + скан живых .jsonl (чтобы видеть сессии,
    стартовавшие до register() и не поддержанные хуками)."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404

    items = [s for s in _collect_all_sessions() if s.get("project") == name]
    return jsonify(items=items)


@app.post("/api/active-sessions/force-dump")
def api_active_sessions_force_dump():
    """Ручной backfill для sid.

    Body:
        sid: required — session id
        target_project: optional — если передан, сохраняем в raw/chats/ этого
            проекта (override автоматического резолва по cwd). Используется
            когда сессия непривязана, и пользователь хочет сразу прикрепить её
            к конкретному проекту.

    Источник сессии — реестр active-sessions.json ИЛИ скан живых .jsonl
    (чтобы работало для сессий, стартовавших до register()).
    """
    payload = request.get_json(silent=True) or {}
    sid = payload.get("sid") or ""
    target_project = (payload.get("target_project") or "").strip() or None
    if not sid:
        return jsonify(error="sid required"), 400

    # Ищем в реестре
    state = load_state(ACTIVE_SESSIONS_STATE, default={})
    found: Optional[dict] = None
    for _cwd_norm, sessions in state.items():
        for s in sessions:
            if s.get("sid") == sid:
                found = s
                break
        if found:
            break

    # Fallback: скан живых транскриптов (для сессий вне реестра)
    if not found:
        for s in _scan_live_transcripts():
            if s["sid"] == sid:
                found = s
                break

    if not found:
        return jsonify(error=f"session not found: {sid}"), 404

    transcript_path = found.get("transcript_path") or ""
    cwd_raw = found.get("cwd_raw") or ""

    # target_project override: передаём forced_project в dump_transcript,
    # чтобы обойти resolve_project (cwd_patterns могут не покрывать vault).
    # Без этого файл попадал в .unassigned/, а не в target vault.
    if target_project and _find_project(target_project) is None:
        return jsonify(error=f"target project not found: {target_project}"), 400

    try:
        ok = _dump_transcript(
            session_id=sid,
            transcript_path_str=transcript_path,
            cwd=cwd_raw,
            hook_event="ManualForceDump",
            reason=f"force-dump{':' + target_project if target_project else ''}",
            log_source="force-dump",
            forced_project=target_project,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=str(exc)), 500

    # Убираем из реестра независимо от результата дампа
    if cwd_raw:
        active_unregister(cwd_raw, sid)

    return jsonify(ok=bool(ok), sid=sid, dumped=bool(ok), target=target_project or "auto")


# =====================================================================
#  Массовый импорт архивных сессий Claude Code (из ~/.claude/projects/)
# =====================================================================

IMPORT_PREVIEW_CHARS = 200

# Глобальная очередь импорта: только один bulk-ingest одновременно.
# Обеспечивает последовательность: ingest запускается по одному, чтобы не
# перегружать API-лимиты Claude.
_import_lock = threading.Lock()

# Прогресс активных и недавних импортов для UI.
# Формат: { import_id: {
#   "project", "total", "done", "skipped", "errors",
#   "current_source", "status" ('running'|'done'|'failed'),
#   "started_at", "finished_at", "trigger_ingest"
# }}
# Храним до 20 последних; старые автоочищаются.
_import_progress: dict[str, dict] = {}
_import_progress_lock = threading.Lock()


def _progress_add(imp_id: str, data: dict) -> None:
    with _import_progress_lock:
        _import_progress[imp_id] = data
        # Зомби-очистка: записи status=running старше 2 часов считаем мёртвыми
        # (воркер мог упасть до установки статуса done/failed). Без этой
        # очистки они копятся бесконечно, блокируя автоочистку по N>20.
        from datetime import datetime as _dt
        zombie_cutoff = _dt.now().timestamp() - 2 * 3600
        for k, v in list(_import_progress.items()):
            if v.get("status") == "running":
                try:
                    started = _dt.strptime(
                        v.get("started_at") or "", "%Y-%m-%d %H:%M:%S"
                    ).timestamp()
                except ValueError:
                    continue
                if started < zombie_cutoff:
                    v["status"] = "failed"
                    v["finished_at"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

        # Автоочистка: храним последние 20 (running + finished)
        if len(_import_progress) > 20:
            finished = [k for k, v in _import_progress.items() if v.get("status") != "running"]
            finished.sort(key=lambda k: _import_progress[k].get("finished_at") or "")
            for k in finished[:-10]:  # Оставляем последние 10 завершённых
                _import_progress.pop(k, None)


def _progress_update(imp_id: str, **fields) -> None:
    with _import_progress_lock:
        if imp_id in _import_progress:
            _import_progress[imp_id].update(fields)


def _progress_incr(imp_id: str, field: str, delta: int = 1) -> None:
    """Атомарный инкремент счётчика (skipped/errors/done).

    Заменяет паттерн `_progress_update(id, X=_import_progress[id][X] + 1)`
    где read и write разделены, что создавало теоретическую гонку.
    """
    with _import_progress_lock:
        if imp_id in _import_progress:
            _import_progress[imp_id][field] = _import_progress[imp_id].get(field, 0) + delta


def _progress_get_all() -> list[dict]:
    with _import_progress_lock:
        result = []
        for imp_id, data in _import_progress.items():
            result.append({"id": imp_id, **data})
        # Running сверху, потом по started_at desc (ISO-строка сортируется лексикографически).
        result.sort(key=lambda x: (x.get("status") != "running", x.get("started_at") or ""), reverse=False)
        # reverse=False + running_first: но внутри groups хотим desc по started_at.
        # Проще: сначала сортируем всё по started_at desc, потом стабильная сортировка
        # кидает running наверх.
        result.sort(key=lambda x: x.get("started_at") or "", reverse=True)
        result.sort(key=lambda x: 0 if x.get("status") == "running" else 1)
        return result


def _first_user_prompt(jsonl_path: Path, max_chars: int = IMPORT_PREVIEW_CHARS) -> str:
    """Превью: первый user-message в транскрипте (для отображения в списке)."""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 40:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, str) and content.strip():
                    return content.strip()[:max_chars]
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            txt = (item.get("text") or "").strip()
                            if txt:
                                return txt[:max_chars]
    except OSError:
        pass
    return ""


@app.get("/api/claude-projects-folders")
def api_claude_projects_folders():
    """Список папок в ~/.claude/projects/ с архивными транскриптами.
    Каждая папка соответствует одному cwd (закодированному дефисами)."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return jsonify(folders=[])

    folders: list[dict] = []
    for folder in CLAUDE_PROJECTS_DIR.iterdir():
        if not folder.is_dir():
            continue
        jsonls = [p for p in folder.glob("*.jsonl") if p.is_file()]
        if not jsonls:
            continue
        # cwd из первого попавшегося транскрипта
        cwd = ""
        for jsonl in jsonls:
            cwd = _extract_cwd_from_transcript(jsonl)
            if cwd:
                break
        if not cwd:
            continue
        # Без subsession'ов для оценки
        real_sessions = [p for p in jsonls if not _is_subsession_transcript(p)]
        if not real_sessions:
            continue
        last_mtime = max(p.stat().st_mtime for p in real_sessions)
        folders.append({
            "encoded": folder.name,
            "cwd": cwd,
            "sessions_count": len(real_sessions),
            "subsessions_count": len(jsonls) - len(real_sessions),
            "last_mtime": datetime.fromtimestamp(last_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "last_mtime_ts": last_mtime,
        })

    folders.sort(key=lambda f: -f["last_mtime_ts"])
    return jsonify(folders=folders)


@app.get("/api/claude-projects-folder/<encoded>/sessions")
def api_claude_projects_folder_sessions(encoded: str):
    """Список транскриптов в конкретной папке ~/.claude/projects/.
    Query: ?project=<name> — помечает уже импортированные в этот проект."""
    folder = (CLAUDE_PROJECTS_DIR / encoded)
    try:
        folder.resolve().relative_to(CLAUDE_PROJECTS_DIR.resolve())
    except (ValueError, OSError):
        return jsonify(error="outside claude projects dir"), 403
    if not folder.exists() or not folder.is_dir():
        return jsonify(error="folder not found"), 404

    target_project = request.args.get("project") or ""
    existing_sids: set[str] = set()
    if target_project:
        res = _find_project(target_project)
        if res is not None:
            chats_dir = res.vault_root / "raw" / "chats"
            if chats_dir.exists():
                # Имя файла: YYYY-MM-DD-HH-MM-<sid8>.md → вытащим sid8
                for md in chats_dir.glob("*.md"):
                    stem = md.stem  # без .md
                    parts = stem.split("-")
                    # последняя часть — sid8 (или sid8+suffix). Извлекаем первые 8 символов.
                    if parts:
                        last = parts[-1]
                        existing_sids.add(last[:8])

    sessions: list[dict] = []
    for jsonl in folder.glob("*.jsonl"):
        if not jsonl.is_file():
            continue
        if _is_subsession_transcript(jsonl):
            continue
        try:
            st = jsonl.stat()
        except OSError:
            continue
        sid = jsonl.stem
        sid_short = sid[:8]
        sessions.append({
            "sid": sid,
            "sid_short": sid_short,
            "path": str(jsonl).replace("\\", "/"),
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "mtime_ts": st.st_mtime,
            "preview": _first_user_prompt(jsonl),
            "already_imported": sid_short in existing_sids,
        })

    sessions.sort(key=lambda s: -s["mtime_ts"])
    return jsonify(sessions=sessions, total=len(sessions))


def _import_worker(project_name: str, sources: list[str], trigger_ingest: bool,
                   import_id: str, ingest_timeout: int = 900) -> None:
    """Последовательно импортирует транскрипты и (опционально) запускает ingest.

    ingest_timeout — таймаут каждого ingest в секундах (по умолчанию 900).
    Communicate timeout даёт запас 300 сек на инициализацию subprocess.
    """
    _progress_add(import_id, {
        "project": project_name,
        "total": len(sources),
        "done": 0,
        "skipped": 0,
        "errors": 0,
        "current_source": "",
        "status": "running",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "trigger_ingest": bool(trigger_ingest),
    })

    # _import_lock уже захвачен вызывающим endpoint'ом через acquire(blocking=False).
    # Мы обязаны освободить его в finally. Если вход сюда был без лока (какой-то
    # тест/рефакторинг) — не пытаемся acquire, чтобы не уронить работу.
    try:
        from lib.transcript import format_session as _format_session
        res = _find_project(project_name)
        if res is None:
            _progress_update(import_id, status="failed",
                             finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return
        chats_dir = res.vault_root / "raw" / "chats"
        chats_dir.mkdir(parents=True, exist_ok=True)

        for src_path in sources:
            src = Path(src_path)
            _progress_update(import_id, current_source=src.name)

            if not src.exists():
                _progress_incr(import_id, "skipped")
                continue
            sid = src.stem
            sid_short = sid[:8]

            # Дедуп по sid: если файл с таким sid8 уже в chats/ — пропуск
            already = any(sid_short in md.stem for md in chats_dir.glob("*.md"))
            if already:
                _progress_incr(import_id, "skipped")
                continue

            # Читаем cwd из транскрипта
            cwd_from_jsonl = _extract_cwd_from_transcript(src) or str(res.vault_root)

            # Имя файла по mtime (чтобы даты совпадали с реальными)
            try:
                mtime = src.stat().st_mtime
            except OSError:
                mtime = time.time()
            ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d-%H-%M")
            out_path = chats_dir / f"{ts}-{sid_short}.md"
            if out_path.exists():
                _progress_incr(import_id, "skipped")
                continue

            try:
                markdown = _format_session(
                    src,
                    session_id=sid,
                    cwd=cwd_from_jsonl,
                    hook_event="BulkImport",
                )
                out_path.write_text(markdown, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                job = make_job(
                    job_type="ingest", project=project_name,
                    trigger="import", source=str(src).replace("\\", "/"),
                    options={"origin": "bulk-import", "stage": "format"},
                )
                append_job(job)
                update_job(
                    job.id, status="failed", exit_code=-1,
                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    stderr_tail=f"format_session failed: {exc}",
                )
                _progress_incr(import_id, "errors")
                continue

            # Если нужно — запускаем ingest СИНХРОННО (чтобы очередь была реально последовательной)
            if trigger_ingest:
                job = make_job(
                    job_type="ingest", project=project_name,
                    trigger="import", source=str(out_path).replace("\\", "/"),
                    options={"timeout": ingest_timeout, "origin": "bulk-import"},
                )
                append_job(job)
                cmd = [
                    sys.executable, str(INGEST_SCRIPT),
                    project_name,
                    "--source", str(out_path),
                    "--timeout", str(ingest_timeout),
                ]
                # Communicate ждёт ingest_timeout + 300 сек запас на старт subprocess
                communicate_timeout = ingest_timeout + 300
                proc = None
                try:
                    # Popen + kill_proc_tree вместо subprocess.run(timeout=) —
                    # без этого на Windows при таймауте дочерние процессы
                    # (claude → node.exe) остаются zombie.
                    from lib.jobs import kill_proc_tree, popen_posix_group_flags
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace",
                        cwd=str(SHARED_ROOT),
                        **popen_posix_group_flags(),
                    )
                    try:
                        stdout, stderr = proc.communicate(timeout=communicate_timeout)
                    except subprocess.TimeoutExpired:
                        kill_proc_tree(proc)
                        try:
                            stdout, stderr = proc.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            stdout, stderr = "", ""
                        update_job(
                            job.id, status="failed", exit_code=-2,
                            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            stderr_tail=f"ingest timeout (>{communicate_timeout}s) — tree killed\n{tail_text(stderr or '')}",
                        )
                        _progress_incr(import_id, "errors")
                        continue
                    update_job(
                        job.id,
                        status="done" if proc.returncode == 0 else "failed",
                        exit_code=proc.returncode,
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        stdout_tail=tail_text(stdout),
                        stderr_tail=tail_text(stderr),
                    )
                except Exception as exc:  # noqa: BLE001
                    update_job(
                        job.id, status="failed", exit_code=-1,
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        stderr_tail=f"subprocess error: {exc}",
                    )

            # Инкремент счётчика выполненных (на каждой итерации)
            _progress_incr(import_id, "done")

        # Финализация прогресса (внутри try, после цикла)
        _progress_update(import_id,
                         status="done",
                         current_source="",
                         finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        # Обязательный release лока, захваченного в endpoint'е.
        try:
            _import_lock.release()
        except RuntimeError:
            # Лок не был захвачен — вход сюда без acquire(), редкий случай.
            pass


@app.get("/api/import-progress")
def api_import_progress():
    """Список активных и недавних импортов."""
    return jsonify(imports=_progress_get_all())


@app.post("/api/import-sessions")
def api_import_sessions():
    """Массовый импорт архивных транскриптов Claude Code в проект.

    Body:
        project: required — имя целевого проекта
        session_paths: required — список абсолютных путей к .jsonl
        trigger_ingest: optional bool — запустить ingest после импорта (по очереди)
    """
    payload = request.get_json(silent=True) or {}
    project_name = (payload.get("project") or "").strip()
    paths = payload.get("session_paths") or []
    trigger = bool(payload.get("trigger_ingest"))
    # Опциональный ingest_timeout (если trigger_ingest=True). Clamp как в api_ingest.
    try:
        ingest_timeout = int(payload.get("ingest_timeout", 900))
    except (TypeError, ValueError):
        return jsonify(error="ingest_timeout must be integer"), 400
    ingest_timeout = max(30, min(3600, ingest_timeout))
    if not project_name:
        return jsonify(error="project required"), 400
    if not isinstance(paths, list) or not paths:
        return jsonify(error="session_paths required"), 400
    if _find_project(project_name) is None:
        return jsonify(error=f"project not found: {project_name}"), 404

    # Валидация путей: только внутри ~/.claude/projects/
    valid_paths: list[str] = []
    claude_proj_resolved = CLAUDE_PROJECTS_DIR.resolve()
    for p in paths:
        try:
            rp = Path(p).resolve()
            rp.relative_to(claude_proj_resolved)
            if rp.is_file() and rp.suffix == ".jsonl":
                valid_paths.append(str(rp))
        except (ValueError, OSError):
            continue

    if not valid_paths:
        return jsonify(error="no valid paths"), 400

    # Атомарный acquire: если лок уже захвачен — 409. Это устраняет TOCTOU
    # между `locked()` и `threading.Thread(...).start()`. Лок будет освобождён
    # внутри _import_worker в finally.
    if not _import_lock.acquire(blocking=False):
        return jsonify(
            error="import already running",
            hint="wait for current import to finish",
        ), 409

    # Запускаем в фоне — это может занять минуты (особенно с ingest)
    import uuid as _uuid
    import_id = _uuid.uuid4().hex[:12]
    try:
        threading.Thread(
            target=_import_worker,
            args=(project_name, valid_paths, trigger, import_id, ingest_timeout),
            daemon=True,
            name=f"bulk-import-{project_name[:8]}",
        ).start()
    except Exception:
        # Не смогли запустить тред — освобождаем лок чтобы не остался вечно.
        _import_lock.release()
        raise

    return jsonify(
        ok=True,
        import_id=import_id,
        queued=len(valid_paths),
        project=project_name,
        trigger_ingest=trigger,
    )


# =====================================================================
#  Архивирование log.md / бэкапы
# =====================================================================

from lib.backups import (  # noqa: E402
    cleanup_old_backups as _cleanup_old_backups,
    create_backup as _create_backup,
    delete_backup as _delete_backup,
    list_backups as _list_backups,
    restore_backup as _restore_backup,
)


def _split_log_entries(text: str) -> list[tuple[int, str]]:
    """Разбивает log.md на записи по заголовкам `## [...`.

    Возвращает список (start_line_index, заголовок). Заголовки — первая строка
    каждой записи. Записи — всё между двумя такими заголовками.
    """
    lines = text.splitlines(keepends=True)
    headers: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if ln.startswith("## ["):
            headers.append((i, ln.rstrip()))
    return headers


@app.get("/api/project/<path:name>/log-stats")
def api_log_stats(name: str):
    """Возвращает статистику log.md: число записей, размер, превью хвоста.
    Query: ?list=1 — добавить полный список заголовков + размер каждой записи."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    log_path = res.vault_root / "log.md"
    if not log_path.exists():
        return jsonify(exists=False, entries=0, size=0, chars=0)
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        size = log_path.stat().st_size
    except OSError as exc:
        return jsonify(error=f"read failed: {exc}"), 500
    lines = text.splitlines(keepends=True)
    headers = _split_log_entries(text)

    result = {
        "exists": True,
        "entries": len(headers),
        "size": size,
        "chars": len(text),
        "first_entry": (headers[0][1] if headers else ""),
        "last_entry": (headers[-1][1] if headers else ""),
    }

    if request.args.get("list"):
        # Детальный список: заголовок + сколько символов занимает запись
        detailed: list[dict] = []
        for n, (start, title) in enumerate(headers):
            end = headers[n + 1][0] if n + 1 < len(headers) else len(lines)
            body = "".join(lines[start:end])
            detailed.append({
                "idx": n,
                "title": title,
                "chars": len(body),
            })
        result["items"] = detailed

    return jsonify(**result)


@app.post("/api/project/<path:name>/archive-log")
def api_archive_log(name: str):
    """Архивирует старые записи log.md: оставляет последние N, остальные
    перемещает в log-archive.md.

    Body:
        keep_last: required int — сколько записей оставить в log.md
    """
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    payload = request.get_json(silent=True) or {}
    try:
        keep_last = int(payload.get("keep_last") or 0)
    except (ValueError, TypeError):
        return jsonify(error="keep_last must be integer"), 400
    if keep_last < 1:
        return jsonify(error="keep_last must be >= 1"), 400

    vault = res.vault_root
    log_path = vault / "log.md"
    archive_path = vault / "log-archive.md"

    if not log_path.exists():
        return jsonify(error="log.md not found"), 404

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return jsonify(error=f"read log.md failed: {exc}"), 500
    lines = text.splitlines(keepends=True)
    headers = _split_log_entries(text)
    total = len(headers)

    if total <= keep_last:
        return jsonify(ok=True, archived=0, kept=total, message="Нечего архивировать: записей меньше лимита.")

    # Шапка log.md — всё до первого `## [`. Её оставляем в log.md.
    header_end = headers[0][0]  # индекс первой строки `## [`
    log_header_text = "".join(lines[:header_end])

    # Точка разреза: начало (total - keep_last)-й записи
    cut_idx = headers[total - keep_last][0]

    # Записи, которые архивируем: от header_end до cut_idx
    archived_text = "".join(lines[header_end:cut_idx])
    # Записи, которые остаются: от cut_idx до конца
    remaining_entries_text = "".join(lines[cut_idx:])

    # Бэкап перед любой записью
    backup_files = [log_path]
    if archive_path.exists():
        backup_files.append(archive_path)
    _create_backup(
        vault,
        operation="archive-log",
        files=backup_files,
        description=f"Архивирование log.md: оставлено {keep_last} записей, перемещено {total - keep_last}",
    )

    # Порядок важен: сначала ПИШЕМ архив, потом обрезаем log.md.
    # Иначе при сбое между двумя write записи из log.md уже усечены,
    # а в archive.md их ещё нет — данные потеряны навсегда.
    # Кроме того, обе записи — через tempfile+replace для атомарности.
    import tempfile as _tempfile
    import os as _os

    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            _os.replace(tmp, path)
        except Exception:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            raise

    # 1) Сформировать + записать новый архив
    if archive_path.exists():
        try:
            old_archive = archive_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return jsonify(error=f"read archive failed: {exc}"), 500
        archive_new = old_archive.rstrip() + "\n\n" + archived_text
    else:
        archive_header = (
            "# 📦 Архив журнала\n\n"
            "Старые записи, перемещённые из `log.md` чтобы он не раздувался.\n"
            "Новые записи сверху не добавляются — только ниже предыдущего среза.\n\n"
            "---\n\n"
        )
        archive_new = archive_header + archived_text
    try:
        _atomic_write(archive_path, archive_new)
    except OSError as exc:
        return jsonify(error=f"write archive failed: {exc}"), 500

    # 2) Только теперь обрезаем log.md (архив уже на диске, данные сохранены)
    new_log = log_header_text + remaining_entries_text
    try:
        _atomic_write(log_path, new_log)
    except OSError as exc:
        return jsonify(
            error=f"write log.md failed after archive saved: {exc}",
            hint="archive already updated; log.md unchanged; use backup or manual merge",
        ), 500

    return jsonify(
        ok=True,
        archived=total - keep_last,
        kept=keep_last,
        log_size=log_path.stat().st_size,
        archive_size=archive_path.stat().st_size,
    )


@app.get("/api/project/<path:name>/backups")
def api_list_backups(name: str):
    """Список бэкапов проекта."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    return jsonify(backups=_list_backups(res.vault_root))


@app.post("/api/project/<path:name>/backups/<backup_id>/restore")
def api_restore_backup(name: str, backup_id: str):
    """Восстановить файлы из бэкапа."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    result = _restore_backup(res.vault_root, backup_id)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.delete("/api/project/<path:name>/backups/<backup_id>")
def api_delete_backup(name: str, backup_id: str):
    """Удалить бэкап навсегда."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    ok = _delete_backup(res.vault_root, backup_id)
    return jsonify(ok=ok)


@app.post("/api/project/<path:name>/backups/cleanup")
def api_cleanup_backups(name: str):
    """Ручная автоочистка: удаляет бэкапы старше 30 дней, кроме 10 самых
    свежих на каждый тип операции.

    Body (optional): {max_age_days: 30, keep_per_operation: 10}
    """
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    payload = request.get_json(silent=True) or {}
    try:
        max_age = int(payload.get("max_age_days") or 30)
        keep = int(payload.get("keep_per_operation") or 10)
    except (ValueError, TypeError):
        return jsonify(error="max_age_days/keep_per_operation must be integers"), 400
    result = _cleanup_old_backups(
        res.vault_root,
        max_age_days=max(1, max_age),
        keep_per_operation=max(1, keep),
    )
    return jsonify(**result)


# =====================================================================
#  Разбиение index.md на под-индексы
# =====================================================================

# Соответствие заголовков секций и имён под-индексов.
# Ключи — нормализованные (lower, без пробелов по краям) имена заголовков.
SECTION_TO_SUBINDEX = {
    "сущности": "index-entities.md",
    "entities": "index-entities.md",
    "концепции": "index-concepts.md",
    "concepts": "index-concepts.md",
    "источники": "index-sources.md",
    "sources": "index-sources.md",
}

SUBINDEX_FILES = ("index-entities.md", "index-concepts.md", "index-sources.md")


def _parse_index_sections(text: str) -> list[dict]:
    """Разбивает index.md на секции по заголовкам уровня `## `.

    Возвращает [{title, title_raw, start_idx, end_idx, body}] —
    индексы относятся к списку строк из text.splitlines(keepends=True).
    Всё до первого `## ` — это «шапка» (target=header).
    """
    lines = text.splitlines(keepends=True)
    sections: list[dict] = []
    indices: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            title_raw = ln[3:].rstrip()
            indices.append((i, title_raw))

    # Шапка
    header_end = indices[0][0] if indices else len(lines)
    if header_end > 0:
        sections.append({
            "title": "(шапка)",
            "title_raw": "",
            "start_idx": 0,
            "end_idx": header_end,
            "body": "".join(lines[:header_end]),
            "target": "header",
        })

    # Секции
    for n, (start, title_raw) in enumerate(indices):
        end = indices[n + 1][0] if n + 1 < len(indices) else len(lines)
        norm = title_raw.strip().lower()
        # Пробуем по полному совпадению и по первому слову
        target_file = SECTION_TO_SUBINDEX.get(norm)
        if target_file is None:
            first_word = norm.split()[0] if norm.split() else ""
            target_file = SECTION_TO_SUBINDEX.get(first_word)
        target = target_file if target_file else "root"
        sections.append({
            "title": title_raw,
            "title_raw": title_raw,
            "start_idx": start,
            "end_idx": end,
            "body": "".join(lines[start:end]),
            "target": target,
        })
    return sections


@app.get("/api/project/<path:name>/index-analyze")
def api_index_analyze(name: str):
    """Анализирует index.md: какие секции найдены, как их разложить по под-индексам.
    Возвращает превью нового root + под-индексов."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    idx_path = res.vault_root / "index.md"
    if not idx_path.exists():
        return jsonify(error="index.md not found"), 404

    try:
        text = idx_path.read_text(encoding="utf-8")
    except OSError as exc:
        return jsonify(error=f"read failed: {exc}"), 500
    sections = _parse_index_sections(text)

    # Проверяем, разбит ли уже. Если есть хотя бы один под-индекс но не все —
    # считаем «частично разбит» и предупреждаем UI об этом состоянии.
    subindex_exists = [(f, (res.vault_root / f).exists()) for f in SUBINDEX_FILES]
    existing_subindices = [f for f, ok in subindex_exists if ok]
    already_split = len(existing_subindices) == len(SUBINDEX_FILES)
    partial_split = 0 < len(existing_subindices) < len(SUBINDEX_FILES)

    # Считаем распределение
    sub_bodies: dict[str, list[str]] = {f: [] for f in SUBINDEX_FILES}
    root_parts: list[str] = []
    header_part: str = ""
    for s in sections:
        if s["target"] == "header":
            header_part = s["body"]
        elif s["target"] == "root":
            root_parts.append(s["body"])
        else:
            sub_bodies[s["target"]].append(s["body"])

    # Превью под-индексов
    sub_previews: dict[str, str] = {}
    for fname, parts in sub_bodies.items():
        if not parts:
            continue
        header = (
            f"# 📑 Под-индекс: {fname.replace('index-', '').replace('.md','').title()}\n\n"
            "Создан автоматически при разбиении `index.md`. "
            "Корневой `index.md` ссылается сюда.\n\n---\n\n"
        )
        sub_previews[fname] = header + "".join(parts).rstrip() + "\n"

    # Превью нового root (содержит шапку + секции для root + ссылки на под-индексы)
    links_block_lines = ["## 📑 Под-индексы", ""]
    for fname in SUBINDEX_FILES:
        if sub_bodies[fname]:
            label = fname.replace("index-", "").replace(".md", "").capitalize()
            links_block_lines.append(f"- [[{fname.replace('.md','')}]] — {label}")
    links_block_lines.append("")

    new_root = header_part.rstrip() + "\n\n"
    if root_parts:
        new_root += "".join(root_parts).rstrip() + "\n\n"
    new_root += "\n".join(links_block_lines)

    # Summary
    summary = []
    for s in sections:
        if s["target"] == "header":
            continue
        summary.append({
            "title": s["title_raw"],
            "target": s["target"],
            "lines": s["end_idx"] - s["start_idx"],
        })

    return jsonify(
        already_split=already_split,
        partial_split=partial_split,
        existing_subindices=existing_subindices,
        can_split=any(sub_bodies[f] for f in SUBINDEX_FILES),
        sections=summary,
        new_root_preview=new_root,
        sub_previews=sub_previews,
        original=text,
        original_chars=len(text),
        new_root_chars=len(new_root),
    )


@app.post("/api/project/<path:name>/split-index")
def api_split_index(name: str):
    """Разбивает index.md на под-индексы. Перед — backup."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    idx_path = res.vault_root / "index.md"
    if not idx_path.exists():
        return jsonify(error="index.md not found"), 404

    text = idx_path.read_text(encoding="utf-8")
    sections = _parse_index_sections(text)

    sub_bodies: dict[str, list[str]] = {f: [] for f in SUBINDEX_FILES}
    root_parts: list[str] = []
    header_part: str = ""
    for s in sections:
        if s["target"] == "header":
            header_part = s["body"]
        elif s["target"] == "root":
            root_parts.append(s["body"])
        else:
            sub_bodies[s["target"]].append(s["body"])

    if not any(sub_bodies[f] for f in SUBINDEX_FILES):
        return jsonify(error="no matching sections to split (ожидались «Сущности»/«Концепции»/«Источники»)"), 400

    # Backup: index.md + существующие под-индексы (если уже были)
    files_to_backup = [idx_path]
    for fname in SUBINDEX_FILES:
        p = res.vault_root / fname
        if p.exists():
            files_to_backup.append(p)
    _create_backup(
        res.vault_root,
        operation="split-index",
        files=files_to_backup,
        description="Разбиение index.md на под-индексы (entities/concepts/sources)",
    )

    # Запись под-индексов
    written: list[str] = []
    for fname, parts in sub_bodies.items():
        if not parts:
            continue
        path = res.vault_root / fname
        header = (
            f"# 📑 Под-индекс: {fname.replace('index-', '').replace('.md','').title()}\n\n"
            "Создан автоматически при разбиении `index.md`. "
            "Корневой `index.md` ссылается сюда.\n\n---\n\n"
        )
        path.write_text(header + "".join(parts).rstrip() + "\n", encoding="utf-8")
        written.append(fname)

    # Новый root
    links_block_lines = ["## 📑 Под-индексы", ""]
    for fname in SUBINDEX_FILES:
        if sub_bodies[fname]:
            label = fname.replace("index-", "").replace(".md", "").capitalize()
            links_block_lines.append(f"- [[{fname.replace('.md','')}]] — {label}")
    links_block_lines.append("")

    new_root = header_part.rstrip() + "\n\n"
    if root_parts:
        new_root += "".join(root_parts).rstrip() + "\n\n"
    new_root += "\n".join(links_block_lines)
    idx_path.write_text(new_root, encoding="utf-8")

    return jsonify(ok=True, written=written, new_root_chars=len(new_root))


@app.post("/api/project/<path:name>/merge-index")
def api_merge_index(name: str):
    """Обратное разбиению: сливает под-индексы обратно в index.md и удаляет их."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    idx_path = res.vault_root / "index.md"
    if not idx_path.exists():
        return jsonify(error="index.md not found"), 404

    sub_paths = {f: res.vault_root / f for f in SUBINDEX_FILES}
    existing = {f: p for f, p in sub_paths.items() if p.exists()}
    if not existing:
        return jsonify(error="no sub-indexes found (ничего сливать)"), 400

    # Backup: index.md + все существующие под-индексы
    files_to_backup = [idx_path] + list(existing.values())
    _create_backup(
        res.vault_root,
        operation="merge-index",
        files=files_to_backup,
        description="Слияние под-индексов обратно в index.md",
    )

    # Читаем текущий index.md, разбиваем на секции, убираем секцию «📑 Под-индексы»
    try:
        text = idx_path.read_text(encoding="utf-8")
    except OSError as exc:
        return jsonify(error=f"read index.md failed: {exc}"), 500
    sections = _parse_index_sections(text)
    root_parts: list[str] = []
    header_part: str = ""
    for s in sections:
        if s["target"] == "header":
            header_part = s["body"]
            continue
        title_low = (s["title_raw"] or "").lower()
        if "под-индекс" in title_low or "sub-index" in title_low:
            continue  # выбрасываем навигационную секцию
        root_parts.append(s["body"])

    # Читаем под-индексы, из каждого выделяем только секции (`## ...`) — без header'а под-индекса
    subindex_sections: list[str] = []
    try:
        for fname, p in existing.items():
            sub_text = p.read_text(encoding="utf-8")
            sub_sections = _parse_index_sections(sub_text)
            for s in sub_sections:
                if s["target"] == "header":
                    continue
                subindex_sections.append(s["body"])
    except OSError as exc:
        return jsonify(error=f"read sub-index failed: {exc}"), 500

    new_index = header_part.rstrip() + "\n\n"
    if root_parts:
        new_index += "".join(root_parts).rstrip() + "\n\n"
    new_index += "".join(subindex_sections).rstrip() + "\n"

    # Атомарная запись index.md (через tempfile+replace) — и только потом unlink
    # под-индексов. Если запись упадёт — все sub-index'ы сохранны, данные не потеряны.
    import tempfile as _tempfile
    import os as _os
    idx_parent = idx_path.parent
    idx_parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(prefix="index.", suffix=".tmp", dir=str(idx_parent))
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_index)
        _os.replace(tmp, idx_path)
    except Exception as exc:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
        return jsonify(error=f"write index.md failed: {exc}"), 500

    # Удаляем под-индексы (после успешной записи index.md)
    deleted_subs: list[str] = []
    failed_delete: list[str] = []
    for p in existing.values():
        try:
            p.unlink()
            deleted_subs.append(p.name)
        except OSError as exc:
            failed_delete.append(f"{p.name}: {exc}")

    result: dict = {"ok": True, "deleted": deleted_subs, "new_index_chars": len(new_index)}
    if failed_delete:
        result["warnings"] = failed_delete  # UI покажет предупреждение
    return jsonify(**result)


# =====================================================================
#  Claude-оптимизация index.md / log.md
# =====================================================================

OPTIMIZE_ALLOWED_FILES = {"index.md", "log.md"}


@app.post("/api/project/<path:name>/suggest-optimization")
def api_suggest_optimization(name: str):
    """Запускает claude -p с задачей сократить index.md или log.md.

    Body:
        file: required — "index.md" или "log.md"
        target_chars: optional int — целевой размер (дефолт: половина текущего)

    Возвращает { original, suggested, original_chars, suggested_chars }.
    Операция синхронная (ждёт 10-90 сек).
    """
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    payload = request.get_json(silent=True) or {}
    file_name = (payload.get("file") or "").strip()
    if file_name not in OPTIMIZE_ALLOWED_FILES:
        return jsonify(error=f"file must be one of {list(OPTIMIZE_ALLOWED_FILES)}"), 400

    target = res.vault_root / file_name
    if not target.exists():
        return jsonify(error=f"{file_name} not found in vault"), 404

    try:
        original = target.read_text(encoding="utf-8")
    except OSError as exc:
        return jsonify(error=f"read failed: {exc}"), 500
    if not original.strip():
        return jsonify(error="file is empty"), 400

    try:
        target_chars = int(payload.get("target_chars") or max(500, len(original) // 2))
    except (ValueError, TypeError):
        return jsonify(error="target_chars must be integer"), 400

    # Читаем промпт-шаблон
    prompt_path = PROMPTS_DIR / "optimize-index-ru.md"
    if not prompt_path.exists():
        return jsonify(error="optimize-index-ru.md not found"), 500
    from lib.runner import render_template, run_claude

    prompt = render_template(
        prompt_path.read_text(encoding="utf-8"),
        {
            "VAULT_ROOT": str(res.vault_root).replace("\\", "/"),
            "PROJECT_NAME": res.name,
            "TARGET_FILE": file_name,
            "TARGET_CHARS": str(target_chars),
            "CURRENT_CONTENT": original,
        },
    )

    result = run_claude(
        prompt=prompt,
        cwd=res.vault_root,
        permission_mode="bypassPermissions",
        timeout=300,
        dangerously_skip_permissions=True,
        exclude_user_claude_md=True,
    )
    if not result["success"]:
        return jsonify(
            error="claude -p failed",
            stderr=result.get("stderr", "")[:2000],
            timed_out=result.get("timed_out", False),
        ), 500

    suggested = result["stdout"].strip("\r\n")
    if not suggested.strip():
        return jsonify(error="empty output from claude"), 500

    return jsonify(
        file=file_name,
        original=original,
        suggested=suggested,
        original_chars=len(original),
        suggested_chars=len(suggested),
    )


@app.post("/api/project/<path:name>/apply-optimization")
def api_apply_optimization(name: str):
    """Применяет предложенный текст к файлу. Перед заменой — backup.

    Body:
        file: required — "index.md" или "log.md"
        new_content: required — новый текст файла
    """
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    payload = request.get_json(silent=True) or {}
    file_name = (payload.get("file") or "").strip()
    new_content = payload.get("new_content")
    if file_name not in OPTIMIZE_ALLOWED_FILES:
        return jsonify(error=f"file must be one of {list(OPTIMIZE_ALLOWED_FILES)}"), 400
    if not isinstance(new_content, str) or not new_content.strip():
        return jsonify(error="new_content required"), 400

    target = res.vault_root / file_name
    if not target.exists():
        return jsonify(error=f"{file_name} not found in vault"), 404

    # Backup → запись
    _create_backup(
        res.vault_root,
        operation=f"optimize-{file_name.replace('.md','')}",
        files=[target],
        description=f"Оптимизация {file_name} через Claude: {target.stat().st_size} → {len(new_content.encode('utf-8'))} байт",
    )
    target.write_text(new_content, encoding="utf-8")

    return jsonify(ok=True, size=target.stat().st_size)


@app.get("/api/inject-preview/<path:name>")
def api_inject_preview(name: str):
    """Возвращает полный предпросмотр того, что SessionStart инжектит в контекст."""
    res = _find_project(name)
    if res is None:
        return jsonify(error=f"project not found: {name}"), 404
    try:
        inj = compute_injection(res.vault_root, res.name, limit=res.context_limit)
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=str(exc)), 500
    return jsonify(
        project=res.name,
        vault_root=str(res.vault_root).replace("\\", "/"),
        raw_size=inj["raw_size"],
        effective_size=inj["effective_size"],
        limit=inj["limit"],
        warn_threshold=inj["warn_threshold"],
        status=inj["status"],
        truncated=inj["truncated"],
        preview=inj["preview"],
        raw=inj["raw"],
    )


@app.get("/api/chat/preview")
def api_chat_preview():
    path = request.args.get("path") or ""
    if not path:
        return jsonify(error="path required"), 400
    p = Path(path)
    if not p.exists() or not p.is_file():
        return jsonify(error="not found"), 404
    # Безопасность: разрешаем только файлы внутри vault_base
    try:
        p.resolve().relative_to(VAULT_BASE.resolve())
    except (ValueError, OSError):
        return jsonify(error="outside vault"), 403
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as exc:
        return jsonify(error=f"read failed: {exc}"), 500
    truncated = len(content) > CHAT_PREVIEW_CHARS
    preview = content[:CHAT_PREVIEW_CHARS]
    return jsonify(
        preview=preview,
        truncated=truncated,
        total_chars=len(content),
    )


@app.patch("/api/settings/<path:project>")
def api_settings_patch(project: str):
    payload = request.get_json(silent=True) or {}
    updates: dict = {}
    if "auto_ingest" in payload:
        updates["auto_ingest"] = bool(payload["auto_ingest"])
    if "lint_schedule" in payload:
        val = payload["lint_schedule"]
        if val is None or val == "":
            updates["lint_schedule"] = None
        else:
            try:
                CronTrigger.from_crontab(val)
            except ValueError as e:
                return jsonify(error=f"invalid cron: {e}"), 400
            updates["lint_schedule"] = val
    if "cwd_patterns" in payload:
        patterns = payload["cwd_patterns"]
        if not isinstance(patterns, list):
            return jsonify(error="cwd_patterns must be list"), 400
        updates["cwd_patterns"] = [str(p).strip() for p in patterns if str(p).strip()]
    if "vault_root" in payload:
        vr = str(payload["vault_root"]).strip()
        if not vr:
            return jsonify(error="vault_root cannot be empty"), 400
        # Симметрично api_projects_create — путь должен быть абсолютным,
        # иначе через PATCH можно подменить vault_root на '../../etc'.
        try:
            vp = Path(vr)
            if not vp.is_absolute():
                return jsonify(error="vault_root must be absolute path"), 400
            vr = str(vp).replace("\\", "/")
        except (ValueError, OSError) as exc:
            return jsonify(error=f"invalid vault_root: {exc}"), 400
        updates["vault_root"] = vr
    if "context_limit" in payload:
        from lib.context_injection import ALLOWED_CONTEXT_LIMITS
        val = payload["context_limit"]
        if val is None or val == "" or val == 0:
            updates["context_limit"] = None  # сброс к дефолту (10k)
        else:
            try:
                n = int(val)
            except (ValueError, TypeError):
                return jsonify(error="context_limit must be integer"), 400
            if n not in ALLOWED_CONTEXT_LIMITS:
                return jsonify(
                    error=f"context_limit must be one of {list(ALLOWED_CONTEXT_LIMITS)}"
                ), 400
            updates["context_limit"] = n

    if not updates:
        return jsonify(error="no valid updates"), 400

    ok = update_project_settings(project, updates)
    if not ok:
        return jsonify(error=f"project not found: {project}"), 404

    # Если поменяли lint_schedule — перерегистрируем в scheduler
    if "lint_schedule" in updates:
        register_scheduled_lints()

    # Возвращаем актуальное состояние
    updated = next((p for p in list_projects() if p.name == project), None)
    return jsonify(
        ok=True,
        project=_project_stats(updated) if updated else None,
        schedules=current_schedules(),
    )


# =====================================================================
#  Wiki utils (для страницы проекта и графа)
# =====================================================================


def _parse_frontmatter(content: str) -> dict:
    """Мини-парсер YAML frontmatter без внешних зависимостей."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}
    result: dict = {}
    current_list_key: Optional[str] = None
    for line in m.group(1).split("\n"):
        if not line.strip():
            current_list_key = None
            continue
        if line.startswith((" ", "\t")) and current_list_key:
            stripped = line.strip()
            if stripped.startswith("- "):
                result[current_list_key].append(stripped[2:].strip().strip("'\""))
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                result[key] = []
                current_list_key = key
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                items = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()] if inner else []
                result[key] = items
                current_list_key = None
            else:
                result[key] = val.strip("'\"")
                current_list_key = None
    return result


def _extract_wikilinks(content: str) -> list[str]:
    """Извлекает wikilinks, игнорируя те что внутри code-блоков."""
    stripped = FENCED_CODE_RE.sub("", content)
    stripped = INLINE_CODE_RE.sub("", stripped)
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(stripped)]


def _gather_wiki_pages(vault_root: Path) -> list[dict]:
    """Собирает все md-файлы в wiki/ с frontmatter."""
    wiki_dir = vault_root / "wiki"
    if not wiki_dir.exists():
        return []
    result: list[dict] = []
    for p in sorted(wiki_dir.rglob("*.md")):
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(content)
        body = FRONTMATTER_RE.sub("", content, count=1)
        rel = p.relative_to(vault_root)
        stem_path = str(rel.with_suffix("")).replace("\\", "/")
        result.append({
            "path": str(p).replace("\\", "/"),
            "rel_path": str(rel).replace("\\", "/"),
            "stem_path": stem_path,
            "stem": p.stem,
            "name": fm.get("name") or p.stem,
            "type": fm.get("type") or "overview",
            "created": fm.get("created") or "",
            "updated": fm.get("updated") or "",
            "tags": fm.get("tags") if isinstance(fm.get("tags"), list) else [],
            "sources": fm.get("sources") if isinstance(fm.get("sources"), list) else [],
            "words": len(body.split()),
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return result


def _gather_chats(vault_root: Path) -> list[dict]:
    chats_dir = vault_root / "raw" / "chats"
    if not chats_dir.exists():
        return []

    # Собираем set успешно проингестенных путей (для бейджа «обработан»)
    ingested_paths: set[str] = set()
    for j in load_jobs():
        if j.get("type") == "ingest" and j.get("status") == "done":
            src = (j.get("source") or "").replace("\\", "/")
            if src:
                ingested_paths.add(src)

    result: list[dict] = []
    for p in sorted(chats_dir.glob("*.md")):
        try:
            stat = p.stat()
        except OSError:
            continue
        path_norm = str(p).replace("\\", "/")
        result.append({
            "path": path_norm,
            "name": p.name,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "ingested": path_norm in ingested_paths,
        })
    return result


def _build_graph(wiki_pages: list[dict], vault_root: Path) -> dict:
    """Строит граф связей для Cytoscape.js.

    nodes = страницы wiki/
    edges = wikilinks между ними (резолвятся по stem_path или stem)
    """
    # Строим индекс для резолюции ссылок
    link_index: dict[str, dict] = {}
    for page in wiki_pages:
        sp = page["stem_path"]
        link_index[sp] = page
        if sp.startswith("wiki/"):
            link_index[sp[5:]] = page
        link_index[page["stem"]] = page

    # Собираем edges
    edges: list[dict] = []
    seen = set()
    for page in wiki_pages:
        try:
            content = (vault_root / page["rel_path"]).read_text(encoding="utf-8")
        except OSError:
            continue
        for link in _extract_wikilinks(content):
            link_norm = link.strip()
            if link_norm.endswith(".md"):
                link_norm = link_norm[:-3]
            target = link_index.get(link_norm) or link_index.get(link_norm.replace("\\", "/"))
            if target is None or target["stem"] == page["stem"]:
                continue
            key = (page["stem"], target["stem"])
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "data": {
                    "id": f"{page['stem']}->{target['stem']}",
                    "source": page["stem"],
                    "target": target["stem"],
                },
            })

    nodes = [
        {
            "data": {
                "id": p["stem"],
                "label": p["name"],
                "type": p["type"],
                "path": p["rel_path"],
            },
        }
        for p in wiki_pages
        if not p["rel_path"].startswith("wiki/lint-reports")
    ]
    # Фильтр edges: убираем рёбра, у которых source/target попали в исключённые
    allowed = {n["data"]["id"] for n in nodes}
    edges = [e for e in edges if e["data"]["source"] in allowed and e["data"]["target"] in allowed]

    return {"nodes": nodes, "edges": edges}


def _find_project(name: str) -> Optional[ProjectResolution]:
    for p in list_projects():
        if p.name == name:
            return p
    return None


# =====================================================================
#  Entry point
# =====================================================================


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    port = 5757
    host = "127.0.0.1"

    # Запускаем scheduler до Flask
    scheduler.start()
    register_scheduled_lints()
    print(f"Scheduler started, jobs: {len(scheduler.get_jobs())}")

    if not any(arg in sys.argv for arg in ("--no-browser", "--no-open")):
        def open_browser():
            time.sleep(1.2)
            try:
                webbrowser.open(f"http://{host}:{port}")
            except Exception:
                pass
        threading.Thread(target=open_browser, daemon=True).start()

    print(f"LLM Wiki Control Panel → http://{host}:{port}")
    print(f"  shared: {SHARED_ROOT}")
    print(f"  vault base: {VAULT_BASE}")
    try:
        # threaded=True позволяет параллельную обработку запросов.
        # До этого Werkzeug single-thread — 180 параллельных GET выстраивались в
        # очередь по ~2 сек каждый. Все shared state (jobs.json, active-sessions,
        # DEDUP_STATE, _import_progress, _scheduler) уже защищены через
        # threading.Lock + межпроцессный filelock, так что безопасно.
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
