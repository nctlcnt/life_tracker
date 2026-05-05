#!/usr/bin/env bash
# Pull latest DB snapshot from Cloudflare R2 (via litestream) into ./data/.
# Existing local DB is renamed with a .bak-<ts> suffix so nothing is lost.
#
# Required env vars (put in .env.dev at repo root):
#   R2_BUCKET                       e.g. life-tracker
#   R2_ENDPOINT                     e.g. https://<account>.r2.cloudflarestorage.com
#   LITESTREAM_ACCESS_KEY_ID
#   LITESTREAM_SECRET_ACCESS_KEY
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env.dev ]; then
  set -a
  # shellcheck disable=SC1091
  . .env.dev
  set +a
fi

: "${R2_BUCKET:?R2_BUCKET not set (put it in .env.dev)}"
: "${R2_ENDPOINT:?R2_ENDPOINT not set (put it in .env.dev)}"
: "${LITESTREAM_ACCESS_KEY_ID:?LITESTREAM_ACCESS_KEY_ID not set (put it in .env.dev)}"
: "${LITESTREAM_SECRET_ACCESS_KEY:?LITESTREAM_SECRET_ACCESS_KEY not set (put it in .env.dev)}"

if ! command -v litestream >/dev/null 2>&1; then
  echo "❌ litestream 未安装。macOS: brew install benbjohnson/litestream/litestream" >&2
  exit 1
fi

mkdir -p ./data
DB="./data/life_tracker.db"

if [ -f "$DB" ]; then
  ts=$(date +%Y%m%d-%H%M%S)
  backup="${DB}.bak-${ts}"
  echo "📦 备份本地 DB → ${backup}"
  mv "$DB" "$backup"
fi

echo "⬇️  从 R2 拉取最新备份…"
litestream restore -config litestream.dev.yml "$DB"
echo "✅ 完成: $DB"
