"""聊天上下文 token 窗口装配（LT-135）。

窗口 = [compact 摘要（若有）] + 明文尾巴。单阈值 + 派生保护：

- 阈值 config.CONTEXT_COMPACT_THRESHOLD_TOKENS（默认 20k，唯一可调数字）：
  每次装配时评估 estimate(摘要 + 全部明文)，达到即标记 needs_compact，
  由调用方后台触发 compact（本模块只做纯装配，不发起 AI 调用）。
- 明文保留 = 阈值 × KEEP_RATIO(0.4)：compact 的切点——折叠更老的部分，
  保留最近原话的语气与细节。
- 硬上限 = 阈值 × HARD_CAP_RATIO(1.2)：compact 失效期间的保险丝，
  超过按 token 从最老明文开始硬裁（摘要永不裁）。

摘要状态持久化在 app_state（STATE_KEY_PREFIX + channel_id），JSON：
{"summary", "upto_message_id", "model", "updated_at"}——重启不丢。
compact 期间明文不动、照常增长；compact 完成写回新状态后，
下一次装配自然呈现「新摘要 + 保留明文 + 期间新消息」，即原子切换。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import config
from bot.logger import get_logger
from bot.memory.markdown_repository import estimate_tokens

logger = get_logger(__name__)

KEEP_RATIO = 0.4        # compact 后保留的明文预算 / 阈值
HARD_CAP_RATIO = 1.2    # 硬裁上限 / 阈值
# 单条消息的结构开销（role/分隔等），与 estimate_tokens 一样只求保守量级
PER_MESSAGE_OVERHEAD_TOKENS = 4

SUMMARY_LABEL = "【早前对话摘要（系统自动生成，供你参考上下文）】"
STATE_KEY_PREFIX = "context_summary:"


def _state_key(channel_id: str) -> str:
    return f"{STATE_KEY_PREFIX}{channel_id}"


def load_summary_state(db, channel_id: str) -> dict | None:
    """读取持久化的摘要状态；不存在/损坏返回 None。"""
    raw = db.get_state(_state_key(str(channel_id)))
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"context summary state 损坏，忽略: {raw[:80]}")
        return None
    if not isinstance(state, dict) or not state.get("summary"):
        return None
    return state


def save_summary_state(db, channel_id: str, *, summary: str,
                       upto_message_id: int, model: str,
                       updated_at: str) -> None:
    db.set_state(_state_key(str(channel_id)), json.dumps({
        "summary": summary,
        "upto_message_id": int(upto_message_id),
        "model": model,
        "updated_at": updated_at,
    }, ensure_ascii=False))


@dataclass
class ContextWindow:
    """一次窗口装配的结果。messages 可直接交给 AI 引擎。"""
    messages: list[dict] = field(default_factory=list)
    summary_present: bool = False
    summary_tokens: int = 0
    tail_count: int = 0          # 明文条数（chat 语义检索 exclude_recent 联动）
    tail_tokens: int = 0
    total_tokens: int = 0
    upto_message_id: int = 0     # 当前摘要覆盖到的消息 id（0 = 无摘要）
    last_message_id: int = 0     # 尾巴最后一条消息 id（0 = 无明文）
    needs_compact: bool = False
    fold_upto_id: int = 0        # compact 应折叠到的消息 id（含）；0 = 不适用
    hard_trimmed: int = 0        # 被硬裁掉的明文条数（>0 说明 compact 落后了）

    def trace_info(self) -> dict:
        """给 trace 的窗口组成摘要。"""
        return {
            "summary_present": self.summary_present,
            "summary_tokens": self.summary_tokens,
            "tail_count": self.tail_count,
            "tail_tokens": self.tail_tokens,
            "total_tokens": self.total_tokens,
            "upto_message_id": self.upto_message_id,
            "needs_compact": self.needs_compact,
            "hard_trimmed": self.hard_trimmed,
        }


def message_tokens(content: str) -> int:
    return estimate_tokens(content) + PER_MESSAGE_OVERHEAD_TOKENS


def assemble_window(db, channel_id: str, *,
                    max_tokens: int | None = None) -> ContextWindow:
    """装配当前窗口。

    max_tokens: 调用方的展示预算上限（如 random_poll 用小窗口省 token）。
      缺省 = 阈值 × HARD_CAP_RATIO（正常聊天：阈值与硬上限之间允许全明文，
      等 compact 收敛）。无论预算多小，needs_compact 都按全局阈值对
      完整窗口评估——小窗口调用不该触发也不该掩盖 compact 需求。
    """
    channel_id = str(channel_id)
    threshold = config.CONTEXT_COMPACT_THRESHOLD_TOKENS
    cap = max_tokens if max_tokens else int(threshold * HARD_CAP_RATIO)

    state = load_summary_state(db, channel_id)
    summary_text = state["summary"] if state else ""
    upto = int(state.get("upto_message_id", 0)) if state else 0

    summary_msg = None
    summary_tokens = 0
    if summary_text:
        summary_msg = {"role": "user",
                       "content": f"{SUMMARY_LABEL}\n{summary_text}"}
        summary_tokens = message_tokens(summary_msg["content"])

    tail = db.get_ai_messages_after(channel_id, upto)
    tail_tokens_each = [message_tokens(m["content"]) for m in tail]
    full_total = summary_tokens + sum(tail_tokens_each)

    # compact 评估永远基于完整窗口 + 全局阈值（与调用方预算无关）
    needs_compact = full_total >= threshold and bool(tail)
    fold_upto_id = 0
    if needs_compact:
        keep_budget = int(threshold * KEEP_RATIO)
        acc = 0
        for msg, tokens in zip(reversed(tail), reversed(tail_tokens_each)):
            if acc + tokens > keep_budget:
                fold_upto_id = msg["id"]
                break
            acc += tokens
        if not fold_upto_id:
            # 明文全装得进保留预算（超阈值来自超长摘要），折叠无意义
            needs_compact = False

    # 按调用方预算从最老明文开始裁（摘要永不裁，至少保住最后一条明文）
    trimmed = 0
    total = full_total
    start = 0
    while start < len(tail) - 1 and total > cap:
        total -= tail_tokens_each[start]
        start += 1
        trimmed += 1
    visible_tail = tail[start:]
    visible_tail_tokens = sum(tail_tokens_each[start:])

    messages: list[dict] = []
    if summary_msg:
        messages.append(dict(summary_msg))
    messages.extend({"role": m["role"], "content": m["content"]}
                    for m in visible_tail)

    if trimmed:
        logger.warning(
            f"⚠️ 上下文窗口硬裁 {trimmed} 条明文（cap={cap}tk, "
            f"full={full_total}tk）——compact 可能落后或失败")

    return ContextWindow(
        messages=messages,
        summary_present=bool(summary_msg),
        summary_tokens=summary_tokens,
        tail_count=len(visible_tail),
        tail_tokens=visible_tail_tokens,
        total_tokens=summary_tokens + visible_tail_tokens,
        upto_message_id=upto,
        last_message_id=visible_tail[-1]["id"] if visible_tail else 0,
        needs_compact=needs_compact,
        fold_upto_id=fold_upto_id,
        hard_trimmed=trimmed,
    )
