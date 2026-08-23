#!/usr/bin/env python3
"""把长期记忆的读取源从 `data/memory.md` 切到 `personal_memories`。

这个脚本做两件必须**一起**发生的事：

1. 打开 `memory.read_from_db` 开关；
2. 从 `main_template` 的 `<USER_MODEL>` 块里删掉已经进了记忆表的条目。

为什么必须一起：这两步单独做都会造成信息缺口。只开开关不删，同一批事实会在
prompt 里出现两遍（人设块一遍、记忆块一遍）；只删不开开关，那些事实就两边
都不在了——`memory.md` 里本来就没有它们。

**默认 dry-run。** 加 `--apply` 才真正执行，执行前自动拍快照。
`--revert` 退回旧读取源（只关开关；`USER_MODEL` 块请从快照恢复）。

开关是 `config.json` 里的值，进程启动时读取，所以执行后**需要重启 bot** 才生效。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "life_tracker.db"
DEFAULT_CONFIG = ROOT / "config.json"


def ticked_claims(db_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute(
            "SELECT summary FROM personal_memories "
            "WHERE provenance = 'onboarding' ORDER BY id")]
    finally:
        conn.close()


def plan_trim(user_model_body: str, claims: list[str]) -> tuple[str, list[str]]:
    """删掉 USER_MODEL 块里内容已经进了记忆表的行。

    匹配方式刻意保守：只有当某条记忆的 claim 是该行的子串，或该行去掉
    `- ` 前缀之后是某条 claim 的子串时才算重合。宁可漏删（留一点重复）
    也不要误删——误删会让 bot 丢掉一条它本来知道的事，而重复只是浪费 token。
    """
    kept, removed = [], []
    for line in user_model_body.splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if not stripped or stripped.startswith(("#", "⚠")):
            kept.append(line)
            continue
        hit = any(c and (c in stripped or stripped in c) for c in claims)
        if hit:
            removed.append(stripped)
        else:
            kept.append(line)
    return "\n".join(kept), removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--revert", action="store_true",
                        help="只关开关，退回 data/memory.md")
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text())
    current = bool(cfg.get("memory", {}).get("read_from_db", False))

    if args.revert:
        print(f"读取源开关：{current} → False")
        if args.apply:
            cfg.setdefault("memory", {})["read_from_db"] = False
            args.config.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
            print("已关闭。重启 bot 后生效。")
            print("注意：USER_MODEL 块不会自动还原，需要从快照里恢复。")
        else:
            print("dry-run；加 --apply 执行。")
        return 0

    claims = ticked_claims(args.db)
    if not claims:
        print("记忆表里没有 onboarding 种子，先跑 ingest_onboarding_seeds.py",
              file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        mt = conn.execute(
            "SELECT value FROM prompt_sections WHERE key='main_template'"
        ).fetchone()[0]
    finally:
        conn.close()

    m = re.search(r"(<USER_MODEL[^>]*>)(.*?)(</USER_MODEL>)", mt, re.S)
    if not m:
        print("main_template 里找不到 USER_MODEL 块", file=sys.stderr)
        return 1
    new_body, removed = plan_trim(m.group(2), claims)

    print(f"记忆表里有 {len(claims)} 条种子记忆")
    print(f"读取源开关：{current} → True")
    print(f"\nUSER_MODEL 块将删掉 {len(removed)} 行（内容已进记忆表）：")
    for line in removed:
        print(f"  - {line[:64]}")
    kept_lines = [ln for ln in new_body.splitlines() if ln.strip()]
    print(f"\n保留 {len(kept_lines)} 行：")
    for line in kept_lines:
        print(f"  {line.strip()[:64]}")

    if not args.apply:
        print("\n以上为 dry-run。确认后加 --apply 执行。")
        return 0

    print("\n先拍快照：")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "backup_and_verify.py"),
         "--label", "pre-memory-source-switch"],
        capture_output=True, text=True)
    print("  " + result.stdout.strip().replace("\n", "\n  "))
    if result.returncode != 0:
        print("备份未通过，中止。", file=sys.stderr)
        return 1

    merged = mt[:m.start()] + m.group(1) + new_body + "\n" + m.group(3) + mt[m.end():]
    conn = sqlite3.connect(str(args.db))
    try:
        conn.execute("UPDATE prompt_sections SET value = ?, "
                     "updated_at = datetime('now') WHERE key='main_template'",
                     (merged,))
        conn.commit()
    finally:
        conn.close()
    print(f"  main_template: {len(mt)} → {len(merged)} 字符")

    cfg.setdefault("memory", {})["read_from_db"] = True
    args.config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    print("  开关已打开")
    print("\n完成。**重启 bot 后生效。** 出问题用 --revert 关开关，"
          "USER_MODEL 块从上面那个快照恢复。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
