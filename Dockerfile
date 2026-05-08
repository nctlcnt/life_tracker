# ─── Stage 1: Build frontend ──────────────────────────────────────────────────
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend

RUN npm install -g pnpm

# Install deps first (cached layer, only re-runs when package.json changes)
COPY frontend/package.json ./
RUN pnpm install

# Build
COPY frontend/ ./
RUN pnpm run build


# ─── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

WORKDIR /app

# curl is needed for HEALTHCHECK; tzdata supplies IANA zone files for runtime TZ switching
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

# Inject freshly-built frontend (overwrites any stale local dist)
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# SQLite data directory (mount a volume here in production)
RUN mkdir -p data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf "http://localhost:${API_PORT:-8080}/api/health" || exit 1

CMD ["python", "main.py"]
