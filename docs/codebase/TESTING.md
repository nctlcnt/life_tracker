# Testing Patterns

## 1) Test Stack and Commands

- Primary framework: pytest `>=8.0`.
- Assertion/mocking tools: Python `assert`, pytest parametrization, `monkeypatch`, and `tmp_path`; no separate mocking library.
- Latest verified baseline (2026-07-17 UTC): 57 tests passed in 2.54 seconds; the Vite production build and LT-139 production authentication smoke tests also passed.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_scheduler_checkins.py -q
cd frontend && npm run build
# Integration/E2E command: [TODO] none configured
# Coverage command: [TODO] none configured
```

## 2) Test Layout

- Tests are centralized under `tests/`; `pytest.ini` sets `testpaths = tests`.
- Files/functions use `test_*.py` and `test_*`; `tests/legacy_reference.py` is a frozen comparison helper rather than a collected test file.
- There are no global fixture/setup files. Test modules create their own helpers and data.
- `scripts/test_api.py` is a manual API smoke script, not part of pytest discovery.

## 3) Test Scope Matrix

| Scope | Covered? | Typical target | Notes |
|-------|----------|----------------|-------|
| Unit | yes, narrow | Prompt rendering, parsing invariants, scheduler time calculation | `tests/test_prompt_render.py`, `tests/test_template_edge.py`, `tests/test_scheduler_checkins.py` |
| Component/integration | partial | Scheduler plus a temporary real SQLite database | External AI and Discord are replaced with local callbacks/monkeypatches |
| API integration | partial | Authentication middleware, login/session/logout, OAuth exemption | `tests/test_api_auth.py`; broader domain routes still rely on manual smoke checks |
| Frontend unit/component | no | React components | No frontend test dependency or script |
| End-to-end | no | Discord-to-AI-to-DB/UI user flows | No configured harness |

## 4) Mocking and Isolation Strategy

- Scheduler tests monkeypatch module functions/randomness and inject an async no-op sender.
- Database isolation uses a new SQLite database under `tmp_path` for each helper invocation.
- Prompt tests use committed default prompt JSON and a frozen legacy renderer as a golden oracle.
- Tests do not call real Discord, AI, weather, Google, R2, or GHCR services.
- Common failure risk: modules import `config` at collection time, so tests depend on a usable local `config.json`; there is no dedicated test config fixture.

## 5) Coverage and Quality Signals

- Coverage tool + threshold: [TODO] none configured.
- Current reported coverage: [TODO] not measured.
- CI test gate: absent; the release workflow checks out and builds/pushes a container without a separate test step.
- High-risk gaps: AI provider adapters/fallbacks, database migrations/queries, Discord filtering and response flow, Google Calendar behavior, frontend state/mutations, and deployment restore behavior. LT-134 must add provider contract tests before deleting adapters.
- The frontend build verifies bundling but is not a behavioral test and currently emits a 429.03 kB JavaScript bundle (136.68 kB gzip).

## 6) Evidence

- `requirements-dev.txt`
- `pytest.ini`
- `tests/test_prompt_render.py`
- `tests/test_template_edge.py`
- `tests/test_scheduler_checkins.py`
- `tests/test_api_auth.py`
- `scripts/test_api.py`
- `frontend/package.json`
- `.github/workflows/release.yml`
