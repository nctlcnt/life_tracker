"""
进程时区管理：通过 os.environ['TZ'] + time.tzset() 控制 datetime.now() 等本地时间调用。
支持运行时切换（/tz 命令），状态持久化到 data/active_tz.json。

依赖 POSIX time.tzset()，仅 Linux/macOS 可用；依赖系统 tzdata（容器需 apt-get install tzdata）。
"""
import json
import os
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.logger import get_logger

logger = get_logger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "active_tz.json")
_current: str = "UTC"


def _apply(name: str) -> None:
    """校验 + 写 env + tzset。无效名抛 ValueError。"""
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"unknown timezone: {name}") from e
    os.environ["TZ"] = name
    if hasattr(time, "tzset"):
        time.tzset()
    else:
        logger.warning("time.tzset() 在当前平台不可用，TZ 切换可能不生效")


def init_timezone(default: str) -> str:
    """启动时调用：从持久化文件读上次设的 TZ；没有就用 default。返回实际生效的名字。"""
    global _current
    name = default
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f).get("name")
            if saved:
                name = saved
        except Exception as e:
            logger.warning(f"读取 {_STATE_FILE} 失败: {e}，用默认 {default}")
    try:
        _apply(name)
    except ValueError:
        logger.error(f"持久化的 TZ '{name}' 无效，回退到默认 {default}")
        name = default
        _apply(name)
    _current = name
    logger.info(f"🕐 时区已设置: {name} (当前本地时间 {time.strftime('%Y-%m-%d %H:%M:%S')})")
    return name


def get_timezone() -> str:
    return _current


def set_timezone(name: str) -> None:
    """运行时切换。无效名抛 ValueError；成功后持久化到 data/active_tz.json。"""
    global _current
    _apply(name)
    _current = name
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"name": name}, f, ensure_ascii=False, indent=2)
    logger.info(f"🕐 时区已切换: {name} (当前本地时间 {time.strftime('%Y-%m-%d %H:%M:%S')})")
