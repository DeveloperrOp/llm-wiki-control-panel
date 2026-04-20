# Claude Code — инструкция для установки

Этот файл читает Claude Code автоматически при запуске в папке репозитория. Если пользователь попросит **«установи эту систему»** / **«setup this»** / что-то похожее — выполни установку по инструкции ниже.

---

## Что делать, если пользователь попросил установить

### Способ 1 (рекомендуется): запусти `install.py`

```bash
# Windows
python install.py

# macOS / Linux
python3 install.py
```

Скрипт интерактивный: задаёт вопросы, показывает что делает, можно прервать `Ctrl+C` в любой момент. После завершения — всё готово.

**Почему предпочтительно:** скрипт идемпотентен, делает `merge` в `~/.claude/settings.json` (не затирает существующие настройки), атомарно пишет файлы, создаёт бэкапы при необходимости.

### Способ 2: если пользователь хочет делать руками

Читай `README.md` — раздел **Installation**. Сделай всё по шагам:
1. Проверь зависимости (Python 3.10+, `claude --version`, `node --version`)
2. `pip install -r requirements.txt` (на macOS: `pip3`)
3. Спроси у пользователя куда положить vault (дефолт: `C:/Obsidian/` на Windows, `~/Obsidian/` на macOS)
4. Спроси имя первого проекта (дефолт: `My Project`)
5. Создай папку vault со структурой (`raw/chats`, `raw/articles`, `raw/docs`, `raw/assets`, `wiki/entities`, `wiki/concepts`, `wiki/sources`)
6. Скопируй `config/project-map.example.json` → `config/project-map.json`, подставь реальные пути
7. Обнови `~/.claude/settings.json` — добавь блок с 3 хуками (см. `README.md`). **ВАЖНО:** если файл уже существует — делай merge, не перезаписывай

---

## Важные правила

### Абсолютные пути во всех конфигурациях

- В `config/project-map.json` — `vault_root`, `unassigned_root`, `vault_base` должны быть **абсолютными**
- В `~/.claude/settings.json` — путь к `session-start.py` и др. должен быть **абсолютным**
- Никаких `~` и `.` в путях конфигов (они не резолвятся хуками)

### Не трогай существующие настройки Claude Code

Если в `~/.claude/settings.json` уже есть блок `hooks` с другими хуками (от других проектов) — **добавь** наши, не замени. В `install.py` это делается так:

```python
# Нормально: существующий hook остаётся, наш добавляется
existing["hooks"]["SessionStart"] = old_entries + [our_entry]

# Плохо: затирает всё
existing["hooks"] = {"SessionStart": [our_entry]}
```

Если в `settings.json` есть что-то кроме хуков (`model`, `permissions`, etc.) — **не трогай**. Просто добавь секцию `hooks` если её нет, или расширь существующую.

### Проверь что всё работает в конце

После установки:
1. Запусти дашборд: `python scripts/dashboard.py` (или `python3 ...`)
2. Открой `http://localhost:5757/` и проверь плашку **«✅ Всё работает»**
3. Если красная — разверни **«▼ Подробнее»**, там видно что именно не так

---

## Частые проблемы и решения

| Проблема | Причина | Решение |
|---|---|---|
| `python: command not found` | На macOS Python 3 называется `python3` | Используй `python3` вместо `python` |
| `pip install` падает с permission error | Нужно ставить в user-scope | Добавь `--user`: `pip install --user -r requirements.txt` |
| `claude` CLI не найден | Не установлен Node.js + Claude Code | `brew install node && npm install -g @anthropic-ai/claude-code` (macOS) или с nodejs.org + npm (Windows) |
| `~/.claude/settings.json` битый JSON | Ручная правка сломала синтаксис | Скрипт создаст бэкап в `settings.json.bak` и перезапишет валидным JSON. Проверь бэкап, перенеси свои настройки вручную |
| Дашборд не открывается на 5757 | Порт занят | Убей процесс: `netstat -ano \| findstr 5757` (Win) или `lsof -i :5757` (Mac), потом `kill` |

---

## Что НЕ должен делать Claude при установке

1. **Не запускай `pip install` без согласия пользователя** — некоторые окружения (venv, системный Python, conda) требуют специальной обработки. Лучше спроси.

2. **Не трогай `~/.claude/settings.json` без подтверждения** — это глобальный конфиг Claude Code, пользователь может иметь там свои настройки.

3. **Не создавай vault в папке с кодом** — они должны быть отдельными. Если пользователь по ошибке укажет `./vault` внутри репо — предупреди.

4. **Не коммить `config/project-map.json`** — там абсолютные пути пользователя. Он в `.gitignore`, но если пользователь принудительно добавит — скажи что этого делать не стоит.

5. **Не меняй `requirements.txt`** без нужды — это публичный контракт.

---

## Структура проекта (для ориентации)

```
llm-wiki-control-panel/
├── install.py             ← запусти это для установки
├── README.md              ← описание проекта, ручная установка
├── CLAUDE.md              ← этот файл
│
├── hooks/                 ← 3 хука Claude Code (session-start, session-end, pre-compact)
├── scripts/               ← dashboard.py, ingest.py, lint.py, lib/
├── dashboard/             ← HTML-шаблоны и статика
├── prompts/               ← промпты для ingest/lint/optimize
├── config/
│   └── project-map.example.json   ← шаблон, ДОЛЖЕН быть скопирован в project-map.json
├── tests/                 ← invariants, fuzz, perf
├── docs/
│   ├── GUIDE.md           ← полное руководство пользователя
│   ├── PRIVACY.md         ← приватность и безопасность
│   └── DEVELOPMENT.md     ← для разработчиков
│
├── start-dashboard.bat    ← Windows launcher
├── start-dashboard.command ← macOS launcher
└── create-desktop-shortcut.ps1  ← Windows shortcut creator
```

---

## Если пользователь спросит «а что это?»

Короткий ответ:

> Это система автоматической памяти для Claude Code. Каждый твой разговор с Claude сохраняется в локальную markdown-вики. В следующей сессии Claude сразу видит контекст прошлых разговоров — не нужно каждый раз объяснять сначала. Работает через хуки Claude CLI + локальный веб-дашборд на `localhost:5757`. Совместимо с Obsidian.

Полное описание — в `README.md`, детальное руководство — в `docs/GUIDE.md`.
