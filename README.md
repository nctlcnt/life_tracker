# Life Tracker

A personal life-tracking assistant that combines Discord, AI, and a web dashboard. Chat with the bot like a friend; in the background it logs activities, schedules reminders, manages long-term memory, and surfaces everything on a timeline-based web frontend.

> Single-user, self-hosted. Not intended for public deployment or third-party contribution.

## Features

- **Conversational logging** — natural Discord conversation; the AI decides what's worth recording.
- **Timeline of events** — categorized (`Focus` / `Routine` / `Chill`), supports parallel and in-progress events, grouped by project.
- **Proactive reminders** — the AI identifies reminder-worthy moments and pushes Discord messages on schedule.
- **Persistent memory** — bounded long-term memory (deadlines, preferences, etc.) preserved across conversations.
- **Web dashboard** — daily timeline, weekly view, project Gantt, memory browser, todos, reminders.
- **Multi-provider AI** — Claude / OpenAI / Gemini / relay endpoints, switchable at runtime via slash commands with optional fallback.
- **Weather assist** — `/weather` for the configured location, or `/weather <address>` to query any place via Google Geocoding + tomorrow.io.

## Architecture

| Layer | Stack |
|---|---|
| Bot | `discord.py` slash commands + scheduler + AI engine |
| API | FastAPI, served on the same process |
| Frontend | React + Vite + TypeScript (built artifacts served by FastAPI) |
| Storage | SQLite (single file), optional Litestream → Cloudflare R2 replication |
| Runtime | One Python process orchestrating everything via `asyncio.gather` |
| Packaging | Docker multi-stage build (Node frontend + Python runtime) |

All secrets and tunables live in a single `config.json` mounted into the container. State is persisted in `data/life_tracker.db` plus a small JSON for the active AI preset.

## Quick start (local)

Requires Python 3.12+ and Node/pnpm if running outside Docker.

```bash
cp config.example.json config.json   # fill Discord token, AI keys, etc.

# Option A — bare metal
pip install -r requirements.txt
cd frontend && pnpm install && pnpm build && cd ..
python main.py
python main.py --test                # verbose: dump logs + AI prompt payloads to data/test_logs/

# Option B — Docker (recommended)
make dev                             # docker compose up --build
```

Open `http://localhost:8080` for the dashboard.

## Configuration

`config.json` is the single source of truth. It is **not** committed and **not** baked into the image — the deploy side mounts it. Reference shape (`config.example.json`):

```jsonc
{
  "discord": {
    "token": "...",                  // required
    "allowed_user_id": 0,            // required, single-user mode
    "channel_id": 0                  // bot only listens in this channel
  },
  "ai": {
    "default_preset": "claude-opus", // required, must exist in `presets`
    "default_fallback": "",          // optional, used when primary preset fails
    "presets": {                     // at least one; runtime-switchable via /model
      "claude-opus": {
        "provider": "claude",        // claude / openai / gemini / relay
        "api_key": "...",
        "base_url": "",              // only for relay
        "model": "claude-opus-4-6"
      }
    }
  },
  "server":  { "port": 8080 },
  "weather": {
    "api_key": "",                   // tomorrow.io; empty disables silently
    "location": "-33.8688,151.2093", // default lat,lon
    "geocoding_api_key": ""          // Google Geocoding; required for /weather <address>
  },
  "poll": { "min_seconds": 60, "max_seconds": 3600 },
  "timezone": "Australia/Sydney",
  "log": { "level": "INFO", "file": null }
}
```

Active AI preset and timezone overrides are persisted to `data/active_preset.json` and `data/active_tz.json` so they survive restarts.

## Discord commands

| Command | Purpose |
|---|---|
| (just talk) | Free-form conversation; the AI logs events, sets reminders, and writes memories on its own. |
| `/todo add\|list\|all\|done\|del` | Manual todo management (no AI). |
| `/weather [address]` | Today's weather + outfit/umbrella advice. With an address, geocodes and queries that location. |
| `/model [name]` | Show or switch the primary AI preset. |
| `/fallback [name\|off]` | Show or switch the fallback preset. |
| `/tz [iana]` | Show or switch the process timezone (used for travel). |

The bot only responds in the channel set by `discord.channel_id` and only to the user set by `discord.allowed_user_id`.

## Project structure

```
├── bot/
│   ├── discord_bot.py      # Discord I/O + slash command registration
│   ├── ai_engine*.py       # AI dispatch + tool-calling, one file per provider
│   ├── scheduler.py        # Random check-ins + reminder polling
│   ├── database.py         # SQLite access layer + schema migrations
│   ├── tools.py            # Tool definitions exposed to the AI
│   ├── prompts.py          # System prompt assembly + editable overrides
│   ├── weather.py          # tomorrow.io + Google Geocoding integration
│   └── test_mode.py        # Capture logs and AI payloads to JSONL
├── api/server.py           # FastAPI REST endpoints + static file serving
├── frontend/               # React + Vite + TypeScript dashboard
├── main.py                 # Entry point — asyncio.gather of bot / scheduler / API
├── config.py               # Loads config.json + runtime preset switching
├── data/                   # SQLite DB + active state (mounted from host)
└── docs/                   # Database schema, deployment, dispatch samples
```

## Deployment

The production stack runs on a VPS via `docker compose`, with images published to GitHub Container Registry (`ghcr.io/nctlcnt/life_tracker`). Two image tags are produced per release:

| Tag | Meaning |
|---|---|
| `:vX.Y.Z` | Immutable, archived forever |
| `:stable` | Always points at the latest stable release |

Release workflow:

```bash
make release VERSION=v1.0.0          # tag + push; GitHub Actions builds and publishes the image
make deploy  VERSION=v1.0.0          # on the server: pull and restart prod
```

**Staging is mandatory before prod.** The same VPS hosts a parallel staging stack on port 8081 backed by a second Discord bot and an isolated SQLite. Untested working-tree changes only ever land on staging first; prod only consumes registry releases.

For first-time VPS setup, daily upgrade flow, rollback procedure, the staging stack, and troubleshooting see [`docs/deploy.md`](docs/deploy.md).

## Documentation

- [`docs/deploy.md`](docs/deploy.md) — first-time install, releases, upgrades, staging, troubleshooting
- [`docs/database.md`](docs/database.md) — SQLite schema reference
- [`docs/dispatch-escalation-samples.md`](docs/dispatch-escalation-samples.md) — sample AI dispatch traces
