# Codebase Concerns

## 1) Top Risks (Prioritized)

| Severity | Concern | Evidence | Impact | Suggested action |
|----------|---------|----------|--------|------------------|
| medium | AI provider consolidation lacks adapter characterization tests | `bot/ai_engine_{openai,relay,claude,gemini}.py`; active=`kiro` and fallback=`glm` use OpenAI-compatible paths, but three native Gemini presets remain configured | LT-134 could regress tool rounds, fallback, request parameters, or old trace compatibility while deleting adapters | Freeze OpenAI-compatible request/response/tool-loop contracts before removing native adapters; migrate or explicitly retire native Gemini presets |
| medium | Framework port defaults remain in source | `main.py`, `Dockerfile`, compose files; `infra overview` on 2026-07-17 | Missing explicit port config can still select 8080, though compose bind fallbacks are loopback-only and Vite is strict | Remove remaining framework-default port fallbacks in a coordinated deployment change |
| high | Runtime directly imports undeclared `httpx` | `bot/weather.py`, `bot/ai_engine_relay.py`, `requirements.txt` | Fresh environments rely on a transitive dependency and may break when dependency graphs change | Add a direct, bounded `httpx` requirement and verify a clean image build/import test |
| medium | Release CI has no automated tests | `.github/workflows/release.yml`, `pytest.ini` | A version tag can publish a multi-arch image despite backend regression or frontend behavioral failure | Run pytest and frontend build/tests before image push |
| medium | Large high-churn modules combine responsibilities | `bot/database.py` 1,618 lines; `api/server.py` 864; `bot/discord_bot.py` 840; 90-day git churn | Changes have broad blast radius and weak focused coverage | Split by domain after characterization tests |
| low | No automatic data archive/compaction policy | `bot/trace.py`, `bot/test_mode.py`, conversation/trace tables in `bot/database.py` | Data continues to accumulate; this is accepted for the current single-user scale | No feature is planned now; revisit only if storage or retrieval performance becomes an observed problem |

The supplied VPS inventory says application listeners must bind only `127.0.0.1` or `10.66.66.1`. Current `.env.prod` and compose fallback values bind published ports to `127.0.0.1`; Uvicorn still binds all interfaces inside the container, which is required for Docker port forwarding but does not publish the port by itself.

## 2) Technical Debt

| Debt item | Why it exists | Where | Risk if ignored | Suggested fix |
|-----------|---------------|-------|-----------------|---------------|
| Startup schema migrations | Incremental compatibility uses repeated caught `ALTER TABLE` operations | `bot/database.py` | Drift is hard to audit/rollback; partial failures are obscured | Introduce numbered, transactional schema migrations and schema-version tests |
| Untyped HTTP bodies | Routes accept `dict` and validate fields manually | `api/server.py` | Inconsistent validation and undocumented OpenAPI request schemas | Add Pydantic request/response models by domain |
| Duplicate/legacy persistence paths | `messages` coexists with richer `conversation_messages`; historical tables are described as retained | `bot/database.py`, `docs/database.md` | Extra writes and unclear source-of-truth semantics | Measure readers, migrate remaining use, then retire legacy paths safely |
| Frontend dependency audit findings | `npm ci` reports five findings (1 low, 2 moderate, 2 high) in the resolved dependency graph | `frontend/package-lock.json`; 2026-07-11 verification output | Vulnerable transitive packages may remain until reviewed | Run `npm audit`, assess reachable paths, and update without using an unreviewed forced upgrade |
| No automated style/type gates | No formatter, linter, or explicit TS type-check script | `frontend/package.json`, repository configs | Inconsistency and type regressions accumulate | Add narrowly configured tools and CI gates |
| Documentation drift | Database docs state no explicit indexes, while schema creates conversation/tool indexes | `docs/database.md`, `bot/database.py` | Operators/developers make decisions from stale facts | Make schema documentation generated or test checked |

## 3) Security Concerns

