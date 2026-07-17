"""LT-129 部署前校验：用真实 DB 数据对比新旧 prompt 拼装的逐字节一致性。

用法（在项目根目录，建议对 DB 副本跑）:
  .venv/bin/python scripts/check_prompt_parity.py --db data/life_tracker.db

对同一份 prompt_sections + memories/timeline/deadlines/projects/reminders，
分别用旧四层拼装（tests/legacy_reference.py 冻结副本）和新单模板渲染器
生成 block，打印每块 sha256 前 12 位；4/4 相等才允许部署。

只做 SELECT（不调 expire_past_deadlines 等写路径），bot 在线时也可跑。
"""
import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.database import Database
from bot.memory import MemoryService
from bot.prompts import LEGACY_STRUCTURED_KEYS, build_prompt
from tests.legacy_reference import legacy_from_formatted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "life_tracker.db"))
    args = parser.parse_args()

    db = Database(args.db)
    memory = MemoryService(db)
    sections = db.get_prompt_sections()
    p = build_prompt(
        "chat",
        sections=sections,
        memories=memory.list_durable(),
        memory_markdown=memory.durable_markdown(),
        today_timeline=db.get_today_events(),
        deadlines=db.get_active_deadlines(),
        projects=db.get_all_project_names(),
        pending_reminders=db.list_active_reminders(),
    )

    stripped = {k: (sections.get(k) or "").strip()
                for k in (*LEGACY_STRUCTURED_KEYS, "tools")}
    legacy = legacy_from_formatted(stripped, p.values)
    old_blocks = [t for t in (legacy.static_text(), legacy.stable_context_text(),
                              legacy.memories_text(), legacy.dynamic_text()) if t]
    new_blocks = [t for _, t in p.render_blocks()]

    def h(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    ok = True
    print(f"main_template: {'DB 已迁移' if (sections.get('main_template') or '').strip() else '空（运行时合成）'}")
    print(f"{'':4}{'old hash':14}{'new hash':14}{'chars':>8}  match")
    for i in range(max(len(old_blocks), len(new_blocks))):
        old = old_blocks[i] if i < len(old_blocks) else "<缺块>"
        new = new_blocks[i] if i < len(new_blocks) else "<缺块>"
        match = old == new
        ok &= match
        print(f"  B{i+1} {h(old) if old != '<缺块>' else old:14}{h(new) if new != '<缺块>' else new:14}{len(new):>8}  {'✅' if match else '❌'}")
    print("\n结论: " + ("✅ 全部一致，可以部署" if ok else "❌ 存在差异，禁止部署"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
