# Technology Stack

## 1) Runtime Summary

| Area | Value | Evidence |
|------|-------|----------|
| Primary languages | Python backend; TypeScript/TSX frontend | `Dockerfile`, `frontend/src/main.tsx` |
| Runtime + version | Python 3.12; Node.js 20 for frontend builds | `Dockerfile` |
| Package managers | pip (`requirements*.txt`); npm with a committed lockfile for frontend development and Docker builds | `Dockerfile`, `frontend/package-lock.json`, `frontend/package.json` |
| Module/build system | Python modules started by `main.py`; Vite 6 ESM frontend | `main.py`, `frontend/package.json` |

No exact Python package versions are locked: the Python manifests specify lower bounds. Frontend versions are exact in `frontend/package.json`, resolved by `frontend/package-lock.json`, and installed with `npm ci` in Docker.

## 2) Production Frameworks and Dependencies

| Dependency | Declared version | Role in system | Evidence |
|------------|------------------|----------------|----------|
| FastAPI / Uvicorn | `>=0.110.0` / `>=0.27.0` | REST API and ASGI server | `requirements.txt`, `api/server.py`, `main.py` |
| discord.py | `>=2.3.0` | Discord bot and slash commands | `requirements.txt`, `bot/discord_bot.py` |
| Anthropic / OpenAI / Google GenAI SDKs | `>=0.40.0` / `>=1.0.0` / `>=0.8.0` | Swappable AI provider adapters | `requirements.txt`, `bot/ai_engine_*.py` |
| Google API client/auth libraries | lower-bounded in manifest | Read-only Google Calendar OAuth and API client | `requirements.txt`, `bot/google_calendar.py` |
| React / React DOM | `18.3.1` peer dependencies | Browser UI | `frontend/package.json`, `frontend/src/main.tsx` |
| Material UI, Radix UI, Recharts | exact versions in manifest | UI controls, dashboard components, charts | `frontend/package.json`, `frontend/src/app/components/` |
| SQLite | Python standard library | Local persistent store | `bot/database.py` |
| httpx | transitive only; no direct declaration | Relay, weather, and geocoding HTTP calls | `bot/ai_engine_relay.py`, `bot/weather.py`, `requirements.txt` |

## 3) Development Toolchain

| Tool | Purpose | Evidence |
|------|---------|----------|
| pytest `>=8.0` | Python tests | `requirements-dev.txt`, `pytest.ini` |
| Vite 6.3.5 | Frontend development and production build | `frontend/package.json`, `frontend/vite.config.ts` |
| Tailwind Vite plugin 4.1.12 | CSS build integration | `frontend/package.json`, `frontend/vite.config.ts` |
| Docker Buildx + QEMU | Multi-platform release image (`amd64`, `arm64`) | `.github/workflows/release.yml` |
| Litestream 0.3.13 | Optional SQLite replication sidecar | `compose.yaml`, `litestream.yml` |

No repository formatter, linter, type-check command, or frontend test runner is configured.

## 4) Key Commands

```bash
python3 -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
cd frontend && npm run build
make dev
make release VERSION=vX.Y.Z
make deploy VERSION=vX.Y.Z
```

The confirmed authoritative production path is the version-tag workflow: `.github/workflows/release.yml` builds versioned GHCR images, and `make deploy VERSION=vX.Y.Z` deploys an explicit version. It does not currently create a GitHub Release object.

## 5) Environment and Config

- Application settings and secrets come from ignored `config.json`, with the committed shape in `config.example.json`. Active AI preset and timezone are persisted under `data/`.
- Compose deployment values come from `.env`/`.env.prod`: `BIND`, `API_PORT`, `VERSION`, `LIFE_TRACKER_API_KEY`, `LIFE_TRACKER_COOKIE_SECURE`, and the R2/Litestream credentials are evidenced by compose configuration.
- The Docker runtime is `python:3.12-slim`; the frontend is built in `node:20-alpine`. The image starts one `python main.py` process and persists `/app/data` plus the mounted `/app/config.json`.
- The deployed service port is registered as 8080 in `infra overview` (2026-07-17). `.env.prod` and compose defaults bind it to `127.0.0.1`; framework port defaults still appear in source and Docker.
- `frontend/vite.config.ts` proxies `/api` to `127.0.0.1:8080` and enables the VPS-required `server.strictPort: true`.

## 6) Evidence

- `requirements.txt`
- `requirements-dev.txt`
- `frontend/package.json`
- `Dockerfile`
- `compose.yaml`
- `.github/workflows/release.yml`
