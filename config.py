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
        ("discord", "channel_id"),
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
# Bot 只在这个 channel 里听消息、推主动消息。prod / staging 各自配置不同 id。
CHANNEL_ID: int = int(_cfg.get("discord", {}).get("channel_id", 0) or 0)

# ── AI Presets ─────────────────────────────────────────────────────────
# config.json 里维护一张 presets 表，每条 preset 是一套
# { provider, api_key, base_url, model, use_v1_suffix }
# 运行时通过 /model、/fallback 斜杠命令切换，状态持久化到 data/active_preset.json


@dataclass
class Preset:
    name: str
    provider: str   # claude / openai / relay / gemini
    api_key: str
    base_url: str   # 仅 relay 需要
    model: str
    note: str = ""
    use_v1_suffix: bool = True


_ALLOWED_PROVIDERS: set[str] = {"claude", "openai", "relay", "gemini"}


def _build_preset(name: str, raw: dict) -> Preset:
    return Preset(
        name=name,
        provider=raw.get("provider", "claude"),
        api_key=raw.get("api_key", ""),
        base_url=raw.get("base_url", ""),
        model=raw.get("model", ""),
        note=raw.get("note", ""),
        use_v1_suffix=bool(raw.get("use_v1_suffix", True)),
    )


_ai = _cfg["ai"]
_raw_presets: dict = _ai.get("presets", {})
PRESETS: dict[str, Preset] = {
    _name: _build_preset(_name, _p) for _name, _p in _raw_presets.items()
}

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


# ── Preset 增删改 ──────────────────────────────────────────────────────
# 三个写入函数都会：① 改 config.json 的 ai.presets 段（其他顶层字段不动）
# ② 调 reload_presets() 把磁盘内容重读进 PRESETS dict，本进程立刻生效
# 跨机/跨进程的同步靠人工（用户偏好手动 scp config.json 比自动同步更稳）。

