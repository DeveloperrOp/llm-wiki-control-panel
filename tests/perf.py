"""Performance audit.

Генерируем синтетический большой vault и замеряем время ключевых операций.
Без реального Claude CLI — только то что можно измерить локально.
"""

from __future__ import annotations

import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

BASE = "http://localhost:5757"

SHARED = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SHARED / "scripts"))

from lib.transcript import format_session, iter_events  # noqa: E402

RESULTS: dict = {}


def time_it(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - start, result


# =====================================================================
# 1. Синтетический vault
# =====================================================================

def generate_synthetic_vault(root: Path, n_pages: int = 500, n_chats: int = 100):
    """Создаёт fake vault: n_pages wiki-страниц + n_chats чатов."""
    print(f"\n[gen] Создаю синтетический vault: {n_pages} pages, {n_chats} chats")
    (root / "wiki" / "entities").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "concepts").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "sources").mkdir(parents=True, exist_ok=True)
    (root / "raw" / "chats").mkdir(parents=True, exist_ok=True)

    # Генерим wiki-страницы с wikilink'ами друг на друга
    n_per = n_pages // 3
    for i in range(n_per):
        body = f"---\ntype: entity\nupdated: 2026-04-20\n---\n\n# Entity {i}\n\nRefs: [[concept-{i % n_per}]] [[source-{i % n_per}]]\n" + ("text " * 100)
        (root / "wiki" / "entities" / f"entity-{i}.md").write_text(body, encoding="utf-8")
    for i in range(n_per):
        body = f"---\ntype: concept\nupdated: 2026-04-20\n---\n\n# Concept {i}\n\n[[entity-{i}]]\n" + ("lorem " * 100)
        (root / "wiki" / "concepts" / f"concept-{i}.md").write_text(body, encoding="utf-8")
    for i in range(n_per):
        body = f"---\ntype: source\nsources: [raw/chats/chat-{i}.md]\nupdated: 2026-04-20\n---\n\n# Source {i}\n\n[[entity-{i}]] [[concept-{i}]]\n"
        (root / "wiki" / "sources" / f"source-{i}.md").write_text(body, encoding="utf-8")

    # index.md
    idx = ["# Index\n\n## Сущности\n\n"]
    for i in range(n_per):
        idx.append(f"- [[wiki/entities/entity-{i}]] — entity {i}\n")
    idx.append("\n## Концепции\n\n")
    for i in range(n_per):
        idx.append(f"- [[wiki/concepts/concept-{i}]] — concept {i}\n")
    idx.append("\n## Источники\n\n")
    for i in range(n_per):
        idx.append(f"- [[wiki/sources/source-{i}]] — source {i}\n")
    (root / "index.md").write_text("".join(idx), encoding="utf-8")

    # log.md с N записями
    log = []
    for i in range(n_chats):
        log.append(f"## [2026-04-{(i % 28) + 1:02d} 10:00] ingest | chat-{i}\n\n- Прочитан: raw/chats/chat-{i}.md\n- Создан: wiki/sources/source-{i % n_per}.md\n\n")
    (root / "log.md").write_text("".join(log), encoding="utf-8")

    # raw/chats
    for i in range(n_chats):
        (root / "raw" / "chats" / f"chat-{i}.md").write_text(f"# Chat {i}\n\n" + ("User: hi\nAssistant: ok\n" * 50), encoding="utf-8")


# =====================================================================
# 2. Lint на большом vault
# =====================================================================

def bench_lint(vault: Path):
    print(f"\n[lint] Запускаю lint на vault с {len(list(vault.rglob('*.md')))} .md файлами")
    # Вызываем lint.py напрямую как subprocess
    import subprocess
    start = time.perf_counter()
    # Нужен zarregistry project. Временно добавим в project-map.json.
    # Проще — прогнать lint_structural напрямую через Python.
    sys.path.insert(0, str(SHARED / "scripts"))
    from lint import lint_structural, format_report
    start2 = time.perf_counter()
    report = lint_structural(vault)
    elapsed_logic = time.perf_counter() - start2
    _ = format_report(report, vault)
    elapsed = time.perf_counter() - start
    print(f"  lint_structural: {elapsed_logic:.3f}s")
    print(f"  всего (включая format_report): {elapsed:.3f}s")
    print(f"  broken links: {len(report['broken_links'])}, orphans: {len(report['orphans'])}, sparse: {len(report['sparse'])}")
    RESULTS["lint_structural_500pages"] = {
        "seconds": round(elapsed_logic, 3),
        "total_seconds": round(elapsed, 3),
        "pages": len(list((vault / "wiki").rglob("*.md"))),
    }


# =====================================================================
# 3. transcript.py на гигантском JSONL
# =====================================================================

