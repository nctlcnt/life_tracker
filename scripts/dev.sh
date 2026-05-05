#!/usr/bin/env bash
# 本地前端调试一条龙：先从 R2 拉最新 DB，再以 api-only 模式起 FastAPI。
# 想跳过拉取直接起服务用：python main.py --api-only
set -euo pipefail

cd "$(dirname "$0")/.."

./scripts/dev_pull.sh
exec python main.py --api-only
