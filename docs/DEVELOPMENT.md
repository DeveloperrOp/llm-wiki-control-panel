# Development notes

## Repository layout

```
llm-wiki-control-panel/
├── hooks/                     Claude Code hooks (SessionStart / SessionEnd / PreCompact)
│   ├── session-start.py
│   ├── session-end.py
│   └── pre-compact.py
│
├── scripts/
│   ├── dashboard.py           Flask web server (~2000 lines, single file)
│   ├── ingest.py              CLI: run Claude on a source, generate wiki pages
│   ├── lint.py                CLI: structural + semantic checks on the wiki
│   ├── _job_wrapper.py        Runs long subprocess, updates jobs.json on exit
│   └── lib/                   Shared modules
│       ├── active_sessions.py Registry of live sessions (for backfill)
│       ├── backups.py         Per-operation backup snapshots
│       ├── context_injection.py  Build the additionalContext string
│       ├── jobs.py            Jobs tracker, kill_proc_tree, process-group flags
│       ├── mapping.py         cwd → project resolution
│       ├── runner.py          Wrapper around `claude -p`
│       ├── session_dump.py    JSONL transcript → markdown dump, with atomic dedup reserve
│       ├── state.py           Atomic JSON state + cross-process filelock
│       └── transcript.py      Streaming JSONL → markdown parser
│
├── dashboard/
│   ├── templates/             Jinja2 HTML (Alpine.js + TailwindCSS from CDN)
│   └── static/
│
├── prompts/
│   ├── ingest-ru.md           Prompt for ingest subsession
│   ├── lint-semantic-ru.md    Prompt for semantic contradictions check
│   └── optimize-index-ru.md   Prompt for `index.md` / `log.md` reduction
│
├── tests/
│   ├── invariants.py          Formal invariant checks (filelock, dedup, path traversal, etc.)
│   ├── fuzz_api.py            Fuzz-test every HTTP endpoint with evil payloads
│   └── perf.py                Performance benchmarks (synthetic vault, large JSONL, HTTP load)
│
├── config/
│   └── project-map.example.json
│
├── docs/
│   ├── GUIDE.md               User-facing guide (Russian)
│   ├── PRIVACY.md             Privacy / security notes (English)
│   └── DEVELOPMENT.md         This file
│
├── start-dashboard.bat        Windows launcher
├── start-dashboard.command    macOS launcher
├── create-desktop-shortcut.ps1  Windows shortcut creator
├── requirements.txt
├── LICENSE                    MIT
├── README.md
└── .gitignore
```

## Dependencies — minimal and explicit

See [requirements.txt](../requirements.txt). No compiled extensions, no npm build step:

- **Flask** — web server
- **APScheduler** — cron for scheduled lint
- **filelock** — cross-process locks
- `requests` + `psutil` — only used by tests

Frontend is served as-is: Tailwind + Alpine.js + marked + DOMPurify all via CDN.

## Code style

**Minimal:**
- Don't add features, refactor, or introduce abstractions beyond what the task requires
- Three similar lines is better than a premature abstraction
- No half-finished implementations

**Error handling at boundaries only:**
- Validate at the API boundary (JSON payload)
- Trust internal code and framework guarantees
- Don't add defensive exception handlers for scenarios that can't happen

**Comments:**
- Default: no comments. Let identifiers do the work
- Add a comment only when the **why** is non-obvious: a hidden constraint, a subtle invariant, a workaround for a specific bug
- Don't explain what the code does

## Cross-platform conventions

- `if os.name == "nt":` for Windows-specific branches (PowerShell dialog, `taskkill /T /F`, `DETACHED_PROCESS` flags)
- `elif sys.platform == "darwin":` for macOS-specific branches (AppleScript `choose folder`)
- `popen_posix_group_flags()` helper in `scripts/lib/jobs.py` — returns `{}` on Windows, `{"start_new_session": True}` on POSIX
- `kill_proc_tree(proc)` handles both: `taskkill /T` on Windows, `os.killpg(SIGKILL)` on POSIX

