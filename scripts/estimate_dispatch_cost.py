"""
Phase 2 dispatch 成本离线估算

扫 messages 表，用三种方式估 "需要工具" 的消息占比：
  1. regex 关键词命中（快、有偏）
  2. Flash Lite 判断（可选，调 API）
  3. 人工 spot check（可选，导出样本由用户标注）

然后把占比套进三种 ESCALATE_STRATEGY × 三种 Smart 模型的定价表，
算月成本矩阵，写到 plans/dispatch-cost-estimate.md。

使用：
    # 最快：只跑 regex
    python -m scripts.estimate_dispatch_cost

    # 过去 14 天 + 跑 Flash 判断（任意 preset 名，走统一引擎）
    python -m scripts.estimate_dispatch_cost --days 14 --flash <preset>


    # 导出 30 条人工标注样本
    python -m scripts.estimate_dispatch_cost --spot-check 30
    # 人工标注后保存为 plans/dispatch-labels.json，再跑
    python -m scripts.estimate_dispatch_cost --labels plans/dispatch-labels.json

PRICING 和 TOKEN_BUDGET 写死在脚本顶部，按实际场景调。
"""
import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "life_tracker.db"
OUT_PATH = ROOT / "plans" / "dispatch-cost-estimate.md"
SPOT_CHECK_PATH = ROOT / "plans" / "dispatch-spot-check.txt"


# ── 关键词规则（rule_based 策略模拟 + 粗筛 need_tool_rate）─────────────────
# 策略：只匹 "强意图词"。日期/时间词太宽（"今天/昨天"几乎每条都有），单独出现不算工具意图。
# 配合实际数据自行调整。
TOOL_KEYWORDS = [
    r"记[一下]?(一?下|着|的|录)?|帮我记|给我记",
    r"提醒我|别忘[了记]|再叫我|到点喊我",
    r"deadline|截止|期限|ddl",
    r"设(一?个)?(闹钟|提醒)",
    r"加(一?条|到)?(todo|待办|任务)",
    r"(明天|后天|下周|下月|今晚|周[一二三四五六日天])\s*(要|有|得|去|做|考|交|见|开会|上课)",
    r"\d{1,2}\s*[点:：]\s*(要|有|得|去|做|考|交|见|开会|上课|开始|结束|睡|吃|提醒)",
    r"安排一?下|日程(怎么样|有什么)",
]
KEYWORD_RE = re.compile("|".join(TOOL_KEYWORDS), re.IGNORECASE)


def regex_needs_tool(msg: str) -> bool:
    return bool(KEYWORD_RE.search(msg or ""))


# ── 定价（per million tokens，USD）────────────────────────────────────────
# 粗估，按 2026-04 公开价格。实际请校准后再下结论。
PRICING = {
    "opus":        {"in": 5.0,  "out": 25.0,  "cache_read": 0.50},
    "sonnet":      {"in": 3.0,   "out": 15.0,  "cache_read": 0.30},
    "haiku":       {"in": 1.0,   "out": 5.0,  "cache_read": 0.10},
    "gemini-pro":  {"in": 1.25,  "out": 5.0,   "cache_read": 0.00},
    "flash-lite":  {"in": 0.10,  "out": 0.40,  "cache_read": 0.00},
}


# ── Token 预算（单条消息粗估，CLI 可覆盖）─────────────────────────────────
# conditional_flash: Flash 全量对话 + 偶尔 escalate 到 Smart 带工具
# always_smart:      Smart 轻量决策 + Flash 风格转述
# rule_based:        匹到走 Smart 工具路径；没匹到走 Flash（带 escalate 兜底）
TOKEN_BUDGET = {
    "flash_chat":      {"in": 2000, "out": 150},  # Flash 对话层
    "smart_tools":     {"in": 5000, "out": 300},  # Smart 带完整工具 + 历史
    "smart_decision":  {"in": 3000, "out": 200},  # always_smart 的 Smart 层（不含转述）
}

DEFAULT_SMART_CACHE = 0.70  # Sonnet/Opus prompt caching 实测 ~73-85%
DEFAULT_FLASH_CACHE = 0.0   # Gemini 不走 Anthropic cache


