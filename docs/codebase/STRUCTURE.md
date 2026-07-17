# Codebase Structure

## 1) Top-Level Map

| Path | Purpose | Evidence |
|------|---------|----------|
| `main.py` | Process bootstrap for database, API, Discord bot, and scheduler | `main.py` |
| `config.py` | Loads `config.json`, validates settings, and manages mutable AI/calendar state | `config.py` |
| `api/` | FastAPI routes and static frontend mounting | `api/server.py` |
| `bot/` | Discord, AI adapters/tool loop, scheduling, persistence, prompts, and external integrations | `bot/*.py` |
| `frontend/` | React/Vite dashboard source and frontend manifest | `frontend/src/`, `frontend/package.json` |
| `tests/` | pytest prompt-rendering and scheduler tests | `pytest.ini`, `tests/test_*.py` |
| `scripts/` | One-off maintenance, prompt import/export, embedding, and analysis CLIs | `scripts/*.py` |
| `docs/` | Deployment, database, design-history, and codebase reference documents | `docs/*.md` |
| `data/`, `data-dev/` | Ignored SQLite databases, runtime state, traces, tokens, and backups | `.gitignore`, `config.py`, `bot/trace.py` |
| `.github/workflows/` | Tag-triggered container release automation | `.github/workflows/release.yml` |
| `plans/`, `progress.md`, `devlog.md` | Historical plans and implementation notes; useful context but not runtime inputs | named paths, repository scan |
| `Dockerfile`, compose files, `Makefile` | Build, local/staging/production orchestration, release/deploy commands | `Dockerfile`, `compose.yaml`, `Makefile` |

Generated or runtime material (`frontend/dist/`, `frontend/node_modules/`, `__pycache__/`, `.pytest_cache/`, `.venv/`, `data*/`) is not source architecture.

## 2) Entry Points

- Main runtime entry: `main.py`, selected by Docker `CMD ["python", "main.py"]`.
- Runtime modes: `python main.py`, `python main.py --test`, and `python main.py --api-only`.
- Browser entry: `frontend/src/main.tsx`; Vite emits `frontend/dist`, mounted at `/app` by `api/server.py`.
- Maintenance entry points: executable `main()` functions under `scripts/`; they are invoked as Python modules or files and are not long-running workers.
- CI entry: semantic version tags matching `v*.*.*` trigger `.github/workflows/release.yml`.

## 3) Module Boundaries

| Boundary | What belongs here | What must not be here |
|----------|-------------------|------------------------|
| `main.py` | Initialization order, dependency wiring, task lifecycle | Business queries or provider-specific AI logic |
| `api/server.py` | HTTP validation/serialization and delegation | New SQLite schema definitions or Discord event handling |
| `bot/database.py` | SQLite schema, migrations, and persistence methods | HTTP response construction or UI state |
| `bot/ai_engine*.py` | Provider selection/adapters and shared tool-call orchestration | Discord-specific transport details |
| `bot/discord_bot.py` | Discord filtering, commands, message transport, conversation capture | Frontend API route definitions |
| `bot/scheduler.py` | Check-in, reminder, and calendar-refresh timing | Database schema ownership |
| `frontend/src/app/` | Views, client state, and `/api` calls | Secrets or direct external-provider calls |

These “must not” boundaries describe the separation visible in current imports and call sites, not an externally declared team policy.

## 4) Naming and Organization Rules

- Python source uses lowercase `snake_case.py`; classes use PascalCase and functions use snake_case.
- React component files and exported components use PascalCase; reusable low-level UI files use lowercase kebab-case.
- The backend is broadly layer/module-oriented; frontend components are feature/view-oriented with a generated-style `components/ui/` library.
- Python uses absolute project imports such as `from bot.database import Database`. TypeScript supports `@` -> `frontend/src`, though application components mostly use relative imports.

## 5) Evidence

- `README.md`
- `main.py`
- `api/server.py`
- `bot/database.py`
- `frontend/vite.config.ts`
- `.github/workflows/release.yml`
- `docs/codebase/.codebase-scan.txt` (generated during discovery; removed after validation)