def bench_transcript_large():
    print("\n[transcript] Генерирую JSONL 50MB и парсим")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        path = Path(f.name)
        # каждая строка ~500 байт, 100k строк = ~50MB
        for i in range(100_000):
            ev = {
                "type": "user" if i % 2 == 0 else "assistant",
                "message": {"role": "user", "content": f"Message {i} " + ("x" * 400)},
                "timestamp": "2026-04-20T10:00:00Z",
                "sessionId": "perf-test",
                "cwd": "d:/test",
                "uuid": f"u-{i}",
            }
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"  JSONL size: {size_mb:.1f} MB, 100k events")

    # Measure iter_events throughput
    start = time.perf_counter()
    count = 0
    for ev in iter_events(path):
        count += 1
    elapsed_iter = time.perf_counter() - start
    print(f"  iter_events (1 проход): {elapsed_iter:.2f}s, {count} events ({count/elapsed_iter:.0f} ev/sec)")

    # format_session — 2 прохода
    start = time.perf_counter()
    md = format_session(path)
    elapsed_fmt = time.perf_counter() - start
    print(f"  format_session (2 прохода): {elapsed_fmt:.2f}s, {len(md)/1024:.1f} KB markdown")

    path.unlink()
    RESULTS["transcript_50mb"] = {
        "size_mb": round(size_mb, 1),
        "events": count,
        "iter_events_sec": round(elapsed_iter, 2),
        "format_session_sec": round(elapsed_fmt, 2),
    }


# =====================================================================
# 4. HTTP endpoints под нагрузкой
# =====================================================================

def bench_endpoints_concurrent():
    print("\n[http] Concurrent load на ключевые GET endpoints")
    endpoints = [
        "/api/projects", "/api/jobs", "/api/schedules",
        "/api/system-status", "/api/today-stats",
        "/api/active-sessions", "/api/import-progress",
        "/api/unassigned", "/api/raw-map",
    ]
    per_endpoint = 20
    results_per: dict[str, list[float]] = {}
    lock = threading.Lock()

    def worker(ep):
        times = []
        for _ in range(per_endpoint):
            start = time.perf_counter()
            try:
                requests.get(BASE + ep, timeout=10)
                times.append(time.perf_counter() - start)
            except Exception:
                pass
        with lock:
            results_per[ep] = times

    threads = [threading.Thread(target=worker, args=(ep,)) for ep in endpoints]
    start = time.perf_counter()
    for t in threads: t.start()
    for t in threads: t.join()
    total = time.perf_counter() - start
    print(f"  Общий wall clock: {total:.2f}s ({len(endpoints) * per_endpoint} requests parallel)")
    print(f"\n  | endpoint                        | mean ms | p95 ms | max ms |")
    print(f"  |---------------------------------|---------|--------|--------|")
    for ep, times in sorted(results_per.items()):
        if not times: continue
        mean = statistics.mean(times) * 1000
        p95 = statistics.quantiles(times, n=20)[-1] * 1000 if len(times) > 1 else times[0] * 1000
        mx = max(times) * 1000
        print(f"  | {ep:<31} | {mean:7.1f} | {p95:6.1f} | {mx:6.1f} |")
        RESULTS.setdefault("endpoint_perf_ms", {})[ep] = {"mean": round(mean, 1), "p95": round(p95, 1), "max": round(mx, 1)}


# =====================================================================
# 5. Memory usage
# =====================================================================

def bench_memory():
    print("\n[memory] dashboard resident memory")
    try:
        import psutil
    except ImportError:
        subprocess_install = __import__("subprocess").run
        subprocess_install([sys.executable, "-m", "pip", "install", "psutil"], capture_output=True)
        import psutil
    # Найдём процесс dashboard.py
    for p in psutil.process_iter(["pid", "cmdline", "memory_info"]):
        cmd = p.info.get("cmdline") or []
        if any("dashboard.py" in c for c in cmd):
            mem = p.info["memory_info"].rss / 1024 / 1024
            print(f"  dashboard PID={p.info['pid']}: {mem:.1f} MB RSS")
            RESULTS["dashboard_memory_mb"] = round(mem, 1)
            return
    print("  dashboard не найден в процессах")


# =====================================================================
# MAIN
# =====================================================================

def main():
    print("=" * 60)
    print("PERFORMANCE audit")
    print("=" * 60)

    # 1. Vault + lint
    with tempfile.TemporaryDirectory(prefix="perf-vault-") as tmp:
        vault = Path(tmp)
        t, _ = time_it(generate_synthetic_vault, vault, n_pages=500, n_chats=100)
        print(f"[gen] сгенерирован vault за {t:.1f}s")
        RESULTS["synth_vault_gen_sec"] = round(t, 1)

        bench_lint(vault)

    # 2. transcript large
    bench_transcript_large()

    # 3. HTTP
    bench_endpoints_concurrent()

    # 4. Memory
    bench_memory()

    print()
    print("=" * 60)
    print("RESULTS JSON:")
    print(json.dumps(RESULTS, indent=2, ensure_ascii=False))
    out = Path(__file__).parent / "perf-results.json"
    out.write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