def one_shot_cost(model: str, t_in: int, t_out: int, cache_rate: float) -> float:
    """单条消息的 USD 成本"""
    p = PRICING[model]
    in_cost = t_in * ((1 - cache_rate) * p["in"] + cache_rate * p["cache_read"])
    out_cost = t_out * p["out"]
    return (in_cost + out_cost) / 1_000_000


def estimate_strategy_costs(
    msg_count: int,
    need_tool_rate: float,
    smart_model: str,
    flash_cache_rate: float = DEFAULT_FLASH_CACHE,
    smart_cache_rate: float = DEFAULT_SMART_CACHE,
    rule_recall: float = 0.85,
    rule_precision: float = 0.80,
) -> dict:
    f = TOKEN_BUDGET["flash_chat"]
    st = TOKEN_BUDGET["smart_tools"]
    sd = TOKEN_BUDGET["smart_decision"]

    flash_cost = one_shot_cost("flash-lite", f["in"], f["out"], flash_cache_rate)
    smart_tools_cost = one_shot_cost(smart_model, st["in"], st["out"], smart_cache_rate)
    smart_decision_cost = one_shot_cost(smart_model, sd["in"], sd["out"], smart_cache_rate)

    # strategy 1: conditional_flash
    cond = msg_count * (flash_cost + need_tool_rate * smart_tools_cost)

    # strategy 2: always_smart
    always = msg_count * (smart_decision_cost + flash_cost)

    # strategy 3: rule_based
    # match_rate ≈ need*recall + (1-need)*(1-precision)
    match_rate = need_tool_rate * rule_recall + (1 - need_tool_rate) * (1 - rule_precision)
    # 规则没命中但其实要工具的那部分，走 Flash 并 escalate → 额外 Smart 成本
    miss_need = need_tool_rate * (1 - rule_recall)
    rule = msg_count * (
        match_rate * smart_tools_cost +
        (1 - match_rate) * (flash_cost + miss_need * smart_tools_cost)
    )

    return {"conditional_flash": cond, "always_smart": always, "rule_based": rule}


