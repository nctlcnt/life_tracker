import json
import os
import sys
from dataclasses import dataclass

# api-only 模式跳过 Discord/AI 相关字段校验，便于纯前端调试启动
API_ONLY_MODE: bool = os.environ.get("LIFE_TRACKER_API_ONLY") == "1"

_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
_STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "active_preset.json")

if not os.path.exists(_CONFIG_FILE):
    if API_ONLY_MODE:
        # api-only 时容忍配置缺失，提供最小骨架让 FastAPI 跑起来
        _cfg = {"server": {}, "ai": {"presets": {}, "default_preset": ""}}
    else:
        print(
            f"[ERROR] 找不到配置文件 {_CONFIG_FILE}\n"
            "请复制模板并填写配置：\n"
            "  cp config.example.json config.json",
            file=sys.stderr,
        )
        sys.exit(1)
else:
    with open(_CONFIG_FILE, encoding="utf-8") as _f:
        _cfg: dict = json.load(_f)

# ── 必填字段校验 ───────────────────────────────────────────────────────────
# api-only 模式跳过 Discord / AI 校验，纯前端调试不需要这些字段
if not API_ONLY_MODE:
    _REQUIRED_PATHS: list[tuple[str, ...]] = [
        ("discord", "token"),
        ("discord", "allowed_user_id"),
        ("ai", "presets"),
        ("ai", "default_preset"),
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
DISCORD_TOKEN: str = _cfg.get("discord", {}).get("token", "")
ALLOWED_USER_ID: int = int(_cfg.get("discord", {}).get("allowed_user_id", 0) or 0)

# ── AI Presets ─────────────────────────────────────────────────────────
# config.json 里维护一张 presets 表，每条 preset 是一套 { provider, api_key, base_url, model }
# 运行时通过 /model、/fallback 斜杠命令切换，状态持久化到 data/active_preset.json


@dataclass
class Preset:
    name: str
    provider: str   # claude / relay / gemini
    api_key: str
    base_url: str   # 仅 relay 需要
    model: str


_ai = _cfg["ai"]
_raw_presets: dict = _ai.get("presets", {})
PRESETS: dict[str, Preset] = {}
for _name, _p in _raw_presets.items():
    PRESETS[_name] = Preset(
        name=_name,
        provider=_p.get("provider", "claude"),
        api_key=_p.get("api_key", ""),
        base_url=_p.get("base_url", ""),
        model=_p.get("model", ""),
    )

_DEFAULT_PRESET: str = _ai.get("default_preset", "")
_DEFAULT_FALLBACK: str = _ai.get("default_fallback", "")

if not API_ONLY_MODE:
    if _DEFAULT_PRESET not in PRESETS:
        print(
            f"[ERROR] default_preset '{_DEFAULT_PRESET}' 不在 presets 列表里\n"
            f"已知 presets: {list(PRESETS.keys())}",
            file=sys.stderr,
        )
        sys.exit(1)

    if _DEFAULT_FALLBACK and _DEFAULT_FALLBACK not in PRESETS:
        print(
            f"[ERROR] default_fallback '{_DEFAULT_FALLBACK}' 不在 presets 列表里\n"
            f"已知 presets: {list(PRESETS.keys())}",
            file=sys.stderr,
        )
        sys.exit(1)


# ── 运行时状态 ─────────────────────────────────────────────────────────
# 启动时读 state 文件；不存在就用 config.json 里的 default
def _load_state() -> tuple[str, str | None]:
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                s = json.load(f)
            active = s.get("active") or _DEFAULT_PRESET
            fb = s.get("fallback")
            if active not in PRESETS:
                active = _DEFAULT_PRESET
            if fb is not None and fb not in PRESETS:
                fb = _DEFAULT_FALLBACK or None
            return active, fb
        except Exception as e:
            print(f"[WARN] 读取 {_STATE_FILE} 失败: {e}，用默认 preset", file=sys.stderr)
    return _DEFAULT_PRESET, (_DEFAULT_FALLBACK or None)


_ACTIVE_NAME: str
_FALLBACK_NAME: str | None
_ACTIVE_NAME, _FALLBACK_NAME = _load_state()


def _save_state() -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"active": _ACTIVE_NAME, "fallback": _FALLBACK_NAME},
            f, ensure_ascii=False, indent=2,
        )


def get_active() -> Preset:
    return PRESETS[_ACTIVE_NAME]


def get_fallback() -> Preset | None:
    if not _FALLBACK_NAME:
        return None
    return PRESETS.get(_FALLBACK_NAME)


def set_active(name: str) -> None:
    global _ACTIVE_NAME
    if name not in PRESETS:
        raise ValueError(f"unknown preset: {name}")
    _ACTIVE_NAME = name
    _save_state()


def set_fallback(name: str | None) -> None:
    global _FALLBACK_NAME
    if name is not None and name not in PRESETS:
        raise ValueError(f"unknown preset: {name}")
    _FALLBACK_NAME = name
    _save_state()


def list_presets() -> list[str]:
    return list(PRESETS.keys())


# ── 服务器 ─────────────────────────────────────────────────────────────
API_PORT: int = int(_cfg.get("server", {}).get("port", 8080))

# ── 时区 ───────────────────────────────────────────────────────────────
# 进程默认时区（IANA 名）。运行时可通过 /tz 命令切换并持久化到 data/active_tz.json。
TIMEZONE: str = _cfg.get("timezone", "Australia/Sydney")

# ── 数据库 ─────────────────────────────────────────────────────────────
DB_PATH: str = os.path.join(os.path.dirname(__file__), "data", "life_tracker.db")

# ── 天气 ───────────────────────────────────────────────────────────────
# tomorrow.io API；api_key 空时天气模块会静默降级（返回 None，不影响主流程）
_weather = _cfg.get("weather", {})
WEATHER_API_KEY: str = _weather.get("api_key", "")
WEATHER_LOCATION: str = _weather.get("location", "-33.8688,151.2093")  # 默认悉尼

# ── 日志 ───────────────────────────────────────────────────────────────
_log = _cfg.get("log", {})
LOG_LEVEL: str = _log.get("level", "INFO")
LOG_FILE: str | None = _log.get("file") or None  # null → 仅输出 stdout
