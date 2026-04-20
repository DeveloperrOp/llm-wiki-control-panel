# Privacy and security

## What leaves your machine

**To the Anthropic API** (unavoidable — this is how Claude CLI works):

- The contents of every Claude Code session — both normal sessions and ingest/lint subsessions started by this system
- The context injection at `SessionStart`: your project's `index.md` + the last 30 lines of `log.md`
- The source file fed to `ingest` (the saved transcript) and the prompt from `prompts/ingest-ru.md`
- The full list of wiki pages fed to semantic `lint` (if you use the "Глубокая проверка" / deep lint button)

**To nowhere else:**

- `<vault>/raw/` stays entirely local until you run ingest on it
- `.backups/`, service files under `state/`, `.unassigned/`, logs — never sent anywhere
- Passwords, keys, configs — unless they ended up in your Claude Code conversation by your own action

## Keep secrets out of conversations

If you show Claude a `.env`, password, DB credentials, or API key in a chat:

1. **The conversation already went to Anthropic** — nothing this project does can undo that
2. The transcript is saved locally in `<vault>/raw/chats/` — delete the `.md` file there
3. If auto-ingest was on, generated pages in `<vault>/wiki/` may contain the secret — check and delete them
4. The raw JSONL in `~/.claude/projects/...` also has the secret — delete that file too

## Dashboard is localhost-only

The Flask server listens on `127.0.0.1:5757`. It is **not reachable from your local network**. There is no authentication, and none is needed at this binding address.

**Do not change this:**

- ❌ Don't set `host="0.0.0.0"` in `app.run()` — that opens the dashboard to anyone on your Wi-Fi
- ❌ Don't expose via `ngrok`, `cloudflared`, or a reverse proxy without adding HTTP basic auth first — the API can **delete your files** (there's a `DELETE /api/chat` endpoint, a project delete endpoint, etc.)
- ❌ Don't commit `config/project-map.json` to a public repository — it contains absolute paths to your personal folders

## What `.gitignore` keeps out

If you use git for this repo, `.gitignore` prevents accidental commits of:

- `config/project-map.json` — your personal paths
- `state/` — operational logs, job history, session registry
- `.unassigned/` — unassigned Claude Code sessions
- `raw/`, `wiki/`, `log.md`, `index.md`, `CLAUDE.md` — user knowledge base content
- `.backups/` — local per-operation snapshots
- `tmp/`, `.playwright-mcp/` — scratch space, test artifacts

## Report a vulnerability

If you find a security issue, open a private security advisory on GitHub rather than a public issue.
