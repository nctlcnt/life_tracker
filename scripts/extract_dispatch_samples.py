"""
Dispatch POC 步骤 0: 从 test_mode JSONL 抽样 ~50 条用户消息, 生成标注表。

策略:
  - 必标层: 关键词正则命中 (显式工具意图词)
  - 采样层: 从非关键词样本中随机抽 N 条 (seed 固定保证可复现)

每条样本附带 tool_use_proxy: AI 当时实际调了哪些工具 (从 ai_response 提取),
作为软 ground truth 给标注者参考。

用法:
    python scripts/extract_dispatch_samples.py
    python scripts/extract_dispatch_samples.py --gray-n 30 --seed 42

输出: plans/2-specs/dispatch-escalation-samples.md
标注后跑 scripts/parse_dispatch_labels.py (后续) 转 JSON。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "data" / "test_logs"
OUT_PATH = ROOT / "plans" / "2-specs" / "dispatch-escalation-samples.md"

INTERNAL_MARKERS = (
    "[内部",
    "[约定",
    "[系统轮询",
    "[提醒触发",
    "[BEDTIME",
    "[MORNING",
    "[PROACTIVE",
)

# 系统模板特征 (天气查询、scheduler 内部 prompt 等), 不是真实用户消息
SYSTEM_TEMPLATE_PREFIXES = (
    "根据以下天气数据",
    "提醒她该",  # bedtime scheduler 模板
    "现在该",
)

# 用于 stratification 的"宽口径"关键词 — 高召回容忍假阳性,
# 标注者用 [N] 把误判排除。production pre-filter 应该窄得多。
KEYWORD_RE = re.compile(
    r"(提醒(我|一下)?|记[一]?下|帮我记|别忘[了记]|再叫我|到点喊我|"
    r"几分钟后|分钟后(叫|喊|提醒)|半小时后|小时后|"
    r"deadline|"
    r"明天|后天|大后天|周[一二三四五六日天]|"
    r"\d+月\d+号|\d+点(钟|半)?|"
    r"考试|预约|跟进|约了|约在|安排|"
    r"\d+:\d{2})"
)


def is_internal(content: str) -> bool:
    s = content.lstrip()
    if any(s.startswith(p) for p in INTERNAL_MARKERS):
        return True
    if any(s.startswith(p) for p in SYSTEM_TEMPLATE_PREFIXES):
        return True
    # scheduler 内部触发标签可能附在用户消息后面 (混合消息), 这种也排除
    if any(m in s for m in INTERNAL_MARKERS):
        return True
    return False


def strip_ts_prefix(s: str) -> str:
    s = s.lstrip()
    if s.startswith("["):
        idx = s.find("]")
        if idx > 0:
            return s[idx + 1 :].strip()
    return s


def extract_tools_from_response(resp: dict) -> list[str]:
    """从 Claude shape ai_response.response.content 提 tool_use name."""
    content = resp.get("content")
    if not isinstance(content, list):
        return []
    return [
        b.get("name", "?")
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


def collect_round1_samples():
    """扫所有 jsonl 文件, 返回 round-1 用户消息样本列表。"""
    samples = []
    for fp in sorted(LOG_DIR.glob("*.jsonl")):
        with open(fp) as f:
            entries = []
            for line in f:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        for i, e in enumerate(entries):
            if e.get("type") != "ai_prompt" or e.get("round") != 1:
                continue
            msgs = e.get("payload", {}).get("messages", [])
            last_user = None
            for m in reversed(msgs):
                if m.get("role") == "user":
                    last_user = m.get("content", "")
                    break
            if not last_user or is_internal(last_user):
                continue

            # 收集后续轮的 ai_response, 提取 tool_use
            tools_called = []
            max_round = 1
            for j in range(i + 1, len(entries)):
                e2 = entries[j]
                if e2.get("type") == "ai_prompt" and e2.get("round") == 1:
                    break
                if e2.get("type") == "ai_response":
                    max_round = max(max_round, e2.get("round", 1))
                    tools_called.extend(extract_tools_from_response(e2.get("response", {})))

            samples.append(
                {
                    "source": fp.name,
                    "ts": e.get("ts", ""),
                    "provider": e.get("provider", "?"),
                    "msg": last_user,
                    "msg_clean": strip_ts_prefix(last_user),
                    "tools": tools_called,
                    "max_round": max_round,
                }
            )
    return samples


def dedupe(samples: list[dict]) -> list[dict]:
    """按 msg_clean 去重, 保留最早一条。"""
    seen = set()
    out = []
    for s in sorted(samples, key=lambda x: x["ts"]):
        key = s["msg_clean"]
        if not key or len(key) < 3:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def stratify(samples: list[dict], gray_n: int, seed: int):
    """切两层: 关键词命中必标 + 灰区随机采样。"""
    must = [s for s in samples if KEYWORD_RE.search(s["msg_clean"])]
    gray_pool = [s for s in samples if not KEYWORD_RE.search(s["msg_clean"])]
    rng = random.Random(seed)
    sampled = rng.sample(gray_pool, min(gray_n, len(gray_pool)))
    sampled.sort(key=lambda x: x["ts"])
    return must, sampled


def fmt_sample(idx: int, s: dict) -> str:
    """生成一条带 [ ] checkbox 的标注条目。"""
    tools_str = ",".join(s["tools"]) if s["tools"] else "无"
    proxy = (
        f"tool_use=YES ({s['max_round']} 轮: {tools_str})"
        if s["max_round"] > 1 or s["tools"]
        else "tool_use=NO"
    )
    msg = s["msg"]
    if len(msg) > 300:
        msg = msg[:300] + "…[截断]"
    msg = msg.replace("\n", " / ")
    return (
        f"[ ] #{idx:03d}  ts={s['ts']}  source={s['source']}  {proxy}\n"
        f"    msg: {msg}\n"
    )


def render(must: list[dict], sampled: list[dict]) -> str:
    lines = [
        "# Dispatch POC 步骤 0：escalation 触发样本标注表",
        "",
        "> 来源：`scripts/extract_dispatch_samples.py`",
        "> spec：[`dispatch-poc.md`](dispatch-poc.md) 实施步骤 0",
        "",
        "## 标注方式",
        "",
        "把每条前面的 `[ ]` 改成：",
        "- `[Y]` — SMALL_DECIDE 应该 escalate（需要工具/状态查询/记忆操作）",
        "- `[N]` — SMALL_DECIDE 应该直接闲聊回复（情绪、闲扯、persona 反应）",
        "- `[?]` — 不确定，留待讨论",
        "",
        "**不必参考 `tool_use_proxy`**——那是 prod AI 当时的实际行为，仅供参考。"
        "我们要的是「**理想**情况下小模型该不该 escalate」的判断，可能与 prod 当时不一致。",
        "",
        "## 标尺（写 SMALL_DECIDE prompt 前先想清楚）",
        "",
        "应当 escalate 的典型场景：",
        "- 涉及具体时间/日期推理 → 要 set_reminder / log_timeline_event / add_deadline",
        "- 涉及历史状态查询 → 要 query_timeline / list_reminders / 调记忆",
        "- 涉及多轮信息收集（如「明天去看医生」需要追问几点哪里）",
        "- 状态信号（深度专注/迈不出第一步/高耗宕机/时间感偏移）→ 大模型决定要不要落工具",
        "",
        "不该 escalate 的典型场景：",
        "- 纯情绪倾诉 / 闲聊 / 调侃",
        "- 对前一轮 AI 回复的回应（没有新工具意图）",
        "- 简短确认（嗯、好、知道了）",
        "",
        "---",
        "",
        f"## 必标层：关键词正则命中（共 {len(must)} 条）",
        "",
        "```",
    ]
    for i, s in enumerate(must, 1):
        lines.append(fmt_sample(i, s))
    lines.append("```")
    lines.append("")
    lines.append(f"## 灰区采样：随机抽 {len(sampled)} 条")
    lines.append("")
    lines.append("```")
    base = len(must)
    for i, s in enumerate(sampled, 1):
        lines.append(fmt_sample(base + i, s))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gray-n", type=int, default=30, help="灰区采样条数")
    parser.add_argument("--seed", type=int, default=42, help="采样随机种子")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="输出路径")
    args = parser.parse_args()

    if not LOG_DIR.exists():
        print(f"[ERROR] {LOG_DIR} 不存在", file=sys.stderr)
        sys.exit(1)

    raw = collect_round1_samples()
    deduped = dedupe(raw)
    must, sampled = stratify(deduped, args.gray_n, args.seed)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(must, sampled), encoding="utf-8")

    print(f"原始 round-1 用户消息: {len(raw)}")
    print(f"去重后唯一: {len(deduped)}")
    print(f"  必标层 (关键词命中): {len(must)}")
    print(f"  灰区采样: {len(sampled)} (seed={args.seed})")
    print(f"输出: {args.out}")


if __name__ == "__main__":
    main()
