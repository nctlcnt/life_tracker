"""
对 conversation_messages 存量行补 embedding（memory v3 Part B2 一次性回填）。

没有回填的话，语义检索只覆盖部署之后的新消息，对已有的几周历史完全失明——
而"想起之前说过的话"恰恰是这个功能存在的意义。

用法（在项目根目录）:
  .venv/bin/python scripts/backfill_embeddings.py                # 回填 data/life_tracker.db
  .venv/bin/python scripts/backfill_embeddings.py --db 其他路径.db
  .venv/bin/python scripts/backfill_embeddings.py --dry-run      # 只统计不调 API

- 只处理 embedding 为空、或 embedding_model 不等于当前配置模型的行
- 复用 MemoryService.embed_message（与线上写入路径完全相同的拼接/落库逻辑）
- 幂等，可 Ctrl-C 中断后重跑续传；库是 WAL 模式，bot 在线时跑也安全
"""
import argparse
import asyncio
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from bot.database import Database
from bot.memory import MemoryService

CONCURRENCY = 4


def pending_rows(db_path: str) -> list[tuple[int, str]]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT id, channel_id FROM conversation_messages
        WHERE content != '' AND (embedding IS NULL OR embedding_model != ?)
        ORDER BY id
        """,
        (config.EMBEDDING_MODEL,),
    ).fetchall()
    conn.close()
    return rows


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "life_tracker.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not config.EMBEDDING_ENABLED:
        print("config.json 里 ai.embedding 未配置（api_key/model 为空），退出")
        return 1

    db = Database(args.db)  # 先跑 schema migration（幂等加列），旧库才有 embedding 列可查
    memory = MemoryService(db)
    rows = pending_rows(args.db)
    print(f"待回填: {len(rows)} 行（模型 {config.EMBEDDING_MODEL}, 库 {os.path.abspath(args.db)}）")
    if args.dry_run or not rows:
        return 0
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    failed: list[int] = []
    t0 = time.time()

    async def work(row_id: int, channel_id: str):
        nonlocal done
        async with sem:
            ok = await memory.embed_message(row_id, channel_id)
        if not ok:
            failed.append(row_id)
        done += 1
        if done % 50 == 0 or done == len(rows):
            rate = done / (time.time() - t0)
            print(f"  {done}/{len(rows)} ({rate:.1f} 行/秒, 失败 {len(failed)})")

    await asyncio.gather(*(work(r, c) for r, c in rows))

    print(f"完成: 成功 {len(rows) - len(failed)}，失败 {len(failed)}"
          + (f"（失败行 id: {failed[:20]}{'...' if len(failed) > 20 else ''}，重跑本脚本可续传）" if failed else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
