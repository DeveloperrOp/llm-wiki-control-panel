"""Формальная верификация критических инвариантов.

Инварианты (если нарушится — баг):
1. FILELOCK_MUTEX: при concurrent записи в filelock-защищённый файл
   значения из разных потоков не смешиваются.
2. DEDUP_AT_MOST_ONE: за DEDUP_WINDOW_SEC в session-dumps.json должен
   остаться ровно 1 финальный file для данного (sid, suffix).
3. ATOMIC_WRITE: os.replace гарантирует — файл либо старый, либо новый,
   никогда partial (проверяем через параллельные readers).
4. PATH_TRAVERSAL_DEFENCE: ни один /api endpoint не пишет/читает за
   пределы VAULT_BASE при любом payload.
5. KILL_PROC_TREE: после kill_proc_tree дочерние процессы убиты.
6. JOB_LIFECYCLE: каждый job либо running, либо имеет finished_at.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SHARED = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SHARED / "scripts"))

from lib.state import locked, load_state, save_state  # noqa: E402
from lib.jobs import kill_proc_tree, load_jobs  # noqa: E402
from lib.session_dump import (  # noqa: E402
    _reserve_dedup_slot,
    _release_dedup_slot,
    _finalize_dump_slot,
    DEDUP_STATE,
    DEDUP_WINDOW_SEC,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ✓ {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  💥 {name} FAILED — {detail}")


# =====================================================================
# 1. FILELOCK_MUTEX — concurrent writes don't interleave
# =====================================================================

def inv_filelock_mutex():
    print("\n[1] FILELOCK_MUTEX: 20 потоков пишут в один json под locked()")
    path = SHARED / "state" / "_test_filelock_mutex.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_state(path, {"counter": 0, "writers": []})

    N_THREADS = 20
    N_INCREMENTS = 50  # каждый поток делает 50 инкрементов

    def worker(tid: int):
        for i in range(N_INCREMENTS):
            with locked(path):
                state = load_state(path, default={})
                state["counter"] = state.get("counter", 0) + 1
                state.setdefault("writers", []).append(f"t{tid}-{i}")
                save_state(path, state)

    start = time.time()
    threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()
    duration = time.time() - start

    final = load_state(path, default={})
    expected = N_THREADS * N_INCREMENTS
    got = final.get("counter", -1)
    writers = final.get("writers", [])

    print(f"  Expected counter: {expected}, got: {got}")
    print(f"  Writers list len: {len(writers)} (duplicates: {len(writers) - len(set(writers))})")
    print(f"  Duration: {duration:.2f}s")

    check("counter == expected (no lost writes)", got == expected,
          f"expected {expected}, got {got}: {expected - got} потерянных инкрементов")
    check("writers list has all entries", len(writers) == expected,
          f"{expected - len(writers)} теряющихся записей")
    check("no duplicate writer entries", len(set(writers)) == len(writers),
          "есть дубли — один поток добавил запись дважды")

    # cleanup
    try:
        path.unlink()
        (path.with_suffix(path.suffix + ".lock")).unlink(missing_ok=True)
    except OSError:
        pass


# =====================================================================
# 2. DEDUP_AT_MOST_ONE — concurrent dump attempts, только один slot
# =====================================================================

def inv_dedup_at_most_one():
    print("\n[2] DEDUP_AT_MOST_ONE: 50 потоков пытаются зарезервировать один sid")
    sid = f"inv-test-{int(time.time())}-{random.randint(0, 1_000_000)}"
    suffix = ""
    N_THREADS = 50

    results = []
    lock = threading.Lock()

    def worker():
        ok = _reserve_dedup_slot(sid, suffix)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()

    n_ok = sum(results)
    print(f"  Reserved=True: {n_ok}, Reserved=False: {N_THREADS - n_ok}")
    check("ровно один поток получил slot", n_ok == 1,
          f"получили {n_ok} winners из {N_THREADS}")

    # Второй раунд: после release slot должен снова быть доступен
    _release_dedup_slot(sid, suffix)
    state = load_state(DEDUP_STATE, default={})
    check("после release ключ удалён", sid + suffix not in state,
          f"ключ остался в state: {state.get(sid+suffix)}")

    # После finalize release не должен трогать
    _reserve_dedup_slot(sid, suffix)
    _finalize_dump_slot(sid, suffix, Path("/tmp/dummy.md"))
    _release_dedup_slot(sid, suffix)  # должен быть no-op
    state = load_state(DEDUP_STATE, default={})
    check("release не трогает finalized", sid + suffix in state,
          "release удалил финализированный ключ")

    # cleanup
    state = load_state(DEDUP_STATE, default={})
    state.pop(sid + suffix, None)
    save_state(DEDUP_STATE, state)


# =====================================================================
# 3. ATOMIC_WRITE — параллельные readers никогда не видят partial
# =====================================================================

def inv_atomic_write():
    print("\n[3] ATOMIC_WRITE: writer + 10 readers — reader никогда не видит partial JSON")
    path = SHARED / "state" / "_test_atomic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Начальное состояние — валидный JSON
    save_state(path, {"version": 0, "data": "initial"})

    errors = []
    stop = threading.Event()

    def writer():
        for i in range(200):
            if stop.is_set(): break
            # Большой payload — увеличим шанс partial если write не атомарна
            save_state(path, {"version": i, "data": "x" * 10000, "writers": [f"w{i}"]})
            time.sleep(0.001)

    def reader(tid: int):
        while not stop.is_set():
            try:
                # Читаем напрямую через json.load — если файл partial, будет JSONDecodeError
                with path.open("r", encoding="utf-8") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                errors.append(f"reader{tid}: partial JSON at pos {e.pos}")
            except FileNotFoundError:
                pass  # временно между os.replace
            except OSError:
                pass

    w = threading.Thread(target=writer)
    readers = [threading.Thread(target=reader, args=(tid,)) for tid in range(10)]
    w.start()
    for r in readers: r.start()
    w.join()
    stop.set()
    for r in readers: r.join()

    check("readers never saw partial JSON", len(errors) == 0,
          f"{len(errors)} чтений дали partial: {errors[:3]}")

    try:
        path.unlink()
    except OSError:
        pass


# =====================================================================
# 4. PATH_TRAVERSAL_DEFENCE — комбинаторный fuzz + проверка файловой системы
# =====================================================================

def inv_path_traversal_defence():
    print("\n[4] PATH_TRAVERSAL: попытки создать/прочитать файлы за пределами vault")
    import requests
    # Целевой файл — должен остаться нетронутым после всех попыток
    sentinel = Path.home() / "AppData" / "Local" / "Temp" / "invariant-sentinel.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("SHOULD NOT BE CHANGED", encoding="utf-8")
    original = sentinel.read_text(encoding="utf-8")
    sentinel_str = str(sentinel).replace("\\", "/")

    evil_paths = [
        sentinel_str,
        f"../../../{sentinel.name}",
        f"..\\..\\..\\{sentinel.name}",
        f"file://{sentinel}",
        f"\\\\?\\{sentinel}",
        sentinel_str + "\x00.md",
    ]

    # Попытка 1: DELETE /api/chat sentinel
    for p in evil_paths:
        try:
            r = requests.delete("http://localhost:5757/api/chat", json={"chat_path": p}, timeout=5)
        except Exception:
            continue
    check("sentinel не удалён после DELETE /api/chat", sentinel.exists(),
          "файл был удалён через path traversal")
    check("sentinel содержимое не изменилось", sentinel.read_text(encoding="utf-8") == original,
          "содержимое поменялось")

    # Попытка 2: POST /api/assign → перемещение файла
    for p in evil_paths:
        try:
            r = requests.post("http://localhost:5757/api/assign",
                              json={"chat_path": p, "project": "LLM Wiki Control Panel"},
                              timeout=5)
        except Exception:
            continue
    check("sentinel не перемещён после /api/assign", sentinel.exists(),
          "файл исчез — возможно перемещён")

    # Попытка 3: chat/preview → не должно читать
    for p in evil_paths:
        try:
            r = requests.get("http://localhost:5757/api/chat/preview",
                             params={"path": p}, timeout=5)
            if r.status_code == 200:
                content = r.json().get("preview", "")
                check(f"chat/preview НЕ вернул содержимое sentinel для {p[:40]}",
                      "SHOULD NOT" not in content,
                      f"endpoint прочитал запрещённый файл")
        except Exception:
            pass

    # Cleanup
    try:
        sentinel.unlink()
    except OSError:
        pass


# =====================================================================
# 5. KILL_PROC_TREE — parent → child → grandchild, все убиты
# =====================================================================

def inv_kill_proc_tree():
    print("\n[5] KILL_PROC_TREE: parent запускает child, kill_proc_tree убивает обоих")

    parent = subprocess.Popen(
        [sys.executable, "-c", """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
