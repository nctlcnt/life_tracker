import json
import os
import sys

_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

if not os.path.exists(_CONFIG_FILE):
    print(
        f"[ERROR] 找不到配置文件 {_CONFIG_FILE}\n"
        "请复制模板并填写配置：\n"
        "  cp config.example.json config.json",
        file=sys.stderr,
    )
    sys.exit(1)

with open(_CONFIG_FILE, encoding="utf-8") as _f:
    _cfg: dict = json.load(_f)

# ── 必填字段校验 ───────────────────────────────────────────────────────────
_REQUIRED_PATHS: list[tuple[str, ...]] = [
    ("discord", "token"),
    ("discord", "allowed_user_id"),
    ("ai", "api_key"),
]
for _keys in _REQUIRED_PATHS:
    _val = _cfg
    for _k in _keys:
        _val = _val.get(_k, "") if isinstance(_val, dict) else ""
    if not _val:
        print(
            f"[ERROR] config.json 缺少必填项: {'.'.join(_keys)}\n"
            "请参考 config.example.json 填写完整配置。",
            file=sys.stderr,
        )
        sys.exit(1)

# ── Discord ────────────────────────────────────────────────────────────
DISCORD_TOKEN: str = _cfg["discord"]["token"]
ALLOWED_USER_ID: int = int(_cfg["discord"]["allowed_user_id"])

# ── AI 主引擎 ───────────────────────────────────────────────────────────
# provider: claude / relay / gemini
_ai = _cfg["ai"]
AI_PROVIDER: str = _ai.get("provider", "claude")
AI_API_KEY: str = _ai.get("api_key", "")
AI_BASE_URL: str = _ai.get("base_url", "")        # 仅 relay 需要
CHAT_MODEL: str = _ai.get("chat_model", "claude-opus-4-6")
POLL_MODEL: str = _ai.get("poll_model", "claude-3-5-sonnet-latest")

# ── AI Fallback（可选）─────────────────────────────────────────────────
# 主引擎 API 调用失败时自动切换；留空表示不启用
_fb = _ai.get("fallback", {})
AI_FALLBACK_PROVIDER: str = _fb.get("provider", "")
AI_FALLBACK_API_KEY: str = _fb.get("api_key", "")
AI_FALLBACK_BASE_URL: str = _fb.get("base_url", "")   # 仅 relay 需要
AI_FALLBACK_CHAT_MODEL: str = _fb.get("chat_model", "")  # 留空则继承主引擎模型名
AI_FALLBACK_POLL_MODEL: str = _fb.get("poll_model", "")  # 留空则继承主引擎模型名

# ── 服务器 ─────────────────────────────────────────────────────────────
API_PORT: int = int(_cfg.get("server", {}).get("port", 8080))

# ── 数据库 ─────────────────────────────────────────────────────────────
DB_PATH: str = os.path.join(os.path.dirname(__file__), "data", "life_tracker.db")

# ── 随机轮询间隔范围（秒）──────────────────────────────────────────────
_poll = _cfg.get("poll", {})
POLL_MIN_SECONDS: int = int(_poll.get("min_seconds", 60))
POLL_MAX_SECONDS: int = int(_poll.get("max_seconds", 3600))

# ── 日志 ───────────────────────────────────────────────────────────────
_log = _cfg.get("log", {})
LOG_LEVEL: str = _log.get("level", "INFO")
LOG_FILE: str | None = _log.get("file") or None  # null → 仅输出 stdout
