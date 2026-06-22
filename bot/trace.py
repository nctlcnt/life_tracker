"""
AI 对话 trace 写入模块。

每次 chat / scheduled_action / simple_completion 调用产生一条 trace，
完整记录 prompt 组成（PromptParts 4 层 block）、消息历史、每轮模型输出
（含 <think> 原文）、工具调用与结果，写入 data/ai_traces/<YYYY-MM-DD>.jsonl。

设计：
- contextvars 维护当前 trace 上下文，引擎之间不需要传递 trace_id
- 写失败永远不抛异常——主流程不能因为 trace 崩
- 每行 = 一条完整 call（包含所有 rounds）
- 文件按 ts 的日期切分，便于按天浏览和归档
"""
from __future__ import annotations

import contextvars
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from bot.prompts import PromptParts, TOOL_POST_HINTS
from bot.logger import get_logger

logger = get_logger(__name__)

_TRACE_DIR = Path("data/ai_traces")

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ai_trace_current", default=None
)


# ── 序列化辅助 ──────────────────────────────────────────────────


def _coerce(obj: Any) -> Any:
    """把 SDK 返回对象（Anthropic ContentBlock / Gemini pydantic 等）转成 JSON-friendly 结构。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _coerce(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce(x) for x in obj]
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(exclude_none=True, mode="json")
        except Exception:
            pass
    if hasattr(obj, "type"):
        d = {"type": getattr(obj, "type", None)}
        for attr in ("text", "id", "name", "input", "tool_use_id", "content"):
            if hasattr(obj, attr):
                v = getattr(obj, attr)
                if v is not None:
                    d[attr] = _coerce(v)
        return d
    return str(obj)


def _serialize_prompt(p: PromptParts | None) -> dict | None:
    if p is None:
        return None
    return {
        "block1_static": {
            "identity": p.identity,
            "user_model": p.user_model,
            "system_mechanics": p.system_mechanics,
            "communication": p.communication,
            "protocols": p.protocols,
            "tools_section": p.tools or "",
        },
        "block2_projects": p.projects,
        "block3_memories": p.memories,
        "block4_dynamic": {
            "today_timeline": p.today_timeline,
            "pending_reminders": p.pending_reminders,
            "deadlines": p.deadlines,
            "weather": p.weather,
            "calendar": p.calendar,
        },
    }


def _content_to_str(c: Any) -> str:
    """把 message.content（可能是 str 或 list of blocks）压成纯字符串供 user_message 字段用。"""
    if isinstance(c, str):
        return c
    try:
        return json.dumps(c, ensure_ascii=False, default=str)
    except Exception:
        return str(c)


# ── 写入 API ────────────────────────────────────────────────────


def start(trigger: str, model: str, provider: str,
          prompt_parts: PromptParts | None,
          messages: list[dict] | None = None) -> dict:
    """初始化当前 async context 的 trace。返回 entry dict（调试可用）。

    trigger: chat / poll / reminder / bedtime / morning / oneshot
    messages: 已规范化的消息列表（最后一条 = 本轮触发的 user message）
    """
    coerced_messages = [_coerce(m) for m in (messages or [])]
    user_message = ""
    history: list[dict] = []
    if coerced_messages:
        history = coerced_messages[:-1]
        last = coerced_messages[-1]
        user_message = _content_to_str(last.get("content"))
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now().isoformat(timespec="seconds"),
        "trigger": trigger,
        "model": model,
        "provider": provider,
        "prompt_parts": _serialize_prompt(prompt_parts),
        "user_message": user_message,
        "history": history,
        "rounds": [],
        "final_text": None,
        "error": None,
    }
    _current.set(entry)
    return entry


def add_round(*, raw_output: str, think: str, visible_text: str,
              tool_calls: list[dict] | None = None,
              tool_results: list[dict] | None = None,
              usage: dict | None = None,
              stop_reason: str | None = None) -> None:
    """追加一轮模型响应到当前 trace。tool_calls / tool_results 元素需为 dict。"""
    entry = _current.get()
    if entry is None:
        return
    calls = _coerce(tool_calls or [])
    results = _coerce(tool_results or [])
    called_names = [c.get("name", "") for c in calls if isinstance(c, dict)]
    post_hints = [n for n in called_names if n in TOOL_POST_HINTS]
    entry["rounds"].append({
        "n": len(entry["rounds"]) + 1,
        "raw_output": raw_output,
        "think": think,
        "visible_text": visible_text,
        "tool_calls": calls,
        "tool_results": results,
        "post_hints_triggered": post_hints,
        "usage": usage,
        "stop_reason": stop_reason,
    })


def finalize(final_text: str | None = None, error: str | None = None) -> None:
    """终结当前 trace，落盘并清空 context。"""
    entry = _current.get()
    if entry is None:
        return
    entry["final_text"] = final_text
    entry["error"] = error
    _write(entry)
    _current.set(None)


def _write(entry: dict) -> None:
    try:
        _TRACE_DIR.mkdir(parents=True, exist_ok=True)
        day = entry["ts"][:10]  # YYYY-MM-DD
        path = _TRACE_DIR / f"{day}.jsonl"
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.exception("trace write failed")


def is_active() -> bool:
    return _current.get() is not None


# ── 读取 API（FastAPI 用）─────────────────────────────────────


def list_dates() -> list[dict]:
    """返回所有有 trace 的日期，最新在前。"""
    if not _TRACE_DIR.exists():
        return []
    out = []
    for p in sorted(_TRACE_DIR.glob("*.jsonl"), reverse=True):
        try:
            count = sum(1 for _ in p.open(encoding="utf-8"))
        except Exception:
            count = 0
        out.append({"date": p.stem, "count": count})
    return out


def read_day(date: str, trigger: str | None = None, limit: int = 100) -> list[dict]:
    """读某一天的 trace，最新在前。"""
    path = _TRACE_DIR / f"{date}.jsonl"
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if trigger and entry.get("trigger") != trigger:
                    continue
                entries.append(entry)
    except Exception:
        logger.exception("trace read failed")
    entries.reverse()
    return entries[:limit]


def list_recent_tool_calls(
    *,
    limit: int = 50,
    date: str | None = None,
    trigger: str | None = None,
    name: str | None = None,
) -> list[dict]:
    """从 trace 文件抽取最近的工具调用，最新在前。

    返回元素只包含便于浏览器 GET 查看和排查的信息，不展开完整 prompt/history。
    """
    if date:
        paths = [_TRACE_DIR / f"{date}.jsonl"]
    elif _TRACE_DIR.exists():
        paths = sorted(_TRACE_DIR.glob("*.jsonl"), reverse=True)
    else:
        paths = []

    out: list[dict] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            logger.exception("trace tool-call read failed")
            continue

        for line in reversed(lines):
            if len(out) >= limit:
                return out
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if trigger and entry.get("trigger") != trigger:
                continue

            rounds = entry.get("rounds") or []
            for round_entry in reversed(rounds):
                calls = round_entry.get("tool_calls") or []
                for call in reversed(calls):
                    if not isinstance(call, dict):
                        continue
                    call_name = call.get("name") or ""
                    if name and call_name != name:
                        continue
                    out.append({
                        "trace_id": entry.get("id"),
                        "ts": entry.get("ts"),
                        "trigger": entry.get("trigger"),
                        "provider": entry.get("provider"),
                        "model": entry.get("model"),
                        "round": round_entry.get("n"),
                        "tool": call_name,
                        "args": call.get("input") or {},
                        "tool_call_id": call.get("id"),
                        "visible_text": round_entry.get("visible_text") or "",
                    })
                    if len(out) >= limit:
                        return out
    return out
