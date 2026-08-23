#!/usr/bin/env python3
"""生成 onboarding 种子记忆的人工审阅清单。

背景：`personal_memories` 需要一批可信度高的 `confirmed` 记忆，才能在不依赖
curator 选型的前提下验证读取侧。可用的存量材料有两处，可信度**不同**：

- **自述**（`prompt_sections.user_model`）：用户第一人称写自己，可信度最高；
- **`data/memory.md`**：30 条全部 `source = "ai"`，是聊天模型调 `save_memory`
  写的。里面既有用户原话的忠实记录，也有模型自己的归纳，而数据里**没有任何
  字段能区分这两者**。因此不能整批当作用户确认过的事实。

解决办法就是这份清单：用户逐条过一遍。按术语表对 confirmation 的定义
（"用户对确认问题的明确肯定回复"），这次审阅本身就是一次合法的确认事件，
所以过完之后标 `confirmed` 是诚实的，不是钻空子。

本脚本只读不写，反复跑不会有副作用。清单默认输出到
`docs/modules/memory/onboarding-review.md`。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "life_tracker.db"
DEFAULT_OUT = ROOT / "docs" / "modules" / "memory" / "onboarding-review.md"

# 正文止于「下一条记录」「下一个小节标题」或文件末尾。
# `\n##` 这个终止条件是必须的：每个小节的最后一条记录后面跟的是下一个
# `## 标题`，漏掉它会把标题当成正文尾巴吃进去（9/30 条中过招）。
_ENTRY_RE = re.compile(
    r"<!--\s*memory:(?P<meta>\{.*?\})\s*-->\s*\n-\s*(?P<body>.*?)(?=\n<!--|\n##|\Z)",
    re.DOTALL)

# `memory.md` 里的自由发挥类型 → 代码当前实际使用的八个取值。
# 拿不准的一律给 general，让用户在清单里改——**猜错的类型比空着更难发现**。
_TYPE_HINT = {
    "preference": "preference",
    "plan": "plan",
    "current_task": "current_state",
    "health": "current_state",
    "Health and Wellbeing": "current_state",
    "academic": "current_state",
    "self_awareness": "identity",
    "self_management": "interaction_style",
    "general": "general",
    "other": "general",
}

# ── 第一层：自述拆出来的候选 claim ─────────────────────────────────────────
# 这一层刻意手工拆分而不是按行自动切：原文一行里常常并列了好几件事
# （"喜欢看小说、看剧、编程，喜欢玩 sillytavern 但纠结要不要戒"是四五条），
# 而 claim 必须是单一的、可独立判断真假的陈述。自动切会切错。
#
# 每项：(claim, memory_type, 建议 status, 备注)
# 哪些自述条目**仍在线上 prompt 里**（main_template 的 USER_MODEL 块，
# 2026-08-11 的版本）。其余是用户 8 月主动删减掉的。
#
# 这个区分很重要但容易误读：「从 prompt 删掉」不等于「不是事实」。更可能是
# 为了控制常驻 token、让 prompt 聚焦。这类事实恰恰适合放进记忆表——不占常驻
# 位置，相关时才被检索出来。所以删掉过的条目**不要因此默认不勾**。
IN_LIVE_PROMPT = {1, 2, 4, 14, 17, 18, 19, 20, 21, 22, 23, 24, 25}

SELF_DESCRIPTION = [
    ("用户是女生", "identity", "confirmed", ""),
    ("用户住在悉尼，时区 AEST（UTC+10）/ 夏令时 AEDT（UTC+11）", "identity",
     "confirmed", ""),
    ("用户自认是 Ne 很强的 INTP", "identity", "confirmed",
     "自我认知标签，措辞保留「自认」"),
    ("用户在学数据科学", "current_state", "confirmed", ""),
    ("用户准备转向数据工程 / 后端方向", "plan", "confirmed",
     "线上 prompt 里缺这条"),
    ("用户喜欢看小说，主要是推理悬疑", "preference", "confirmed", ""),
    ("用户也看科幻和文学类小说", "preference", "confirmed", ""),
    ("用户喜欢看剧，不挑国籍只挑类型，偏好悬疑、医疗、探案", "preference",
     "confirmed", ""),
    ("用户喜欢编程", "preference", "confirmed", ""),
    ("用户喜欢玩 SillyTavern", "preference", "confirmed", ""),
    ("用户觉得自己玩 SillyTavern 太容易过于沉浸，在纠结要不要戒掉", "open_loop",
     "confirmed", "原文是「在纠结」，属未决事项而非已决定"),
    ("用户的爱好包括编织，时不时会做", "preference", "confirmed", ""),
    ("用户玩游戏的模式是：放下就想不起来玩，开始玩就停不下来", "preference",
     "confirmed", ""),
    ("用户讨厌被说教", "interaction_style", "confirmed", ""),
    ("用户适应一边做一件事一边顺手做另一件事的节奏（例如沉浸看剧时可以打扫卫生）",
     "preference", "confirmed", ""),
    ("用户在沉浸状态下很难自己想到「顺手做另一件事」这个念头", "identity",
     "confirmed", "原文后半句是给模型的用法说明，已拆走"),
    ("用户通常在 23:00–01:00 之间入睡，均值约 23:30", "identity", "confirmed", ""),
    ("用户平均睡眠约 7 小时", "identity", "confirmed", ""),
    ("用户起床时间较固定，在 06:00–08:00 之间，均值约 07:00", "identity",
     "confirmed", ""),
    ("用户睡眠质量不稳定，压力较大时偶尔失眠", "identity", "confirmed", ""),
    ("用户进入专注状态会很深，会忘记时间和吃饭，此时注意力极难自由切换",
     "identity", "confirmed", ""),
    ("用户有时明知该做某事却迈不出第一步，原因是预期阻力过高或短时认知负担过载",
     "identity", "confirmed", ""),
    ("用户在这种情况下需要的是极低阻力的具体启动点", "interaction_style",
     "confirmed", ""),
    ("用户的精力不是均匀流动而更像脉冲，耗散过度后会进入彻底的虚脱状态",
     "identity", "confirmed", ""),
    ("用户对时间流逝的感知与常人不同，容易出现时间感横向漂移", "identity",
     "confirmed", ""),
]

# 用户自己就加了双重限定语（"大体""类似于"），且明确声明没有寻求诊断。
# 按原文口径它本就不该是确定事实，单独列出来让用户自己决定去留。
HEDGED = [
    ("用户大体会表现出一些类似于执行功能障碍或非典型多巴胺受体回路的特征",
     "identity", "provisional",
     "原文双重限定且声明未寻求诊断；建议要么不入库，要么严格保留限定语并停在 provisional"),
]

# 这些是**给模型的行为指令**，不是关于用户的事实，因此不进记忆表。
# 混进去会造成两种坏结果：一是它们会被当作"关于用户的事实"注入，
# 二是将来 curator 可能对它们提"确认问题"，问出很荒谬的话。
MODEL_DIRECTIVES = [
    "⚠️ 绝对禁止约束：以上仅仅是为了借用你的底层模型知识库来理解她，"
    "她并没有寻求诊断，更不是你的病人。",
    "读到她分享的观察和模式时，当作「这个人的特点」，不是任何综合征的症状。",
    "不要在内部把她归类到任何诊断标签下。",
    "想到任何临床术语请主动忽略——她没有寻求诊断。",
    "你的任务是认识她这个人，不是认识一个类别。",
    "当她正在拖延某事却沉浸在别的事情里时，可以适时提醒她「可以用 iPad 看剧的"
    "同时顺手把衣服洗了」，但不要期待她自己能想到这个点子。",
]


def load_markdown_entries(text: str) -> list[dict]:
    entries = []
    for match in _ENTRY_RE.finditer(text):
        try:
            meta = json.loads(match.group("meta"))
        except json.JSONDecodeError:
            continue
        body = " ".join(match.group("body").split())
        if not body or "id" not in meta:
            continue
        entries.append({
            "id": int(meta["id"]),
            "content": body,
            "memory_type": meta.get("memory_type") or "general",
            "created_at": meta.get("created_at"),
            "valid_until": meta.get("valid_until"),
        })
    return sorted(entries, key=lambda e: e["id"])


def _item(index: str, claim: str, memory_type: str, status: str,
          note: str = "", extra: str = "") -> str:
    lines = [f"- [ ] **{index}** {claim}",
             f"      - 类型 `{memory_type}` ｜ 状态 `{status}`"]
    if extra:
        lines.append(f"      - {extra}")
    if note:
        lines.append(f"      - 说明：{note}")
    return "\n".join(lines)


def build(entries: list[dict]) -> str:
    out: list[str] = []
    w = out.append

    w("# Onboarding 种子记忆审阅清单")
    w("")
    w("> 由 `scripts/build_onboarding_review.py` 生成。**这份文件是给你手动改的**，"
      "改完之后由入库脚本读取，所以格式请保持。")
    w("> 脚本只读不写，重新生成会覆盖本文件——改之前先确认没有未保存的编辑。")
    w("")
    w("## 这份清单要做什么")
    w("")
    w("给 `personal_memories` 灌一批可信度高的记忆，用来在**不依赖 curator 选型**"
      "的前提下验证读取侧。你逐条过一遍，认下的就成为 `confirmed`。")
    w("")
    w("按术语表对 confirmation 的定义（「用户对确认问题的明确肯定回复」），"
      "**你这次审阅本身就是一次合法的确认事件**，所以过完之后标 `confirmed` "
      "是诚实的，不是给自己开后门。")
    w("")
    w("## 怎么标")
    w("")
    w("| 你要做的 | 怎么操作 |")
    w("|---|---|")
    w("| 认可，原样入库 | 勾上 `[x]` |")
    w("| 认可，但措辞要改 | 勾上 `[x]`，**直接把 claim 文字改成你要的样子** |")
    w("| 类型或状态不对 | 勾上 `[x]`，改那一行的 `类型` / `状态` |")
    w("| 不要这条 | 保持 `[ ]` 不勾，或整条删掉 |")
    w("| 一条该拆成两条 | 复制成两行，各自编号随意，入库脚本按顺序重新分配 |")
    w("")
    w("**状态只填 `confirmed` 或 `provisional`。** 前者是「这就是事实」，"
      "后者是「大致如此但我不想把话说死」——后者在聊天里会被加上限定语引用。")
    w("")
    w("所有入库记录都会带 `provenance = onboarding`，与 curator 写的行永远可区分。")
    w("")

    # ── 第一层 ──
    w("---")
    w("")
    w("## 第一层：你自己写的自述")
    w("")
    w(f"来源 `prompt_sections.user_model`（你 2026-06-10 写的）。共 "
      f"{len(SELF_DESCRIPTION)} 条，由原文拆分而来——原文一行里常并列好几件事，"
      "而一条记忆只能是一个可独立判断真假的陈述，所以拆开了。")
    w("")
    w("这一层可信度最高：是你第一人称写自己的，不经任何模型转述。")
    w("")
    w("**注意来源的时间**：`user_model` 停在 2026-06-10，而线上 prompt 的 "
      "USER_MODEL 块更新于 2026-08-11——后者是你主动删减过的版本，"
      "不是合成时漏掉的残留（那句警告从「以上」改成了「以下」，"
      "因为它前面那行被删了，自动截断不会顺手改这个字）。")
    w("")
    w("所以每条都标了它现在还在不在线上 prompt 里。**但「从 prompt 删掉」"
      "不等于「不是事实」**——更可能是为了控制常驻 token。这类事实正适合放进"
      "记忆表：不占常驻位置，相关时才检索出来。删掉过的条目不要因此默认不勾。")
    w("")
    for i, (claim, mtype, status, note) in enumerate(SELF_DESCRIPTION, 1):
        live = ("仍在线上 prompt 里" if i in IN_LIVE_PROMPT
                else "**你 8 月从 prompt 里删掉了这条**")
        w(_item(f"A{i:02d}", claim, mtype, status, note, extra=live))
        w("")

    # ── 需要你决定的 ──
    w("### A-特：你自己加了限定语的")
    w("")
    w("原文用了「大体」「类似于」双重限定，而且紧跟着一句声明说你并没有寻求诊断。"
      "**按你自己的原文口径，它本来就不该是确定事实。** 单独列出来给你决定。")
    w("")
    for i, (claim, mtype, status, note) in enumerate(HEDGED, 1):
        w(_item(f"AH{i}", claim, mtype, status, note))
        w("")

    # ── 不入库 ──
    w("### A-指令：这些不进记忆表")
    w("")
    w("以下是**给模型的行为指令**，不是关于你的事实，所以不入库，"
      "它们留在 prompt 里。列出来只是让你确认我没有漏掉或误判：")
    w("")
    for text in MODEL_DIRECTIVES:
        w(f"- {text}")
    w("")
    w("混进记忆表会有两个坏后果：一是它们会被当成「关于用户的事实」注入；"
      "二是将来提问循环上线后，curator 可能拿它们去问你确认，问出很荒谬的话。")
    w("")

    # ── 第二层 ──
    w("---")
    w("")
    w("## 第二层：`data/memory.md` 的现有记录")
    w("")
    w(f"共 {len(entries)} 条。**全部是 `source = \"ai\"`**——聊天模型调 "
      "`save_memory` 写的，里面既有你原话的忠实记录，也有模型自己的归纳，"
      "而数据里没有字段能区分。所以这一层必须你亲自过，不能整批认下。")
    w("")
    w("重点看两件事：**这条是不是真的**，以及**这条是不是你说过的**。"
      "模型归纳得很准但你其实没这么说过的，建议改成你自己的措辞再勾。")
    w("")
    w("原类型是 `memory.md` 里的自由取值（有 10 种，跟代码枚举对不上），"
      "已给出建议映射，拿不准的一律给了 `general`，请按需改。")
    w("")
    for entry in entries:
        mtype = _TYPE_HINT.get(entry["memory_type"], "general")
        extra = (f"原 id `{entry['id']}` ｜ 原类型 `{entry['memory_type']}`"
                 f" ｜ 写于 {entry['created_at']}")
        note = ""
        if entry["valid_until"]:
            note = f"原有失效时间 `{entry['valid_until']}`，注意是否已过期"
        w(_item(f"B{entry['id']}", entry["content"], mtype, "confirmed",
                note, extra))
        w("")

    w("---")
    w("")
    w("## 过完之后")
    w("")
    w("告诉我一声，我写入库脚本，把勾上的条目写进 `personal_memories`：")
    w("")
    w("- `provenance = onboarding`，与 curator 写的行区分开；")
    w("- 没有 `conversation_messages` 来源和逐字引文——**这批是例外**，"
      "因为你的自述不在消息表里，`memory.md` 的记录也没留下消息 id。"
      "设计文档第 6.5 节的 legacy 豁免机制覆盖这种情况，纪律是它们必须"
      "永远能被单独查出来；")
    w("- 入库前照例先备份数据库。")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--memory-md", type=Path, default=None,
                        help="memory.md 路径；容器内文件可先导出再用本参数指向它")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if args.memory_md and args.memory_md.exists():
        text = args.memory_md.read_text()
    else:
        print("需要 --memory-md 指向可读的 memory.md 副本"
              "（生产文件是 root 600，宿主机读不了）", file=sys.stderr)
        return 2

    entries = load_markdown_entries(text)
    if not entries:
        print("没有解析到任何记录，检查 memory.md 格式", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(entries))
    print(f"已生成 {args.out}")
    print(f"  第一层自述 {len(SELF_DESCRIPTION)} 条 + 限定语 {len(HEDGED)} 条")
    print(f"  第二层 memory.md {len(entries)} 条")
    print(f"  不入库的模型指令 {len(MODEL_DIRECTIVES)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