def reload_presets() -> None:
    """重读 config.json 的 ai.presets，刷新内存中的 PRESETS dict。
    保留 dict 对象身份（mutate in-place），其他模块持有的引用无需重新 import。"""
    with open(_CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    raw = cfg.get("ai", {}).get("presets", {}) or {}
    PRESETS.clear()
    for name, p in raw.items():
        PRESETS[name] = _build_preset(name, p)


def add_preset(name: str, provider: str, api_key: str, base_url: str,
               model: str, note: str = "", use_v1_suffix: bool = True) -> None:
    name = name.strip()
    if not name:
        raise ValueError("name required")
    if name in PRESETS:
        raise ValueError(f"preset already exists: {name}")
    provider = provider.strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise ValueError(f"invalid provider: {provider} (allowed: {sorted(_ALLOWED_PROVIDERS)})")
    if not api_key.strip():
        raise ValueError("api_key required")
    if not model.strip():
        raise ValueError("model required")
    if provider == "relay" and not base_url.strip():
        raise ValueError("base_url required for relay provider")

    with open(_CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    presets = cfg.setdefault("ai", {}).setdefault("presets", {})
    presets[name] = {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "note": note,
        "use_v1_suffix": bool(use_v1_suffix),
    }
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    reload_presets()


def update_preset(name: str, **fields) -> None:
    """更新现有 preset 的字段。允许的字段：
    provider / api_key / base_url / model / note / use_v1_suffix。
    api_key 传空字符串 = 不动（仅当显式传 None 跳过）。不支持改名。"""
    if name not in PRESETS:
        raise ValueError(f"unknown preset: {name}")

    with open(_CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    presets = cfg.setdefault("ai", {}).setdefault("presets", {})
    entry = presets.get(name, {})

    allowed = {"provider", "api_key", "base_url", "model", "note", "use_v1_suffix"}
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"field not editable: {k}")
        if v is None:
            continue
        if k == "provider":
            v = v.strip().lower()
            if v not in _ALLOWED_PROVIDERS:
                raise ValueError(f"invalid provider: {v}")
        elif k == "use_v1_suffix":
            v = bool(v)
        entry[k] = v

    final_provider = entry.get("provider", "claude")
    if final_provider == "relay" and not entry.get("base_url", "").strip():
        raise ValueError("base_url required for relay provider")
    if not entry.get("model", "").strip():
        raise ValueError("model required")
    if not entry.get("api_key", "").strip():
        raise ValueError("api_key required")

    presets[name] = entry
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    reload_presets()


def delete_preset(name: str) -> None:
    """删 preset。当前 active 拒绝；是 fallback 的话先自动 clear fallback 再删。"""
    if name not in PRESETS:
        raise ValueError(f"unknown preset: {name}")
    if name == _ACTIVE_NAME:
        raise ValueError(f"cannot delete active preset: {name}")
    if name == _FALLBACK_NAME:
        set_fallback(None)

    with open(_CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    presets = cfg.setdefault("ai", {}).setdefault("presets", {})
    presets.pop(name, None)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    reload_presets()


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
# Google Geocoding API key（/weather <address> 用，留空则按地址查天气会失败）
WEATHER_GEOCODING_API_KEY: str = _weather.get("geocoding_api_key", "")

# ── Google Calendar ───────────────────────────────────────────────────
# Optional read-only calendar integration. Missing credentials degrade at runtime.
def _resolve_config_path(path: str) -> str:
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(__file__), path)


_google_calendar = _cfg.get("google_calendar", {})
GCAL_ENABLED: bool = bool(_google_calendar.get("enabled", False))
GCAL_CLIENT_SECRET_FILE: str = _resolve_config_path(
    _google_calendar.get("client_secret_file", "data/google_oauth_client.json")
)
GCAL_TOKEN_FILE: str = _resolve_config_path(
    _google_calendar.get("token_file", "data/google_calendar_token.json")
)
GCAL_OAUTH_REDIRECT_URI: str = str(_google_calendar.get("oauth_redirect_uri", "")).strip()
GCAL_CALENDAR_ID: str = _google_calendar.get("calendar_id", "primary")
GCAL_CALENDAR_IDS: list[str] = [
    str(x).strip()
    for x in _google_calendar.get("calendar_ids", [])
    if str(x).strip()
]
GCAL_INCLUDE_ALL_READABLE: bool = bool(_google_calendar.get("include_all_readable", False))
GCAL_DISABLED_CALENDAR_IDS: list[str] = [
    str(x).strip()
    for x in _google_calendar.get("disabled_calendar_ids", [])
    if str(x).strip()
]


def _write_google_calendar_config(**fields) -> None:
    with open(_CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    section = cfg.setdefault("google_calendar", {})
    section.update(fields)
    # 先整体序列化成字符串，再原地写：这样「序列化出错时文件已被 truncate」的
    # 损坏窗口就不存在了。不用 临时文件 + os.replace，因为 config.json 在 Docker 里
    # 是单文件 bind-mount，往挂载点 rename 会 EBUSY 失败 / 让 host 文件不再更新。
    data = json.dumps(cfg, ensure_ascii=False, indent=2)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def set_gcal_disabled_calendar_ids(calendar_ids: list[str]) -> None:
    global GCAL_DISABLED_CALENDAR_IDS
    clean: list[str] = []
    seen: set[str] = set()
    for calendar_id in calendar_ids:
        cid = str(calendar_id).strip()
        if cid and cid not in seen:
            clean.append(cid)
            seen.add(cid)
    GCAL_DISABLED_CALENDAR_IDS = clean
    _write_google_calendar_config(disabled_calendar_ids=clean)

# ── 日志 ───────────────────────────────────────────────────────────────
_log = _cfg.get("log", {})
LOG_LEVEL: str = _log.get("level", "INFO")
LOG_FILE: str | None = _log.get("file") or None  # null → 仅输出 stdout