| Risk | OWASP category | Evidence | Current mitigation | Gap |
|------|----------------|----------|--------------------|-----|
| API key is a shared long-lived secret | A02 Cryptographic Failures | `api/auth.py`, ignored `.env.prod` | 64-hex key, mode 600, HTTPS-only signed HttpOnly cookie, rotation invalidates sessions | No per-client keys, revocation list, or rate limiting; acceptable for the current single-user deployment |
| Raw integration errors returned/rendered | A04 / A05 | preset test responses include provider exception text | API keys are masked; OAuth HTML is escaped | Provider messages may still expose internal operational detail to authenticated admins |
| Plaintext credential/state files | A02 Cryptographic Failures | `config.py`, `bot/google_calendar.py` | ignored by Git; host/container filesystem boundary | No secrets manager, file-mode check, or documented rotation policy |
| Sensitive corpus retention | A09 Logging/Monitoring Failures | `bot/trace.py`, `bot/test_mode.py` | files are ignored by Git and reachable only through the private service/filesystem | Indefinite local retention is intentional; JSONL is explicitly outside DR scope and may be lost with the host |

## 4) Performance and Scaling Concerns

| Concern | Evidence | Current symptom | Scaling risk | Suggested improvement |
|---------|----------|-----------------|-------------|-----------------------|
| Synchronous SQLite in async paths | `Database._get_conn` and route/bot calls | No failure observed in current single-user tests | Long scans/writes block the only event loop | Profile first; add indexes and isolate DB work if latency grows |
| Broad scans and in-Python semantic scoring | `bot/database.py` conversation retrieval/scoring | Acceptable at current personal scale; no benchmark | The intentionally growing corpus can make retrieval increasingly expensive | Add query-plan/data-size benchmarks, indexes, and archival tiers while preserving raw data |
| Single process/global state | `main.py`, `api/server.py`, `config.py` | Matches current deployment | Multiple workers lose shared callbacks/locks/cache coherence; SQLite writes contend | Preserve one-worker constraint explicitly or externalize mutable coordination |
| Frontend fetches several endpoints separately | `frontend/src/app/App.tsx`, `Dashboard.tsx` | Build succeeds; no latency metric | Additional round trips and repeated rerenders as dashboard grows | Measure waterfall; batch only demonstrated hot paths |
| Large initial JS bundle | verified Vite build: 429.03 kB JS / 136.68 kB gzip | No load-time metric | Slower cold loads over remote/mobile links | Lazy-load admin/traces/heavy charts and measure bundle impact |

## 5) Fragile/High-Churn Areas

90-day history was measured with `git log --since='90 days ago' --name-only` on 2026-07-12 UTC.

| Area | Why fragile | Churn signal | Safe change strategy |
|------|-------------|-------------|----------------------|
| `bot/prompts.py` | Cache-sensitive prompt rendering and many context tiers | 34 file appearances | Preserve golden parity tests; add provider payload snapshots |
| `bot/discord_bot.py` | Transport, commands, auth filtering, OAuth flow, persistence | 26 appearances; 840 lines | Add message/command characterization tests before extraction |
| `bot/ai_engine_base.py` | Shared prompt/tool loop affects every provider | 25 appearances; 543 lines | Test tool rounds, failure normalization, and fallback contract |
| `bot/database.py` | Schema, migrations, and all domains share one class | 26 appearances; 1,618 lines | Back up DB; test migrations and each extracted repository |
| `bot/tools.py` / `api/server.py` | Broad mutation surfaces into the same database | 18 / 20 appearances | Add contract/API tests and authorization before structural changes |
| `frontend/src/app/App.tsx` | Top-level routing and dashboard data orchestration | 17 appearances; 450 lines | Add component/API mocking tests before splitting state hooks |

## 6) Confirmed Intent Decisions

1. Automatic database/trace/R2 archiving or compaction is not an existing feature and has no requested threshold. Retain the current data-preserving behavior and reconsider only after an observed capacity or performance issue.
2. Versioned releases are the authoritative production workflow: push a version tag, let `.github/workflows/release.yml` publish immutable versioned GHCR images, then deploy an explicit `VERSION` with `make deploy`. The repository does not currently create GitHub Release objects; “release” means the tag-triggered GHCR workflow evidenced in source.

## 7) Evidence

- Repository scan plus `git log --since='90 days ago' --name-only` and source line-count terminal output captured during the 2026-07-12 documentation run
- `frontend/vite.config.ts`
- `Dockerfile`
- `api/server.py`
- `bot/database.py`
- `bot/discord_bot.py`
- `bot/trace.py`
- `requirements.txt`
- `.github/workflows/release.yml`
- `compose.yaml`
- `.env.prod`
- `docs/deploy.md`
