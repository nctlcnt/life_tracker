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
import re
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

# 卡片名接在原有的时间前缀里：`[2026-09-27 09:00]` → `[check_in3 · 2026-09-27 09:00]`。
# 日期整段保留：_to_ai_message 的注释说明了原因——_ensure_valid_messages 会把连续
# 同角色的消息并成一条，时间前缀是并起来之后唯一的时间锚点，缩写成 9/27 会退化。
_TIMESTAMP_PREFIX = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\] ?")
# discord_bot 在用户消息里内联的引用片段的标记，见 bot/discord_bot.py。
INLINE_QUOTE_MARK = "[回复 "
# 被回复的卡片已经折进摘要时，末尾多引用几条原话
FOLDED_QUOTE_ROUNDS = 4
QUOTE_MAX_CHARS = 200


def _state_key(channel_id: str) -> str:
    return f"{STATE_KEY_PREFIX}{channel_id}"


def _with_card_label(message: dict, labels: dict) -> dict:
    """给一条消息的 content 加上卡片名。没有卡片归属的消息原样返回。"""
    card_id = message.get("card_id")
    label = labels.get(int(card_id)) if card_id is not None else None
    if not label:
        return message
    content = message["content"]
    matched = _TIMESTAMP_PREFIX.match(content)
    if matched:
        content = f"[{label} · {matched.group(1)}] {content[matched.end():]}"
    else:
        # 时间前缀缺失或格式异常（_message_timestamp 的兜底分支）时也要带上卡片名
        content = f"[{label}] {content}"
    return {**message, "content": content}


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= QUOTE_MAX_CHARS else text[:QUOTE_MAX_CHARS] + "..."


def _build_card_reference(db, visible_tail: list[dict], labels: dict,
                          upto: int) -> dict | None:
    """末尾引用：说明最后一条用户消息在回复哪张卡片。

    分三种情况。被回复的消息已经折进摘要时，明文里已经看不到它了，所以连同
    卡片名一起多引用几条原话；明文里还看得见、而且 discord_bot 已经内联过引用
    片段时，只补一个卡片名，不重复引用同一段话；内联引用缺失时（抓取 Discord
    原消息失败也会走到这里），补上卡片名和一小段原话。
    """
    if not visible_tail:
        return None
    last = visible_tail[-1]
    if last.get("role") != "user" or not last.get("reply_to_message_id"):
        return None
    target = db.find_conversation_message_by_discord_id(
        str(last["reply_to_message_id"]))
    if not target or target.get("card_id") is None:
        return None
    card_id = int(target["card_id"])
    label = labels.get(card_id) or db.get_card_labels([card_id]).get(card_id)
    if not label:
        return None

    if int(target["id"]) <= int(upto):
        rounds = db.get_card_tail_messages(
            card_id, limit=FOLDED_QUOTE_ROUNDS, upto_id=int(target["id"]))
        quoted = "\n".join(_truncate(m["content"]) for m in rounds)
        if not quoted:
            quoted = _truncate(target.get("content") or "")
        content = (f"【上一条在回复卡片 {label}，它早于摘要分界线，"
                   f"相关原话：\n{quoted}】")
    elif INLINE_QUOTE_MARK in (last.get("content") or ""):
        content = f"【上一条在回复卡片 {label}】"
    else:
        content = (f"【上一条在回复卡片 {label} 里的："
                   f"\n{_truncate(target.get('content') or '')}】")
    return {"role": "user", "content": content}


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
    # 卡片名在明文里，所以要先加上再算 token：needs_compact 衡量的应该是真正
    # 发给模型的那份文本。没有卡片归属的消息（卡片上线之前的历史、测试数据）
    # 不受影响，token 数与以前一致。
    card_labels = db.get_card_labels([m.get("card_id") for m in tail])
    if card_labels:
        tail = [_with_card_label(m, card_labels) for m in tail]
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

    # 末尾引用插在最后一条消息之前，于是模型读到的顺序是「在回复哪张卡片」
    # 加上新消息本身。它和新消息同为 user 角色，_ensure_valid_messages 会把
    # 两者并成一轮，正是期望的读法。
    reference = _build_card_reference(db, visible_tail, card_labels, upto)
    reference_tokens = 0
    if reference is not None:
        reference_tokens = message_tokens(reference["content"])
        messages.insert(len(messages) - 1, reference)

    if trimmed:
        logger.warning(
            f"⚠️ 上下文窗口硬裁 {trimmed} 条明文（cap={cap}tk, "
            f"full={full_total}tk）——compact 可能落后或失败")

    return ContextWindow(
        messages=messages,
        summary_present=bool(summary_msg),
        summary_tokens=summary_tokens,
        # tail_count 是真实消息条数（chat 语义检索的 exclude_recent 依赖它），
        # 合成的末尾引用不计入，但它的 token 要算进总数。
        tail_count=len(visible_tail),
        tail_tokens=visible_tail_tokens,
        total_tokens=summary_tokens + visible_tail_tokens + reference_tokens,
        upto_message_id=upto,
        last_message_id=visible_tail[-1]["id"] if visible_tail else 0,
        needs_compact=needs_compact,
        fold_upto_id=fold_upto_id,
        hard_trimmed=trimmed,
    )
