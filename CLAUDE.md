# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Run locally:**
```bash
python main.py
```

**Run in Docker:**
```bash
docker build -t life-tracker .
docker run -e DISCORD_TOKEN=... -e AI_API_KEY=... life-tracker
```

**Environment setup:** Copy `.env.example` to `.env` and populate (see `config.py` for defaults):
- `DISCORD_TOKEN` - Discord bot token
- `AI_API_KEY` - API key for the chosen provider
- `AI_PROVIDER` - `claude` (default), `relay`, or `gemini`
- `CHAT_MODEL` / `POLL_MODEL` - model names per provider (defaults: `claude-opus-4-6` / `claude-3-5-sonnet-latest`)
- `AI_BASE_URL` - base URL for relay mode only
- `ALLOWED_USER_ID` - Discord user ID (single-user restriction)
- `API_PORT` - FastAPI port (defaults to `8080`)
- `LOG_LEVEL` - logging level: DEBUG/INFO/WARNING/ERROR (defaults to `INFO`)
- `LOG_FILE` - optional path to write a rotating log file (defaults to stdout-only)

There is no test suite or linter configured.

## Architecture

Personal life-tracking assistant operated via Discord. Three services run concurrently in one asyncio event loop (`asyncio.gather` in `main.py`):

1. **Discord Bot** (`bot/discord_bot.py`) - receives messages from the allowed user, passes to AI engine, sends replies. Handles Discord reply/quote context.
2. **Scheduler** (`bot/scheduler.py`) - two concurrent loops sharing an `asyncio.Lock`:
   - **Timer loop**: random proactive check-ins (1-60 min) + bedtime reminders (22:30-00:00), pure in-memory countdown
   - **Reminder loop**: database reminders with precise countdown to next `trigger_time`, woken by `asyncio.Event` when new reminders are added
3. **FastAPI server** (`api/server.py`) - read-only REST endpoints (`/api/timeline`, `/api/events`, `/api/categories`, `/api/memories`, `/api/reminders`, `/api/todos`). Serves a static frontend from `frontend/` at `/app/`.

### AI Engine: Multi-Provider with Shared Base

`bot/ai_engine.py` is a **router** — it imports `chat`, `scheduled_action` from the correct backend based on `config.AI_PROVIDER`:

- `bot/ai_engine_claude.py` - Anthropic native API (uses `TOOLS_ANTHROPIC` format, supports prompt caching)
- `bot/ai_engine_gemini.py` - Google Gemini REST API (converts OpenAI tool format to Gemini format)
- `bot/ai_engine_relay.py` - OpenAI-compatible relay/proxy API

All three delegate shared logic to `bot/ai_engine_base.py`:
- `_build_dynamic_context()` - injects memories, ongoing events, pending reminders into each call
- `_execute_tool()` - dispatches tool calls to database operations
- `chat()`, `scheduled_action()` - high-level flows that each engine wraps

Each engine only implements its own `_call_with_tools()` (API-specific request/response handling). Max 5 tool-calling rounds per message. Intermediate-round text is sent to the user immediately via `send_callback`.

### Tool Calling

Nine tools defined in `bot/tools.py` in both OpenAI (`TOOLS`) and Anthropic (`TOOLS_ANTHROPIC`) formats:
- `log_timeline_event` / `update_timeline_event` / `query_timeline` - timeline CRUD
- `set_reminder` / `cancel_reminders` / `list_reminders` - reminder management (supports `group_id` and `priority`)
- `save_memory` / `delete_memory` / `update_memory` - AI persistent memory (capped at 20 entries, auto-evicts oldest)

The system prompt (`SYSTEM_PROMPT`) is also in `bot/tools.py`.

### Database

SQLite at `data/life_tracker.db`, managed by `bot/database.py`:
- `events` - timeline entries with `start_time`, `end_time`, `content`, `category`, `notes`, `session_id`
- `messages` - **backup only**; no longer read by the AI engines. Every user turn and assistant reply is still appended here for disaster recovery, but AI context is fetched live from Discord channel history (see below).
- `reminders` - with `trigger_time`, `action`, `group_id`, `priority`, `status` (pending/triggered/cancelled)
- `memories` - AI's persistent memory store
- `todos` - slash-command managed todo list with `id`, `content`, `done` status

When a reminder is added, `Database` fires `_on_reminder_added` callback to wake the scheduler's reminder loop via `asyncio.Event`.

Schema migrations are handled inline via `ALTER TABLE` with `try/except` for idempotency.

### Key Design Decisions

- **Single-user**: all Discord messages from non-`ALLOWED_USER_ID` users are silently ignored
- **Conversation context from Discord, not DB**: callers (`discord_bot.on_message` for user turns, `scheduler._do_*` for polling/reminders) fetch the last 20 channel messages via `LifeTrackerBot._fetch_history_as_messages()` and pass them into `chat()` / `scheduled_action()`. The DB `messages` table is write-only backup. Slash-command outputs (`/todo`, `/weather`) remain in channel history and are included as `assistant`-role messages — `PROMPT_RESPONSE_GUIDELINES` explicitly tells the AI how to identify them so it doesn't mistake them for its own speech.
- **Scheduler wiring**: `Scheduler` constructor takes both `send_callback` (`bot.send_proactive_message`) and `fetch_history_callback` (`bot.fetch_history_for_scheduler`). The fetch callback uses the bot's remembered `target_channel_id`; before any user has ever messaged the bot, it returns an empty history (polling will short-circuit via `allow_silent`).
- **Dependency injection**: `Database` instance created once in `main.py`, passed to all components; FastAPI receives it via `set_database()`
- **Event merging**: `bot/merge.py` merges adjacent events with same content+category into time segments for the `/api/timeline` endpoint
- **Logging**: centralized in `bot/logger.py`. `main.py` calls `setup_logging()` before importing other modules; each module gets its own logger via `get_logger(__name__)`. All config (format, level, handlers) lives in `bot/logger.py` — change it in one place.

### Codebase Language

Comments, the README, and AI system prompts are written in Chinese.
