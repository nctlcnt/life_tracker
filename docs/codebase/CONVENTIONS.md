# Coding Conventions

## 1) Naming Rules

| Item | Rule | Example | Evidence |
|------|------|---------|----------|
| Python files | lowercase snake_case | `google_calendar.py`, `ai_engine_base.py` | `bot/` |
| React component files | PascalCase; UI primitives lowercase kebab-case | `AdminPanel.tsx`, `alert-dialog.tsx` | `frontend/src/app/components/` |
| Functions/methods | snake_case in Python; camelCase in TS/TSX | `get_active`, `routeFromHash` | `config.py`, `frontend/src/app/App.tsx` |
| Types/classes | PascalCase | `Database`, `Scheduler`, `PromptParts`, `ViewMode` | `bot/database.py`, `frontend/src/app/App.tsx` |
| Private helpers | leading underscore in Python; no consistent TS private marker | `_build_preset`, `_fetch_forecast` | `config.py`, `bot/weather.py` |
| Constants/env vars | uppercase snake case | `API_PORT`, `REMINDER_BATCH_WINDOW` | `config.py`, `bot/scheduler.py` |

## 2) Formatting and Linting

- Formatter: none configured in repository.
- Linter/type checker: none configured in repository; `frontend/package.json` has no lint or type-check script.
- Enforced rules: [TODO] no automated style rules are evidenced.
- Available verification commands are `.venv/bin/python -m pytest -q` and `cd frontend && npm run build`.
- Source style is mixed: Python generally follows four-space indentation and type hints selectively; frontend files mix quote/semicolon styles, so observed formatting is not a reliable policy.

## 3) Import and Module Conventions

- Python imports usually group standard library, third-party, then `bot`/`config`, but the order is not mechanically enforced.
- Python uses absolute project imports (`from bot...`) rather than relative package imports.
- TypeScript defines `@` as `frontend/src` in Vite; current feature components predominantly use relative imports.
- Vite has no `server.strictPort` setting even though the host policy requires it; treat this as a deployment gap rather than an established convention.
- No Python or frontend barrel-export policy is present.

## 4) Error and Logging Conventions

- HTTP routes use `HTTPException` for client errors, but accept raw `dict` bodies rather than Pydantic request models. Some integration failures are returned as successful HTTP responses containing `ok: false`.
- AI adapters normalize expected provider failures to `AIProviderError`; the router only invokes fallback for that type.
- Optional weather, embedding, and calendar behavior often logs and degrades without stopping the main chat path.
- Logging uses the standard library through `bot/logger.py`, with module loggers and human-readable emoji-prefixed messages. There is no structured production log schema.
- API keys are masked in preset list responses, but traces, error text, tool arguments, and local runtime files have no documented global redaction policy. The intent question is centralized in `CONCERNS.md`.

## 5) Testing Conventions

- Tests live in `tests/` and use `test_*.py` plus `test_*` functions.
- Tests use plain pytest assertions, parametrization, `tmp_path`, and `monkeypatch`; scheduler async behavior is normally driven with `asyncio.run`.
- Database tests create a fresh SQLite file under pytest temporary directories.
- Coverage expectation: [TODO] no tool, threshold, or stated target exists.
- Frontend testing convention: [TODO] no test framework or test files are configured.

## 6) Evidence

- `bot/logger.py`
- `bot/ai_engine.py`
- `api/server.py`
- `tests/test_prompt_render.py`
- `tests/test_scheduler_checkins.py`
- `frontend/vite.config.ts`
- `frontend/package.json`
