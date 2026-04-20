"""Обёртка над `claude -p` CLI — работает с подпиской, без API-ключа."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator, Optional


DEFAULT_TIMEOUT = 900  # 15 минут по умолчанию


@contextlib.contextmanager
def _hide_user_rules() -> "Iterator[list[tuple[Path, Path]]]":
    """Временно переименовывает файлы в ~/.claude/rules/ (добавляет .llm-wiki-hidden).

    Гарантирует восстановление через try/finally даже при падении subsession.
    Нужно потому что `claudeMdExcludes` через inline --settings не срабатывает —
    это единственный надёжный способ гарантировать что user rules не попадут в
    контекст сабсессии.

    Побочный эффект: на время вызова (обычно 30-120 сек) другие параллельные
    Claude-сессии на этой машине не увидят rules.
    """
    home_env = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    renamed: list[tuple[Path, Path]] = []  # (original, hidden)
    targets: list[Path] = []
    suffix = f".llm-wiki-hidden-{os.getpid()}"

    # Если нет ни USERPROFILE, ни HOME — ничего не прячем (не рискуем задеть
    # случайные .claude/ в cwd через относительный путь).
    if home_env:
        home = Path(home_env)
        rules_dir = home / ".claude" / "rules"
        claude_md = home / ".claude" / "CLAUDE.md"
        if rules_dir.is_dir():
            targets.extend(rules_dir.glob("*.md"))
        if claude_md.is_file():
            targets.append(claude_md)

    for p in targets:
        hidden = p.with_name(p.name + suffix)
        try:
            p.rename(hidden)
            renamed.append((p, hidden))
        except OSError:
            continue

    try:
        yield renamed
    finally:
        failed_restore: list[str] = []
        for original, hidden in renamed:
            try:
                hidden.rename(original)
            except OSError:
                failed_restore.append(str(original))
        if failed_restore:
            # Не можем использовать lib.session_dump.log (циклический импорт);
            # пишем напрямую в stderr — wrapper/job_thread подхватят.
            import sys as _sys
            _sys.stderr.write(
                "⚠ _hide_user_rules: не удалось восстановить "
                f"{len(failed_restore)} файл(ов) правил: "
                + ", ".join(failed_restore[:3])
                + (" …" if len(failed_restore) > 3 else "")
                + f"\n    Найди файлы с суффиксом .llm-wiki-hidden-{os.getpid()} "
                "и переименуй обратно вручную.\n"
            )
            _sys.stderr.flush()


def _find_claude() -> str:
    """Находит исполняемый claude с учётом Windows .cmd/.bat обёрток."""
    # на Windows shutil.which найдёт .cmd по PATHEXT
    found = shutil.which("claude")
    if found:
        return found
    # fallback: npm global bin
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.bat",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
    return "claude"  # пусть упадёт с понятной ошибкой


def run_claude(
    prompt: str,
    cwd: Optional[Path] = None,
    permission_mode: str = "acceptEdits",
    timeout: int = DEFAULT_TIMEOUT,
    additional_dirs: Optional[list[Path]] = None,
    output_format: str = "text",
    model: Optional[str] = None,
    append_system_prompt: Optional[str] = None,
    system_prompt: Optional[str] = None,
    dangerously_skip_permissions: bool = False,
    exclude_user_claude_md: bool = False,
    setting_sources: Optional[str] = None,
) -> dict:
    """Вызывает `claude -p <prompt>` с нужными флагами.

    Args:
        prompt: текст промпта (передаётся как позиционный аргумент claude)
        cwd: рабочая директория запуска (влияет на CLAUDE.md подхват)
        permission_mode: "acceptEdits" | "bypassPermissions" | "default" | ...
        timeout: секунд; при превышении — TimeoutExpired
        additional_dirs: доп. директории для --add-dir
        output_format: "text" | "json" | "stream-json"
        model: например "sonnet"|"opus"|"haiku" или полный ID

    Returns:
        dict: {success, returncode, stdout, stderr, timed_out}
    """
    claude_bin = _find_claude()
    # Промпт передаём через stdin, не как позиционный аргумент — иначе Windows CMD
    # обрезает многострочный промпт на первом LF.
    args: list[str] = [
        claude_bin,
        "-p",
        "--permission-mode", permission_mode,
        "--output-format", output_format,
    ]
    for d in additional_dirs or ():
        args.extend(["--add-dir", str(d)])
    if model:
        args.extend(["--model", model])
    if append_system_prompt:
        args.extend(["--append-system-prompt", append_system_prompt])
    if system_prompt:
        args.extend(["--system-prompt", system_prompt])
    if dangerously_skip_permissions:
        args.append("--dangerously-skip-permissions")
    if setting_sources:
        args.extend(["--setting-sources", setting_sources])

    # skipDangerousModePermissionPrompt через inline settings — помогает при bypass
    if dangerously_skip_permissions:
        args.extend([
            "--settings",
            json.dumps({"skipDangerousModePermissionPrompt": True}, ensure_ascii=False),
        ])

    # Маркер, что это сабсессия — хуки не должны её обрабатывать (инжект / дамп)
    env = os.environ.copy()
    env["LLM_WIKI_SUBSESSION"] = "1"

    if os.environ.get("LLM_WIKI_DEBUG") == "1":
        import sys
        sys.stderr.write("=== claude args ===\n")
        for i, a in enumerate(args):
            preview = a if len(a) < 200 else a[:200] + f"... ({len(a)} chars)"
            sys.stderr.write(f"  [{i}] {preview}\n")
        sys.stderr.write("===================\n")
        sys.stderr.flush()

    # Если просят — физически скрываем user CLAUDE.md/rules на время вызова.
    # Это единственный надёжный способ избежать их подхвата в non-interactive режиме.
    ctx = _hide_user_rules() if exclude_user_claude_md else contextlib.nullcontext()

    # Используем Popen вручную вместо subprocess.run(timeout=...), чтобы при
    # таймауте ГАРАНТИРОВАННО убить всё дерево процессов (claude запускает
    # node.exe и т.д., которые на Windows переживают обычный kill).
    from lib.jobs import kill_proc_tree, popen_posix_group_flags  # локальный импорт — избегаем цикла
    proc = None
    try:
        with ctx:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(cwd) if cwd else None,
                env=env,
                **popen_posix_group_flags(),
            )
            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
                return {
                    "success": proc.returncode == 0,
                    "returncode": proc.returncode,
                    "stdout": stdout or "",
                    "stderr": stderr or "",
                    "timed_out": False,
                }
            except subprocess.TimeoutExpired:
                kill_proc_tree(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
    except subprocess.TimeoutExpired as e:
        stdout = ""
        stderr = f"Timeout после {timeout} секунд (дерево процессов убито)"
        if e.stdout:
            try:
                stdout = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else e.stdout
            except Exception:
                pass
        if e.stderr:
            try:
                extra = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else e.stderr
                stderr += "\n" + (extra or "")
            except Exception:
                pass
        return {
            "success": False,
            "returncode": -1,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
        }


def render_template(template: str, replacements: dict[str, str]) -> str:
    """Простая замена плейсхолдеров %%NAME%% → value (надёжнее str.format для markdown)."""
    out = template
    for key, value in replacements.items():
        out = out.replace(f"%%{key}%%", value)
    return out
