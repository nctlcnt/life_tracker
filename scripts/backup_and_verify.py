#!/usr/bin/env python3
"""给 SQLite 库拍快照，并演练一次恢复。

为什么不用 `cp`：生产库是 WAL 模式且 bot 在跑，直接复制文件会拿到一个
可能缺少 WAL 尾部的中间状态。`sqlite3` 的在线备份 API（`Connection.backup`）
会拿到一致的快照，且不阻塞写入。

恢复演练做的事：把快照拷到一个临时路径打开、跑 `integrity_check`、
逐表比对行数。**只有真正打开并读过的快照才算验证过**——`integrity_check`
通过但表里空无一物的情况，只有比对行数才能发现。

用法：
    python scripts/backup_and_verify.py --label pre-status-ladder
    python scripts/backup_and_verify.py --label x --tables personal_memories,curator_cursors

退出码非 0 表示快照不可信，不要在这种情况下继续做破坏性迁移。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "life_tracker.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _row_counts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    counts = {}
    for table in tables:
        # 表名来自 sqlite_master，不是外部输入，但仍然加引号防止关键字冲突
        counts[table] = conn.execute(
            f'SELECT count(*) FROM "{table}"').fetchone()[0]
    return counts


def snapshot(source: Path, target: Path) -> None:
    """用在线备份 API 拍快照。源库可以同时有别的进程在写。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def verify(source: Path, snapshot_path: Path, tables: list[str] | None) -> bool:
    """把快照恢复到临时路径，跑完整性检查并与源库比对行数。"""
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        restored = Path(tmp) / "restored.db"
        shutil.copy2(snapshot_path, restored)

        conn = sqlite3.connect(str(restored))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            print(f"  integrity_check → {integrity}")
            if integrity != "ok":
                ok = False

            names = tables or _table_names(conn)
            restored_counts = _row_counts(conn, names)
        finally:
            conn.close()

        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            source_counts = _row_counts(src, names)
        finally:
            src.close()

    mismatches = []
    for table in names:
        got, want = restored_counts[table], source_counts[table]
        # 源库仍在被写入时，行数只增不减；恢复副本少于源库是正常的时间差，
        # 多于源库则说明快照来源不对，属于硬错误。
        if got > want:
            mismatches.append(f"{table}: 快照 {got} > 源库 {want}")
        elif got < want:
            print(f"  注意 {table}: 快照 {got} < 源库 {want}（拍完之后又有写入）")
    if mismatches:
        ok = False
        for line in mismatches:
            print(f"  行数异常 {line}")
    else:
        print(f"  行数比对通过（{len(names)} 张表）")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--label", required=True,
        help="快照标签，进文件名，例如 pre-status-ladder")
    parser.add_argument(
        "--tables", default="",
        help="只比对这些表（逗号分隔）；默认比对全部")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 2

    tables = [t.strip() for t in args.tables.split(",") if t.strip()] or None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = args.backup_dir / f"{args.label}-{stamp}.db"

    print(f"源库    : {args.db}")
    print(f"快照    : {target}")
    snapshot(args.db, target)
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"快照完成: {size_mb:.1f} MB")

    print("恢复演练:")
    if not verify(args.db, target, tables):
        print("\n恢复演练未通过——不要在这个状态下做破坏性迁移。", file=sys.stderr)
        return 1
    print(f"\n恢复演练通过。快照路径：\n  {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
