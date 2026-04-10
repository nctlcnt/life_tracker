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

**Environment setup:** Copy `.env.example` to `.env` and populate:
- `DISCORD_TOKEN` - Discord bot token
- `AI_API_KEY` - OpenAI (or compatible) API key
- `AI_MODEL` - model name (defaults to `gpt-4`)
- `ALLOWED_USER_ID` - Discord user ID (single-user restriction)
- `API_PORT` - FastAPI port (defaults to `8000`)

There is no test suite or linter configured.

## Architecture

This is a personal life-tracking assistant operated via Discord chat. It has three services that run concurrently in a single asyncio event loop (`asyncio.gather` in `main.py`):

1. **Discord Bot** (`bot/discord_bot.py`) - receives messages from the allowed user, passes them to the AI engine, sends replies
2. **Scheduler** (`bot/scheduler.py`) - triggers proactive check-ins at random intervals (1-60 min) and polls for pending reminders every 30 seconds
3. **FastAPI server** (`api/server.py`) - exposes read-only REST endpoints for a React frontend (not yet implemented)

### Data Flow

```
Discord message -> discord_bot.py -> ai_engine.chat()
                                      -> loads last 20 messages from SQLite for context
                                      -> calls OpenAI API with tool definitions
                                      -> if tool_call: executes against database
                                      -> saves reply to SQLite
                                      -> returns reply text to Discord
```

### AI Tool Calling

Three tools are defined in `bot/tools.py` in OpenAI function-calling format:
- `log_timeline_event` - inserts into the `events` table
- `set_reminder` - inserts into the `reminders` table
- `query_timeline` - queries `events` by time range

The AI engine (`bot/ai_engine.py`) supports up to 5 rounds of tool-calling per message to handle chained tool use. The system prompt and tool definitions both live in `bot/tools.py`.

### Database

SQLite at `data/life_tracker.db`, managed by `bot/database.py`:
- `events` - timeline entries with `start_time`, `end_time` (nullable), `content`, `category`
- `messages` - rolling conversation history for AI context (last 20 fetched per call)
- `reminders` - scheduled reminders with `trigger_time`, `action`, `done` flag

### Dependency Injection

The `Database` instance is created once in `main.py` and passed to all other components. The FastAPI app receives it via `set_database()`. There is no global state beyond this single shared instance.

### Single-User Design

All Discord messages from users other than `ALLOWED_USER_ID` are silently ignored. The app is intentionally not multi-tenant.

### Codebase Language

Comments, the README, and AI system prompts are written in Chinese.