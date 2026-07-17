# Architecture

## 1) Architectural Style

- Primary style: modular single-process application with layered boundaries and adapter strategies.
- Classification evidence: `main.py` wires a persistence object into HTTP, bot, and scheduler modules; AI providers implement the same `chat`/`scheduled_action`/`simple_completion` surface behind `bot/ai_engine.py`.
- Primary constraints: single-user Discord identity/channel filtering; one SQLite file; one asyncio process; external AI/provider availability; mutable prompt/config state stored outside source control.

## 2) System Flow

```text
Discord message -> LifeTrackerBot -> AI router/provider tool loop -> Database/tools -> Discord response
Browser fetch   -> FastAPI route  -> Database/domain helper       -> JSON response -> React view
Scheduler due   -> AI scheduled_action -> Database/tools          -> Discord message
```

Discord chat flow:

1. `LifeTrackerBot.on_message` accepts configured user/channel traffic, enriches replies, and appends raw and compatibility conversation records.
2. It loads the recent channel context from `Database` and calls `bot.ai_engine.chat`.
3. `bot.ai_engine` dynamically imports the active provider adapter and retries with the configured fallback only for `AIProviderError`.
4. Shared logic in `bot/ai_engine_base.py` assembles prompt context and executes declared tools; tools call `Database` methods.
5. Provider adapters record trace rounds; the bot chunks the final text to Discord and stores outbound conversation messages.

HTTP flow is direct: React calls relative `/api` endpoints; `api/server.py` validates selected fields and calls the globally injected `Database`, with `bot/merge.py` used for timeline presentation.

## 3) Layer/Module Responsibilities

| Layer or module | Owns | Must not own | Evidence |
|-----------------|------|--------------|----------|
| Bootstrap (`main.py`) | Initialization order and concurrent task lifecycle | Provider request formats | `main.py` |
| Transports (`api/server.py`, `bot/discord_bot.py`) | HTTP/Discord input and output | SQLite schema | `api/server.py`, `bot/discord_bot.py` |
| Orchestration (`bot/ai_engine.py`, `bot/ai_engine_base.py`, `bot/scheduler.py`) | AI routing/tool loop and timed workflows | Browser rendering | named files |
| Provider adapters (`bot/ai_engine_*.py`) | SDK/wire-format conversion and provider errors | Route or Discord command ownership | `bot/ai_engine_claude.py`, `bot/ai_engine_openai.py` |
| Persistence (`bot/database.py`) | Schema initialization, migrations, queries, state | HTTP status codes | `bot/database.py` |
| Presentation (`frontend/src/app/`) | Dashboard state, views, and API calls | Provider credentials | `frontend/src/app/App.tsx` |

## 4) Reused Patterns

| Pattern | Where found | Why it exists |
|---------|-------------|---------------|
| Strategy/adapter | `bot/ai_engine.py`, `bot/ai_engine_{claude,openai,relay,gemini}.py` | Switch providers at runtime behind a common operation set |
| Fallback | `bot/ai_engine.py` | Retry an AI operation with another preset after normalized provider failure |
| Manual dependency injection | `main.py`, `api/server.py` | Share one `Database` and scheduler callback without a DI framework |
| Repository-like access object | `bot/database.py` | Centralize SQLite schema and queries |
| Process-local singleton/cache | `config.py`, provider `_clients`, `bot/google_calendar.py` | Reuse mutable configuration and API clients in one process |
| Observer callbacks/events | `main.py`, `bot/scheduler.py` | Wake/recalculate scheduler state after reminders, check-ins, or AI calls |

## 5) Startup and Concurrency

Initialization order is significant: API-only environment selection precedes `config` import; logging and process timezone initialize before modules using them; the database initializes before injection and before bot/scheduler creation. Normal mode creates bot, scheduler, and Uvicorn tasks on one event loop. Scheduler uses an `asyncio.Lock` to serialize its own AI triggers, but Discord chat calls are outside that scheduler lock.

SQLite methods open a new synchronous connection per operation. Those calls run on the same event-loop thread, as do synchronous Google Calendar client calls invoked from async functions.

## 6) Known Architectural Risks

- The global `db` and mutable config/client caches assume one initialized process; multiple workers or replicas would not share in-memory callbacks, active state, locks, or cache state.
- `bot/database.py` (1,618 lines), `api/server.py` (864), and `bot/discord_bot.py` (840) combine many domains and are also high-churn files, increasing regression and onboarding cost.
- HTTP routes expose data and state mutation without application authentication. This is an intentional single-user design: the service is bound privately and dashboard/API access requires WireGuard; it must remain absent from the public Internet.
- Schema migration is embedded in startup as many best-effort `ALTER TABLE` statements rather than versioned, transactional migrations.

## 7) Evidence

- `main.py`
- `bot/discord_bot.py`
- `bot/ai_engine.py`
- `bot/ai_engine_base.py`
- `bot/scheduler.py`
- `bot/database.py`
- `api/server.py`
