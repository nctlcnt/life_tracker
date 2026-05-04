import json
import os
import sys
from dataclasses import dataclass

_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
_STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "active_preset.json")

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
DISCORD_TOKEN: str = _cfg["discord"]["token"]
ALLOWED_USER_ID: int = int(_cfg["discord"]["allowed_user_id"])

# ── AI Presets ─────────────────────────────────────────────────────────
# config.json 里维护一张 presets 表，每条 preset 是一套 { provider, api_key, base_url, model }
# 运行时通过 /model、/fallback 斜杠命令切换，状态持久化到 data/active_preset.json


@dataclass
class Preset:
    name: str
    provider: str   # claude / relay / gemini / openai
    api_key: str
    base_url: str   # 仅 relay 需要
    model: str
    notes: str = ""


SUPPORTED_PROVIDERS: tuple[str, ...] = ("claude", "relay", "gemini", "openai")


_ai = _cfg["ai"]
_raw_presets: dict = _ai.get("presets", {})
PRESETS: dict[str, Preset] = {}


def _rebuild_presets() -> None:
    PRESETS.clear()
    for name, p in _cfg["ai"]["presets"].items():
        PRESETS[name] = Preset(
            name=name,
            provider=p.get("provider", "claude"),
            api_key=p.get("api_key", ""),
            base_url=p.get("base_url", ""),
            model=p.get("model", ""),
            notes=p.get("notes", ""),
        )


_rebuild_presets()

_DEFAULT_PRESET: str = _ai.get("default_preset", "")
_DEFAULT_FALLBACK: str = _ai.get("default_fallback", "")

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


def get_active_name() -> str:
    return _ACTIVE_NAME


def get_fallback_name() -> str | None:
    return _FALLBACK_NAME


# ── 配置写回 ───────────────────────────────────────────────────────────
def _save_config() -> None:
    """把 _cfg 整体写回 config.json（保留 discord/weather 等其他字段）。"""
    tmp_path = _CONFIG_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _CONFIG_FILE)


def add_preset(
    name: str,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    notes: str = "",
) -> Preset:
    name = (name or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    if name in PRESETS:
        raise ValueError(f"preset '{name}' 已存在")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"不支持的 provider: {provider}（可选: {', '.join(SUPPORTED_PROVIDERS)})")
    _cfg["ai"]["presets"][name] = {
        "provider": provider,
        "api_key": api_key or "",
        "base_url": base_url or "",
        "model": model or "",
        "notes": notes or "",
    }
    _save_config()
    _rebuild_presets()
    return PRESETS[name]


def update_preset(
    name: str,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    notes: str | None = None,
) -> Preset:
    if name not in PRESETS:
        raise ValueError(f"未知 preset: {name}")
    p = _cfg["ai"]["presets"][name]
    if provider is not None:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"不支持的 provider: {provider}（可选: {', '.join(SUPPORTED_PROVIDERS)})")
        p["provider"] = provider
    if api_key is not None:
        p["api_key"] = api_key
    if base_url is not None:
        p["base_url"] = base_url
    if model is not None:
        p["model"] = model
    if notes is not None:
        p["notes"] = notes
    _save_config()
    _rebuild_presets()
    return PRESETS[name]


def delete_preset(name: str) -> None:
    if name not in PRESETS:
        raise ValueError(f"未知 preset: {name}")
    if name == _ACTIVE_NAME:
        raise ValueError(f"不能删除当前主 preset '{name}'，请先切换主 preset")
    if name == _FALLBACK_NAME:
        raise ValueError(f"不能删除当前 fallback preset '{name}'，请先切换 fallback")
    del _cfg["ai"]["presets"][name]
    # 修正 config.json 里挂着的默认值，避免下次冷启失效
    if _cfg["ai"].get("default_preset") == name:
        _cfg["ai"]["default_preset"] = _ACTIVE_NAME
    if _cfg["ai"].get("default_fallback") == name:
        _cfg["ai"]["default_fallback"] = _FALLBACK_NAME or ""
    _save_config()
    _rebuild_presets()


# ── 服务器 ─────────────────────────────────────────────────────────────
API_PORT: int = int(_cfg.get("server", {}).get("port", 8080))

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
