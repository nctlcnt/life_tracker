"""
测试模式模块
/start-test 开启后，捕获所有应用日志和发往 AI 服务商的完整 prompt payload，
写入单一 JSONL 文件；/stop-test 结束时以结束时间命名文件。

JSONL 中每行为一个 JSON 对象，type 字段区分两种条目：
  {"type": "log",       "ts": "...", "level": "INFO", "logger": "...", "msg": "..."}
  {"type": "ai_prompt", "ts": "...", "provider": "...", "model": "...",
   "round": N, "payload": {...}}
"""
import json
import logging
import time
from pathlib import Path

_SENTINEL = Path("data/.test_mode_active")   # 内容为开始时间戳，用于重启恢复
_LOG_DIR = Path("data/test_logs")

_active_handler: logging.Handler | None = None
_active_log_path: Path | None = None
_start_ts: str | None = None


# ── 自定义 Handler ──────────────────────────────────────────────────────────


class _JsonLineHandler(logging.Handler):
    """将每条 log record 以 JSON 行追加写入指定文件。"""

    def __init__(self, path: Path):
        super().__init__()
        self._path = path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "type": "log",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "msg": self.format(record),
            }
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            self.handleError(record)


# ── 公共接口 ────────────────────────────────────────────────────────────────


def is_active() -> bool:
    return _SENTINEL.exists()


def start() -> str:
    """开启测试模式。返回开始时间戳字符串。"""
    global _active_handler, _active_log_path, _start_ts

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    _SENTINEL.write_text(ts, encoding="utf-8")
    _start_ts = ts

    log_path = _LOG_DIR / f"test_{ts}.jsonl"
    handler = _JsonLineHandler(log_path)
    logging.getLogger("life_tracker").addHandler(handler)

    _active_handler = handler
    _active_log_path = log_path
    return ts


def stop() -> Path | None:
    """关闭测试模式，以结束时间重命名日志文件，返回最终路径。"""
    global _active_handler, _active_log_path, _start_ts

    if not is_active():
        return None

    end_ts = time.strftime("%Y%m%d_%H%M%S")

    # 移除并关闭 handler
    if _active_handler is not None:
        root = logging.getLogger("life_tracker")
        root.removeHandler(_active_handler)
        try:
            _active_handler.close()
        except Exception:
            pass
        _active_handler = None

    # 重命名日志文件
    final_path: Path | None = None
    if _active_log_path and _active_log_path.exists():
        final_path = _LOG_DIR / f"{end_ts}.jsonl"
        _active_log_path.rename(final_path)
    _active_log_path = None

    # 删除哨兵文件
    _SENTINEL.unlink(missing_ok=True)
    _start_ts = None

    return final_path


def ensure_handler_state() -> None:
    """在每次 AI 调用前调用，处理重启恢复场景。
    - 哨兵存在但 handler 未挂载 → 重新调用 start()
    - handler 已挂载但哨兵不存在 → 调用 stop() 清理
    """
    global _active_handler
    if _SENTINEL.exists() and _active_handler is None:
        start()
    elif not _SENTINEL.exists() and _active_handler is not None:
        stop()


def log_response(provider: str, model: str, response: dict, round_num: int = 1) -> None:
    """将 AI 返回的响应记录到测试日志。"""
    if not is_active() or _active_log_path is None:
        return
    entry = {
        "type": "ai_response",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "provider": provider,
        "model": model,
        "round": round_num,
        "response": response,
    }
    try:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with open(_active_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_prompt(provider: str, model: str, payload: dict, round_num: int = 1) -> None:
    """将完整 AI prompt payload 追加到测试日志。

    payload 直接传入 kwargs / request body dict，用 default=str 兜底处理
    Anthropic SDK ContentBlock 等非可序列化对象。
    """
    if not is_active() or _active_log_path is None:
        return
    entry = {
        "type": "ai_prompt",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "provider": provider,
        "model": model,
        "round": round_num,
        "payload": payload,
    }
    try:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with open(_active_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