# ── 数据加载 ─────────────────────────────────────────────────────────────
def load_user_messages(days: int) -> list[dict]:
    if not DB_PATH.exists():
        print(f"[ERROR] 数据库不存在：{DB_PATH}", file=sys.stderr)
        sys.exit(1)
    since = (datetime.now() - timedelta(days=days)).isoformat(sep=" ")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, content, timestamp FROM messages "
        "WHERE role = 'user' AND timestamp > ? "
        "ORDER BY id ASC",
        (since,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Flash 模拟判断（可选）────────────────────────────────────────────────
async def flash_judge_rate(messages: list[dict], preset_name: str,
                           sample_n: int) -> tuple[int, int]:
    from config import PRESETS
    from bot.ai_engine import _load_engine

    preset = PRESETS.get(preset_name)
    if preset is None:
        raise SystemExit(f"preset {preset_name!r} 不存在；可用：{list(PRESETS.keys())}")
    simple_completion = _load_engine(preset.provider).simple_completion

    sampled = random.sample(messages, min(sample_n, len(messages)))
    would = 0
    sem = asyncio.Semaphore(3)

    async def judge_one(m):
        nonlocal would
        prompt = (
            "判断下面这条用户消息是否需要调用时间管理工具"
            "（记录事件 / 设提醒 / 加 deadline / 查询日程）。\n"
            "只输出一个字：Y（需要）或 N（不需要）。不要解释。\n\n"
            f"消息：{m['content'][:500]}"
        )
        async with sem:
            try:
                resp = await simple_completion(prompt, preset)
                if resp.strip().upper().startswith("Y"):
                    would += 1
            except Exception as e:
                print(f"[WARN] flash judge failed on id={m['id']}: {e}", file=sys.stderr)

    await asyncio.gather(*(judge_one(m) for m in sampled))
    return would, len(sampled)


# ── Spot check 导出 / 标注加载 ────────────────────────────────────────────
def write_spot_check(messages: list[dict], n: int, path: Path):
    sample = random.sample(messages, min(n, len(messages)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "# 人工标注：每条消息判断是否需要工具（记录/提醒/deadline/查询）\n"
            "# 标注后请输出 JSON 到 plans/dispatch-labels.json，格式：\n"
            "#   [{\"id\": 123, \"need_tool\": true}, {\"id\": 124, \"need_tool\": false}, ...]\n"
            "# 然后运行：python -m scripts.estimate_dispatch_cost --labels plans/dispatch-labels.json\n\n"
        )
        for m in sample:
            f.write(f"id={m['id']}  ts={m['timestamp']}\n")
            f.write(f"  {m['content']}\n\n")


def load_labels(path: Path) -> dict[int, bool]:
    if not path.exists():
        raise SystemExit(f"labels 文件不存在：{path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {int(d["id"]): bool(d["need_tool"]) for d in data}


# ── 报告 ─────────────────────────────────────────────────────────────────
def write_report(ctx: dict, out_path: Path):
    lines = []
    lines.append("# Dispatch 三策略成本估算报告")
    lines.append("")
    lines.append(f"生成时间：{datetime.now().isoformat()}")
    lines.append(f"样本范围：过去 {ctx['days']} 天 user 消息 {ctx['msg_count']} 条")
    lines.append(f"id 区间：{ctx['min_id']} – {ctx['max_id']}")
    lines.append("")

    lines.append("## 需要工具的消息占比（三种估算）")
    lines.append("")
    lines.append(f"- regex 关键词命中率：**{ctx['regex_rate']:.1%}**（precision ~0.80，粗筛）")
    if ctx["flash_rate"] is not None:
        lines.append(
            f"- Flash Lite 判断 escalate 率（sample={ctx['flash_sample']}）：**{ctx['flash_rate']:.1%}**"
        )
    else:
        lines.append("- Flash Lite 判断：**未跑**（加 `--flash <preset-name>` 开启）")
    if ctx["manual_rate"] is not None:
        lines.append(
            f"- 人工标注（n={ctx['manual_n']}）：**{ctx['manual_rate']:.1%}** ← ground truth"
        )
    else:
        lines.append(
            "- 人工标注：**未做**（`--spot-check N` 导出样本 → 标注 → `--labels` 回喂）"
        )
    lines.append("")

    msgs_per_day = ctx["msg_count"] / max(ctx["days"], 1)
    month_msgs = int(msgs_per_day * 30)
    chosen_rate = (
        ctx["manual_rate"] if ctx["manual_rate"] is not None
        else (ctx["flash_rate"] if ctx["flash_rate"] is not None else ctx["regex_rate"])
    )
    chosen_src = (
        "manual" if ctx["manual_rate"] is not None
        else ("flash" if ctx["flash_rate"] is not None else "regex")
    )

    lines.append("## 月成本矩阵（按当前日均外推 30 天）")
    lines.append("")
    lines.append(
        f"日均 {msgs_per_day:.1f} 条 → 月约 **{month_msgs}** 条，"
        f"采用 need_tool_rate = {chosen_rate:.1%}（来源：{chosen_src}）"
    )
    lines.append("")
    lines.append("| Smart 模型 | conditional_flash | always_smart | rule_based |")
    lines.append("|---|---|---|---|")
    for smart in ["opus", "sonnet", "gemini-pro"]:
        costs = estimate_strategy_costs(month_msgs, chosen_rate, smart_model=smart)
        lines.append(
            f"| {smart:10s} | ${costs['conditional_flash']:6.2f} | "
            f"${costs['always_smart']:6.2f} | ${costs['rule_based']:6.2f} |"
        )
    lines.append("")

    lines.append("## 建议的判断逻辑")
    lines.append("")
    lines.append(
        "- **regex vs Flash vs 人工标注差异大** → 说明 Flash 判断力差 / regex 规则不全。"
        "Flash 比人工显著低 → `conditional_flash` 漏记严重，慎选。"
    )
    lines.append(
        "- **always_smart + Sonnet** 与 **conditional_flash + Opus** 成本接近 → 选前者（更稳定）。"
    )
    lines.append(
        "- **rule_based + Gemini Pro** 通常最省，但要接受 ~15% miss rate（走 Flash 兜底）。"
    )
    lines.append(
        "- 如果月成本差距 < $10 → 不用在这个维度抠，选稳定性优先。"
    )
    lines.append("")

    lines.append("## 参数")
    lines.append("")
    lines.append("定价（per million tokens，PRICING dict）：")
    lines.append("")
    lines.append("| 模型 | input | output | cache_read |")
    lines.append("|---|---|---|---|")
    for k, v in PRICING.items():
        lines.append(f"| {k} | ${v['in']:.2f} | ${v['out']:.2f} | ${v['cache_read']:.2f} |")
    lines.append("")
    lines.append("Token 预算（单条消息粗估，TOKEN_BUDGET dict）：")
    lines.append("")
    for k, v in TOKEN_BUDGET.items():
        lines.append(f"- `{k}`: in={v['in']}, out={v['out']}")
    lines.append("")
    lines.append(
        f"cache hit rate 假设：Smart={DEFAULT_SMART_CACHE:.0%}，Flash={DEFAULT_FLASH_CACHE:.0%}"
    )
    lines.append("")
    lines.append(f"关键词正则（regex 模拟 rule_based）：`{KEYWORD_RE.pattern}`")
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── 入口 ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="扫最近几天（默认 7）")
    ap.add_argument("--flash", metavar="PRESET",
                    help="用哪个 preset 跑 Flash 判断（任意 preset）；不传则跳过")
    ap.add_argument("--flash-sample", type=int, default=100,
                    help="Flash 判断的抽样量（默认 100）")
    ap.add_argument("--spot-check", type=int, default=0,
                    help="导出 N 条到 dispatch-spot-check.txt 供人工标注，然后退出")
    ap.add_argument("--labels", metavar="PATH",
                    help="已标注的 labels.json，用于算 ground truth need_tool_rate")
    ap.add_argument("--out", default=str(OUT_PATH), help="报告输出路径")
    ap.add_argument("--seed", type=int, default=42, help="随机采样种子")
    args = ap.parse_args()

    random.seed(args.seed)

    msgs = load_user_messages(args.days)
    if not msgs:
        print(f"[ERROR] 过去 {args.days} 天没找到 user 消息", file=sys.stderr)
        sys.exit(1)

    # 只导出 spot-check 然后退出
    if args.spot_check > 0:
        write_spot_check(msgs, args.spot_check, SPOT_CHECK_PATH)
        print(f"已导出 {min(args.spot_check, len(msgs))} 条到 {SPOT_CHECK_PATH}")
        print("请人工标注后保存为 plans/dispatch-labels.json，再跑：")
        print("  python -m scripts.estimate_dispatch_cost --labels plans/dispatch-labels.json")
        return

    regex_hits = sum(1 for m in msgs if regex_needs_tool(m["content"]))
    regex_rate = regex_hits / len(msgs)

    flash_rate = None
    flash_sample = 0
    if args.flash:
        print(f"跑 Flash 判断（最多 {args.flash_sample} 条）…", flush=True)
        would, sampled = asyncio.run(flash_judge_rate(msgs, args.flash, args.flash_sample))
        flash_rate = would / sampled if sampled else 0.0
        flash_sample = sampled

    manual_rate = None
    manual_n = 0
    if args.labels:
        labels = load_labels(Path(args.labels))
        if labels:
            manual_n = len(labels)
            manual_rate = sum(1 for v in labels.values() if v) / manual_n

    ctx = {
        "days": args.days,
        "msg_count": len(msgs),
        "regex_rate": regex_rate,
        "flash_rate": flash_rate,
        "flash_sample": flash_sample,
        "manual_rate": manual_rate,
        "manual_n": manual_n,
        "min_id": msgs[0]["id"],
        "max_id": msgs[-1]["id"],
    }

    write_report(ctx, Path(args.out))
    print(f"报告 → {args.out}")
    print(f"  regex 命中率：{regex_rate:.1%}")
    if flash_rate is not None:
        print(f"  Flash 判断率：{flash_rate:.1%}  (n={flash_sample})")
    if manual_rate is not None:
        print(f"  人工标注率：{manual_rate:.1%}  (n={manual_n})")


if __name__ == "__main__":
    main()