**Never remove a Windows branch when adding a POSIX one — add, don't replace.**

## Tests — run them after every change

```bash
# ~10 seconds
python tests/invariants.py

# ~5-10 minutes (1200+ HTTP requests)
python tests/fuzz_api.py

# ~1 minute (synthetic 500-page vault, 50MB JSONL)
python tests/perf.py
```

Invariants should always be 6/6 green. Fuzz should produce zero HTTP 5xx. Perf numbers are informational baselines.

Before a PR:
1. Run `invariants.py` — all green
2. Do a real ingest via the dashboard (create a short test chat → trigger ingest → verify page created → delete test artifacts)
3. If you touched frontend — open each of `/`, `/project/<name>`, `/settings`, `/help` in a browser and check the DevTools console has no errors

## Architecture decisions — key points

**1. Single-user, localhost-only by design.**
No auth layer. `threading.Lock` + `filelock` are enough for one dashboard + one user's Claude Code sessions. Multi-user support would require rewriting state management.

**2. State lives in JSON files, not a database.**
Every file is read-modify-write under `filelock`. Atomic writes via `tempfile + os.replace`. Trade-off: simple to inspect (just open in any editor), harder to query across large datasets. With `MAX_JOBS=200` history cap, file sizes stay small.

**3. Hooks have a 10-second timeout.**
Anything heavier (ingest, import) is spawned as a **detached subprocess** via `_job_wrapper.py` so it survives the hook exit. Dashboard polls `jobs.json` for status.

**4. Subsession guard against infinite loops.**
When the system starts Claude itself (for ingest/lint), it sets `LLM_WIKI_SUBSESSION=1`. Hooks check this and skip — otherwise every ingest would trigger another ingest.

**5. Dedup reserves a slot atomically.**
`_reserve_dedup_slot()` writes a placeholder under `filelock` before Claude starts processing. Error paths call `_release_dedup_slot()` to free the slot immediately (don't wait 5 minutes).

**6. Jobs lifecycle invariant.**
Every job in `jobs.json` is either `running` (started, no `finished_at`) or terminal (`done`/`failed` with `finished_at`). `update_job()` auto-fills `finished_at` on terminal status.

## Adding a new API endpoint

1. Add `@app.get/post/delete(...)` in `scripts/dashboard.py`
2. Validate all payload fields — type, size, range
3. If the endpoint touches paths from user input, add `p.resolve().relative_to(VAULT_BASE.resolve())` check with `except (ValueError, OSError)`
4. Add a test case to `tests/fuzz_api.py` — pass evil payloads (empty strings, nulls, path traversal, unicode, huge values)
5. Run `fuzz_api.py` — should get 400/403/404, never 5xx

## Adding a new invariant

In `tests/invariants.py`:

1. Define `inv_your_check()` function
2. Use concurrent workers if testing thread-safety
3. Add `check(name, cond, detail)` calls
4. Add the function to the list in `main()`

Run it repeatedly (at least 3-5 times) to catch timing-sensitive issues.

## Known limitations and trade-offs

Documented in the regression audit reports (not in repo — user-specific):

- `state.py locked()` yields without lock on filelock timeout — deliberate graceful degradation
- `_import_worker` holds the import lock for the entire bulk-import (can be hours) — by design, one import at a time
- `_write_map` in `mapping.py` has cross-process filelock protection since v1.x — was missing initially

## Platform support matrix

| Feature | Windows | macOS | Linux |
|---|---|---|---|
| Hooks + dashboard | ✅ tested | ✅ tested | should work, not tested |
| Auto-ingest | ✅ | ✅ | should work |
| Folder picker in UI | ✅ PowerShell | ✅ osascript | ❌ returns 400 |
| kill_proc_tree | ✅ `taskkill /T` | ✅ `killpg(SIGKILL)` | ✅ same as mac |
| Desktop shortcut | ✅ via `.ps1` | ⚠️ manual (Dock or zsh alias) | ⚠️ manual |
| Start launcher | ✅ `.bat` | ✅ `.command` | ⚠️ run `.py` directly |
