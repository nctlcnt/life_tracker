#!/usr/bin/env python3
"""把审阅过的 onboarding 清单写进 `personal_memories`。

读取 `docs/modules/memory/onboarding-review.md` 里勾了 `[x]` 的条目，
写成 `provenance = 'onboarding'` 的记忆行。

**默认 dry-run，只打印不写库。** 真正写入要显式加 `--apply`，
而且写入前会自动拍一次快照。

幂等：脚本按 claim 文本查重，已存在的 onboarding 行不会被重复写入，
所以中途失败可以直接重跑。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.database import Database                       # noqa: E402
from bot.memory.personal_repository import (            # noqa: E402
    PersonalMemoryRepository,
)

DEFAULT_REVIEW = ROOT / "docs" / "modules" / "memory" / "onboarding-review.md"
DEFAULT_DB = ROOT / "data" / "life_tracker.db"

_ITEM_RE = re.compile(
    r"^- \[(?P<tick>[ xX])\] \*\*(?P<code>[A-Z]+\d+)\*\* (?P<claim>.+?)\n"
    r"\s+- 类型 `(?P<type>[^`]+)` ｜ 状态 `(?P<status>[^`]+)`",
    re.MULTILINE)

# 每个小节最后一条记录的正文里混进了下一个 `## 标题`——生成清单时的解析 bug，
# 已在 build_onboarding_review.py 里修掉，但这份清单是用旧版生成并已人工标注过，
# 不能重新生成（会覆盖勾选），所以在这里清理。
_TRAILING_HEADING_RE = re.compile(r"\s*##\s+[\w &]+$")

# 用户明确排除的条目。
#   B155：内容是「服药后首次进入长时间专注」的当日观察，属于时间线，
#         应当由向量检索从原始消息里找回，不该固化成一条记忆。
#         而且它在 memory.md 里本身就是截断的（结尾是半个坏字符）。
SKIP = {"B155"}

# 用户选择「只留当前状态那部分」的条目：原记录是一段跨两个月的就医流水账，
# 既违反「一条 claim 只能是一个可独立判断真假的陈述」，又会随流程推进不断改写。
# 事件叙事留在原始消息里靠检索找回，这里只提取到今天仍然成立的状态与决定。
SPLIT = {
    "B136": [
        ("用户自 2026-08-13 起在服用 Vyvanse（ADHD 处方药）",
         "current_state", "confirmed"),
        ("用户不再去 Myhealth Medical Zetland 诊所",
         "constraint_like", "confirmed"),
    ],
}
# 上面第二条的类型在提取时再定：`constraint` 不在当前八个取值里，
# 落到 `interaction_style` 或 `general` 都不贴切，暂用 `current_state`。
_SPLIT_TYPE_FALLBACK = "current_state"


def parse_review(text: str) -> list[dict]:
    items = []
    for m in _ITEM_RE.finditer(text):
        if m.group("tick").lower() != "x":
            continue
        claim = _TRAILING_HEADING_RE.sub("", m.group("claim").strip())
        items.append({
            "code": m.group("code"),
            "claim": claim,
            "memory_type": m.group("type").strip(),
            "status": m.group("status").strip(),
        })
    return items


def expand(items: list[dict]) -> tuple[list[dict], list[str]]:
    """套用排除与拆分规则。返回（待写入项，说明行）。"""
    out, notes = [], []
    for item in items:
        code = item["code"]
        if code in SKIP:
            notes.append(f"跳过 {code}：属时间线内容，交给向量检索，不入记忆表")
            continue
        if code in SPLIT:
            notes.append(f"拆分 {code}：原记录是事件叙事，只保留仍然成立的当前状态")
            for claim, mtype, status in SPLIT[code]:
                if mtype == "constraint_like":
                    mtype = _SPLIT_TYPE_FALLBACK
                out.append({"code": f"{code}-拆", "claim": claim,
                            "memory_type": mtype, "status": status})
            continue
        out.append(item)
    return out, notes


def existing_seed_claims(db_path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute(
            "SELECT summary FROM personal_memories WHERE provenance = 'onboarding'")}
    except sqlite3.OperationalError:
        return set()          # provenance 列还不存在 = 迁移未跑，一条都没有
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true",
                        help="真正写库；不加则只打印")
    parser.add_argument("--purge-curator-rows", action="store_true",
                        help="同时清空 provenance='curator' 的行（选型期混合 profile 的旧数据）")
    args = parser.parse_args()

    items = parse_review(args.review.read_text())
    planned, notes = expand(items)
    already = existing_seed_claims(args.db)
    todo = [i for i in planned if i["claim"] not in already]

    print(f"清单勾选 {len(items)} 条 → 展开后 {len(planned)} 条")
    for note in notes:
        print(f"  · {note}")
    if already:
        print(f"  · 库中已有 {len(already)} 条 onboarding 记忆，"
              f"其中 {len(planned) - len(todo)} 条与本次重合，跳过")
    print()

    by_type: dict[str, int] = {}
    for item in todo:
        by_type[item["memory_type"]] = by_type.get(item["memory_type"], 0) + 1
    print("待写入按类型：", ", ".join(f"{k}×{v}" for k, v in sorted(by_type.items())))
    print()
    for item in todo:
        print(f"  [{item['code']:>8}] {item['status']:<11} {item['memory_type']:<17} "
              f"{item['claim'][:52]}")

    if not args.apply:
        print(f"\n以上为 dry-run，共 {len(todo)} 条。确认无误后加 --apply 写入。")
        return 0

    print(f"\n开始写入 {len(todo)} 条。先拍快照：")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "backup_and_verify.py"),
         "--label", "pre-onboarding-seed"],
        capture_output=True, text=True)
    print("  " + result.stdout.strip().replace("\n", "\n  "))
    if result.returncode != 0:
        print("备份或恢复演练未通过，中止写入。", file=sys.stderr)
        return 1

    db = Database(str(args.db))
    repo = PersonalMemoryRepository(db)

    if args.purge_curator_rows:
        conn = db._get_conn()
        try:
            n_src = conn.execute(
                "DELETE FROM personal_memory_sources WHERE memory_id IN "
                "(SELECT id FROM personal_memories WHERE provenance = 'curator')"
            ).rowcount
            n_mem = conn.execute(
                "DELETE FROM personal_memories WHERE provenance = 'curator'").rowcount
            conn.commit()
            print(f"  已清空选型期旧数据：记忆 {n_mem} 行、证据关联 {n_src} 行")
        finally:
            conn.close()

    written = 0
    for item in todo:
        repo.create_onboarding_seed(
            claim=item["claim"],
            reason=f"onboarding 种子（{item['code']}）：用户于 2026-08-23 逐条审阅确认",
            memory_type=item["memory_type"],
            status=item["status"],
        )
        written += 1
    print(f"  已写入 {written} 条")

    conn = db._get_conn()
    try:
        print("\n写入后分布：")
        for row in conn.execute(
                "SELECT provenance, status, count(*) n FROM personal_memories "
                "GROUP BY 1, 2 ORDER BY 1, 2"):
            print(f"  {row[0]:<12} {row[1]:<12} {row[2]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
