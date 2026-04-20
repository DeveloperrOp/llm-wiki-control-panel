"""Lint-скрипт для LLM Wiki: 6 структурных проверок + опциональная 7-я семантическая.

Использование:
    python lint.py "<project_name>"                  # базовые 6 проверок
    python lint.py "<project_name>" --semantic       # + семантическая через claude -p
    python lint.py "<project_name>" --save           # + сохранить в wiki/lint-reports/
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent
sys.path.insert(0, str(HERE))

from lib.mapping import list_projects, ProjectResolution  # noqa: E402
from lib.runner import run_claude, render_template  # noqa: E402

PROMPT_FILE = SHARED / "prompts" / "lint-semantic-ru.md"

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FENCED_CODE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
SPARSE_WORD_LIMIT = 200
EXCLUDE_STEMS = {"overview", "index", "log"}
# Папки, файлы из которых не считаются orphan / sparse (автогенерируемые отчёты)
EXCLUDE_DIRS = {"lint-reports"}


def find_project(name: str) -> ProjectResolution:
    for p in list_projects():
        if p.name.lower() == name.lower():
            return p
    raise SystemExit(f"Проект «{name}» не найден")


def parse_frontmatter(content: str) -> dict:
    """Мини-парсер YAML frontmatter без внешних зависимостей.

    Поддерживает: key: value, key: [a, b, c], key:\\n  - a\\n  - b
    """
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}
    result: dict = {}
    current_list_key: str | None = None
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


def extract_wikilinks(content: str) -> list[str]:
    # Вырезаем fenced code blocks и inline code — wikilinks там это примеры синтаксиса,
    # а не реальные ссылки.
    stripped = FENCED_CODE_RE.sub("", content)
    stripped = INLINE_CODE_RE.sub("", stripped)
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(stripped)]


def count_words(text: str) -> int:
    return len(text.split())


def build_page_index(vault_root: Path) -> tuple[list[Path], dict[str, Path]]:
    """Возвращает (список md-файлов в wiki/, map wikilink-cтроки → Path).

    Для коротких алиасов (entities/foo, foo) при коллизии имени между
    wiki/entities/foo.md и wiki/concepts/foo.md — алиас помечается как
    ambiguous и из индекса удаляется. Полный путь wiki/entities/foo
    остаётся рабочим. Это заставляет пользователя явно указывать тип
    при неоднозначных ссылках вместо случайного резолва.
    """
    wiki_dir = vault_root / "wiki"
    files: list[Path] = sorted(wiki_dir.rglob("*.md")) if wiki_dir.exists() else []
    link_index: dict[str, Path] = {}
    ambiguous: set[str] = set()

    def _add_alias(key: str, target: Path) -> None:
        if key in ambiguous:
            return
        existing = link_index.get(key)
        if existing is not None and existing != target:
            ambiguous.add(key)
            link_index.pop(key, None)
        else:
            link_index[key] = target

    for f in files:
        rel = f.relative_to(vault_root)
        path_no_ext = str(rel.with_suffix("")).replace("\\", "/")
        # Полный путь всегда уникален — без ambiguous-проверки.
        link_index[path_no_ext] = f                             # wiki/entities/foo
        if path_no_ext.startswith("wiki/"):
            _add_alias(path_no_ext[5:], f)                      # entities/foo
        _add_alias(f.stem, f)                                   # foo
    return files, link_index


def _rel_key(p: Path, vault_root: Path) -> str:
    """Канонический ключ страницы: путь без расширения, с forward-slash.
    Пример: wiki/entities/foo

    Используется вместо p.stem, чтобы избежать коллизии между
    wiki/entities/foo.md и wiki/concepts/foo.md (обе имели stem='foo').
    """
    return str(p.relative_to(vault_root).with_suffix("")).replace("\\", "/")


def lint_structural(vault_root: Path) -> dict:
    wiki_files, link_index = build_page_index(vault_root)
    raw_dir = vault_root / "raw"

    # Граф ссылок хранится по relative_path (wiki/entities/foo), не по stem.
    # Коллизия stem'ов между entities/foo и concepts/foo раньше сливала их граф.
    forward: dict[str, set[str]] = defaultdict(set)
    backref: dict[str, set[str]] = defaultdict(set)
    broken: list[tuple[Path, str]] = []
    all_source_refs: set[str] = set()
    sparse: list[Path] = []
    stale: list[tuple[Path, str, str, str]] = []
    missing_backlinks: list[tuple[str, str]] = []

    for p in wiki_files:
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue

        fm = parse_frontmatter(content)
        body = FRONTMATTER_RE.sub("", content, count=1)
        p_key = _rel_key(p, vault_root)

        # links
        for link in extract_wikilinks(content):
            link_norm = link.strip()
            if link_norm.endswith(".md"):
                link_norm = link_norm[:-3]
            target = link_index.get(link_norm) or link_index.get(link_norm.replace("\\", "/"))
            if target is None:
                broken.append((p, link))
                continue
            if target == p:
                continue
            t_key = _rel_key(target, vault_root)
            forward[p_key].add(t_key)
            backref[t_key].add(p_key)

        # sparse
        if p.stem not in EXCLUDE_STEMS and count_words(body) < SPARSE_WORD_LIMIT:
            sparse.append(p)

        # sources + stale
        sources = fm.get("sources") or []
        if isinstance(sources, str):
            sources = [sources]
        for s in sources:
            s = (s or "").strip()
            if not s:
                continue
            s_norm = s.replace("\\", "/")
            all_source_refs.add(s_norm)

            updated_str = str(fm.get("updated", "")).strip("'\"")
            if updated_str:
                try:
                    updated_date = datetime.fromisoformat(updated_str)
                except ValueError:
                    updated_date = None
                # Если updated дан как YYYY-MM-DD (без времени) — сравниваем по
                # дате (иначе любой mtime > 00:00 триггерит ложный stale).
                updated_is_date_only = len(updated_str) == 10
                if updated_date:
                    src_path = Path(s) if Path(s).is_absolute() else vault_root / s
                    if src_path.exists():
                        src_mtime = datetime.fromtimestamp(src_path.stat().st_mtime)
                        is_stale = (
                            src_mtime.date() > updated_date.date()
                            if updated_is_date_only
                            else src_mtime > updated_date
                        )
                        if is_stale:
                            stale.append(
                                (p, s_norm,
                                 updated_date.strftime("%Y-%m-%d"),
                                 src_mtime.strftime("%Y-%m-%d %H:%M")),
                            )

    # orphan pages — проверяем backref по rel_key, не stem
    def _in_excluded_dir(p: Path) -> bool:
        return any(part in EXCLUDE_DIRS for part in p.parts)

    orphans = [
        p for p in wiki_files
        if p.stem not in EXCLUDE_STEMS
        and not _in_excluded_dir(p)
        and not backref.get(_rel_key(p, vault_root))
    ]

    # missing backlinks — ключи теперь rel_key (wiki/entities/foo), не stem
    for src_key, targets in forward.items():
        for t_key in targets:
            if src_key not in forward.get(t_key, set()):
                missing_backlinks.append((src_key, t_key))

    # orphan sources: файлы в raw/, не упомянутые ни в одной странице
    orphan_sources: list[Path] = []
    if raw_dir.exists():
        for sp in raw_dir.rglob("*.md"):
            rel = str(sp.relative_to(vault_root)).replace("\\", "/")
            abs_norm = str(sp).replace("\\", "/")
            if rel in all_source_refs or abs_norm in all_source_refs:
                continue
            # также пробуем разные варианты
            if any(rel.endswith(ref) or ref.endswith(rel) for ref in all_source_refs):
                continue
            orphan_sources.append(sp)

    return {
        "wiki_count": len(wiki_files),
        "broken_links": broken,
        "orphans": orphans,
        "stale": stale,
        "missing_backlinks": missing_backlinks,
        "sparse": sparse,
        "orphan_sources": orphan_sources,
    }


def format_report(results: dict, vault_root: Path, semantic: str | None = None) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_issues = (
        len(results["broken_links"])
        + len(results["orphans"])
        + len(results["stale"])
        + len(results["missing_backlinks"])
        + len(results["sparse"])
        + len(results["orphan_sources"])
    )
    out: list[str] = [
        f"# Lint-отчёт вики",
        "",
        f"- **Vault:** `{vault_root}`",
        f"- **Время:** {ts}",
        f"- **Страниц в wiki/:** {results['wiki_count']}",
        f"- **Всего замечаний:** {total_issues}",
        "",
        f"## 1. Битые wikilinks ({len(results['broken_links'])})",
        "",
    ]
    if results["broken_links"]:
        for p, link in results["broken_links"]:
            out.append(f"- `{p.relative_to(vault_root)}` → `[[{link}]]` — цель не найдена")
    else:
        out.append("_нет_")

    out += ["", f"## 2. Orphan pages ({len(results['orphans'])})", ""]
    if results["orphans"]:
        for p in results["orphans"]:
            out.append(f"- `{p.relative_to(vault_root)}` — нет входящих ссылок")
    else:
        out.append("_нет_")

    out += ["", f"## 3. Stale pages ({len(results['stale'])})", ""]
    if results["stale"]:
        for p, src, updated, mtime in results["stale"]:
            out.append(f"- `{p.relative_to(vault_root)}` (updated {updated}) ← источник `{src}` изменён {mtime}")
    else:
        out.append("_нет_")

    out += ["", f"## 4. Missing backlinks ({len(results['missing_backlinks'])})", ""]
    if results["missing_backlinks"]:
        for a, b in results["missing_backlinks"][:50]:
            out.append(f"- `{a}` → `{b}`, обратной ссылки нет")
        if len(results["missing_backlinks"]) > 50:
            out.append(f"- _…и ещё {len(results['missing_backlinks']) - 50}_")
    else:
        out.append("_нет_")

    out += ["", f"## 5. Sparse pages ({len(results['sparse'])}) — < {SPARSE_WORD_LIMIT} слов", ""]
    if results["sparse"]:
        for p in results["sparse"]:
            out.append(f"- `{p.relative_to(vault_root)}`")
    else:
        out.append("_нет_")

    out += ["", f"## 6. Orphan sources ({len(results['orphan_sources'])})", ""]
    if results["orphan_sources"]:
        for sp in results["orphan_sources"]:
            out.append(f"- `{sp.relative_to(vault_root)}` — не упомянут ни в одной wiki-странице")
    else:
        out.append("_нет_")

    if semantic is not None:
        out += ["", "## 7. Семантический анализ (через claude -p)", "", semantic.strip()]

    return "\n".join(out) + "\n"


def save_report(vault_root: Path, report: str) -> Path:
    reports_dir = vault_root / "wiki" / "lint-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    path = reports_dir / f"{ts}.md"
    path.write_text(report, encoding="utf-8")
    return path


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Lint LLM Wiki")
    parser.add_argument("project", help="Имя проекта (из project-map.json)")
    parser.add_argument("--semantic", action="store_true", help="Запустить 7-ю проверку через claude -p")
    parser.add_argument("--save", action="store_true", help="Сохранить отчёт в wiki/lint-reports/")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout для семантической проверки")
    args = parser.parse_args()

    resolution = find_project(args.project)
    print(f"→ Lint проекта «{resolution.name}»")
    print(f"  vault: {resolution.vault_root}")

    results = lint_structural(resolution.vault_root)

    semantic_output: str | None = None
    if args.semantic:
        if not PROMPT_FILE.exists():
            print(f"❌ Промпт не найден: {PROMPT_FILE}")
        else:
            print("  запускаю семантическую проверку через claude -p...")
            prompt_template = PROMPT_FILE.read_text(encoding="utf-8")
            prompt = render_template(
                prompt_template,
                {"VAULT_ROOT": str(resolution.vault_root).replace("\\", "/")},
            )
            r = run_claude(
                prompt=prompt,
                cwd=resolution.vault_root,
                permission_mode="default",  # lint ничего не пишет, только читает
                timeout=args.timeout,
            )
            if r["success"]:
                semantic_output = r["stdout"]
            else:
                semantic_output = f"ERROR: {r['stderr']} (rc={r['returncode']})"

    report = format_report(results, resolution.vault_root, semantic=semantic_output)
    print("─" * 60)
    print(report)
    print("─" * 60)

    if args.save:
        path = save_report(resolution.vault_root, report)
        print(f"Отчёт сохранён: {path}")


if __name__ == "__main__":
    main()
