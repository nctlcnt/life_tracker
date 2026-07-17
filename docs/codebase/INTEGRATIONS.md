# External Integrations

## 1) Integration Inventory

| System | Type | Purpose | Auth model | Criticality | Evidence |
|--------|------|---------|------------|-------------|----------|
| Discord | Gateway/API | Primary chat input/output and slash commands | Bot token; configured user/channel IDs restrict bot handling | high | `config.example.json`, `bot/discord_bot.py` |
| Anthropic API | AI API | Claude chat, tools, and scheduled actions | API key in `config.json` preset | high when selected | `bot/ai_engine_claude.py` |
| OpenAI API | AI API | OpenAI chat/tools and compatible embeddings | API key in `config.json` | high when selected | `bot/ai_engine_openai.py`, `bot/embeddings.py` |
| Relay endpoint | OpenAI-compatible API | Custom AI gateway/provider | API key and configurable base URL | high when selected | `bot/ai_engine_relay.py`, `config.example.json` |
| Google Gemini | AI API | Gemini chat, tools, and scheduled actions | API key in `config.json` | high when selected | `bot/ai_engine_gemini.py` |
| Google Calendar | OAuth/API | Read-only calendars and prompt context | OAuth client JSON and refresh token file | optional | `bot/google_calendar.py`, `config.example.json` |
| Tomorrow.io | Weather API | Forecast/context and dashboard weather | API key in `config.json` | optional | `bot/weather.py` |
| Google Geocoding | API | Resolve `/weather <address>` | API key in `config.json` | optional | `bot/weather.py` |
| Cloudflare R2 via Litestream | Object storage | Optional continuous SQLite replication | access key/secret via environment | operational | `litestream.yml`, `compose.yaml` |
| GitHub/GHCR | CI/container registry | Build and store tagged multi-arch images | Actions `GITHUB_TOKEN`; host registry credentials for private image | operational | `.github/workflows/release.yml`, `docs/deploy.md` |

No queue, event bus, service mesh, or external APM is evidenced.

## 2) Data Stores

| Store | Role | Access layer | Key risk | Evidence |
|-------|------|--------------|----------|----------|
| SQLite `data/life_tracker.db` | Events, conversations, reminders, memories, todos, prompts, traces, app state | `bot/database.py` | Single-file/single-instance assumptions and startup migrations | `config.py`, `bot/database.py` |
| JSON state/config files | Secrets, AI presets, timezone, OAuth client/token | `config.py`, `bot/timezone_state.py`, `bot/google_calendar.py` | Plaintext local credentials and non-atomic mutable config writes | named files |
| JSONL trace files | Detailed local AI debugging traces | `bot/trace.py`, `bot/test_mode.py` | Intentionally excluded from disaster recovery as of 2026-07-12; total host loss may delete them | named files |
| R2 replica | Disaster-recovery copy of SQLite | Litestream sidecar | Restore is operational/manual; retained data must remain access-controlled | `litestream.yml`, `docs/deploy.md` |

## 3) Secrets and Credentials Handling

- Discord, AI, embedding, weather, and geocoding credentials live in ignored `config.json`; OAuth client/token files live under ignored `data/`; R2 keys are injected as environment variables.
- `config.example.json` contains placeholders only, and `.gitignore` excludes `config.json`, `.env*` (with examples allowed), and `data/`.
- The Admin API masks keys on reads but can accept and persist new keys to `config.json`.
- Rotation procedures are documented only for GHCR PAT troubleshooting. [TODO] Rotation procedures for Discord, AI, Google OAuth, weather, and R2 credentials are not documented.

## 4) Reliability and Failure Behavior

- AI provider routing supports one configured fallback after `AIProviderError`; provider adapters translate selected SDK/HTTP failures.
- Weather/geocoding calls use an 8-second `httpx` timeout. Relay uses the `httpx` default timeout. SDK and Google Calendar timeout policies are not set in application code.
- No circuit breaker is present. Provider clients and calendar service/context are cached process-locally.
- Embedding failure leaves the row unembedded and preserves chat behavior. Weather and optional calendar context can degrade to absent context; calendar refresh can send a daily Discord health alert for selected errors.
- Scheduler uses events to wake for new reminders/config changes and an internal lock to avoid overlapping scheduler-originated AI calls.

## 5) Observability for Integrations

- Standard logging surrounds Discord, AI, embedding, weather, and calendar failures.
- AI calls have JSONL traces and SQLite `ai_runs`/`tool_calls`; a frontend trace viewer queries them.
- Container health checks cover only `/api/health`, which returns a constant response and does not probe SQLite, Discord, AI, Calendar, or R2 health.
- No metrics, distributed tracing, alert manager, or explicit SLOs are evidenced. Litestream replica freshness is not exposed by the application.
- Conversation and trace data is retained without an automatic archive/compaction feature or configured size threshold. This is the confirmed current intent for the single-user deployment; revisit only after an observed storage or retrieval problem.
- Disaster-recovery scope intentionally covers SQLite `ai_runs`/`tool_calls`, not `data/ai_traces/*.jsonl`. Reconsider if JSONL becomes a product data source or gains an audit-retention requirement.

## 6) Evidence

- `config.example.json`
- `bot/discord_bot.py`
- `bot/ai_engine.py`
- `bot/weather.py`
- `bot/google_calendar.py`
- `bot/trace.py`
- `litestream.yml`
- `.github/workflows/release.yml`