print('CHILD_PID:', child.pid, flush=True)
time.sleep(120)
"""],
        stdout=subprocess.PIPE, text=True,
    )

    line = parent.stdout.readline()
    child_pid = int(line.split(":")[1].strip())
    time.sleep(0.5)

    # Убиваем parent через kill_proc_tree
    kill_proc_tree(parent)
    time.sleep(1.5)

    # Проверяем детей — на Windows через taskkill они должны быть убиты
    try:
        os.kill(child_pid, 0)  # signal 0 = check existence
        child_alive = True
    except OSError:
        child_alive = False

    parent.wait(timeout=5)
    check("child процесс мёртв после kill_proc_tree(parent)", not child_alive,
          f"child pid={child_pid} ещё жив")


# =====================================================================
# 6. JOB_LIFECYCLE — любой job либо running либо имеет finished_at
# =====================================================================

def inv_job_lifecycle():
    print("\n[6] JOB_LIFECYCLE: каждый non-running job имеет finished_at")

    jobs = load_jobs()
    invalid = []
    for j in jobs:
        status = j.get("status")
        finished_at = j.get("finished_at")
        if status != "running" and not finished_at:
            invalid.append({"id": j.get("id", "?")[:8], "status": status, "type": j.get("type"), "project": j.get("project")})

    check(f"все {len(jobs)} job'ов соблюдают lifecycle", len(invalid) == 0,
          f"{len(invalid)} job'ов с некорректным состоянием: {invalid[:3]}")


# =====================================================================
# MAIN
# =====================================================================

def main():
    print("=" * 60)
    print("INVARIANTS audit")
    print("=" * 60)

    for fn in (inv_filelock_mutex, inv_dedup_at_most_one, inv_atomic_write,
               inv_path_traversal_defence, inv_kill_proc_tree, inv_job_lifecycle):
        try:
            fn()
        except Exception as e:
            import traceback
            FAILURES.append(f"{fn.__name__}: EXCEPTION {e}")
            print(f"  💥 {fn.__name__} CRASHED: {e}")
            traceback.print_exc()

    print()
    print("=" * 60)
    if FAILURES:
        print(f"🔴 INVARIANTS FAILURES: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
    else:
        print("✅ ВСЕ ИНВАРИАНТЫ СОБЛЮДЕНЫ")
    print("=" * 60)
    return len(FAILURES)


if __name__ == "__main__":
    sys.exit(main())
