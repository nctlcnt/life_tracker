"""Schema contract：数据库 CHECK 里的枚举必须与代码常量逐字一致。

为什么需要这组测试：SQLite 无法引用 Python 常量，建表语句里的枚举只能手写，
于是同一份取值在代码和 DB 各存一份。两份可以各改各的，改一处不会同步另一处，
而且失败方式很难看——写入时被 CHECK 拒绝，或者 validator 放行了 DB 不接受的值。

2026-08-22 已经发生过同类事故：curator prompt 里的 memory_type 列表被手抄成
第二份，与 validator 使用的 CURATOR_MEMORY_TYPES 脱钩（当时取值恰好相同，
属于侥幸）。那一份现在改为从常量渲染，本文件负责盯住 DB 这一侧。
"""
import re

import pytest

from bot.database import Database
from bot.memory.curator import (
    CURATOR_ACTIONS,
    CURATOR_EVIDENCE_ROLES,
    CURATOR_MEMORY_TYPES,
)
from bot.memory.personal_repository import MEMORY_STATUSES


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "schema_contract.db"))


def _table_sql(db, table: str) -> str:
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"表不存在: {table}"
    return row["sql"]


def _check_enum(sql: str, column: str) -> set[str]:
    """抽出 `CHECK(<column> IN ('a', 'b'))` 里的取值集合。

    找不到就返回空集——调用方据此区分"没有 CHECK"和"CHECK 内容不同"。
    """
    match = re.search(
        rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\((.*?)\)\s*\)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return set()
    return set(re.findall(r"'([^']*)'", match.group(1)))


def test_memory_status_check_matches_code_constant(db):
    """`personal_memories.status` 的 CHECK 必须等于 MEMORY_STATUSES。

    状态阶梯改造时这条会先失败：改了代码常量而没改建表语句（或反过来），
    写入就会在 CHECK 上炸，而且报错信息不会指向真正的原因。
    """
    assert _check_enum(_table_sql(db, "personal_memories"), "status") == \
        MEMORY_STATUSES


def test_evidence_role_check_matches_curator_contract(db):
    """`personal_memory_sources.evidence_role` 的 CHECK 必须等于
    CURATOR_EVIDENCE_ROLES——curator 提案里允许的取值，写入时一定要能落下去。"""
    assert _check_enum(_table_sql(db, "personal_memory_sources"),
                       "evidence_role") == CURATOR_EVIDENCE_ROLES


def test_memory_type_has_no_second_copy_in_the_database(db):
    """memory_type 故意**不加** DB CHECK：它的枚举尚未拍板（设计文档未决问题 1），
    权威只有 CURATOR_MEMORY_TYPES 一处。

    这条测试防的是"好心人"给它补一个 CHECK——那会立刻造出第二份副本，
    并且让枚举拍板后的迁移多一次建表重写。枚举定下来之前不要加。
    """
    assert _check_enum(_table_sql(db, "personal_memories"), "memory_type") == set()
    assert CURATOR_MEMORY_TYPES, "代码侧的 memory_type 枚举不能为空"


def test_conversation_message_role_check_covers_curator_evidence_rules(db):
    """证据规则依赖 role 只有三种取值：只有 user 消息能当 assertion，
    assistant 永远只能是 context。role 多出第四种取值时，那条规则要重新表述。"""
    assert _check_enum(_table_sql(db, "conversation_messages"), "role") == \
        {"user", "assistant", "system"}


def test_curator_actions_constant_is_the_only_action_authority(db):
    """action 不落库，因此 DB 里不该有它的副本；契约渲染也必须用常量。

    （prompt 侧的渲染由 test_memory_curator.py 的枚举覆盖断言盯住。）
    """
    sql = _table_sql(db, "personal_memories") + _table_sql(
        db, "personal_memory_sources")
    for action in CURATOR_ACTIONS:
        assert f"'{action}'" not in sql, (
            f"action 取值 {action!r} 不应出现在建表语句里——它是提案层的概念，"
            "落库的是它造成的结果（status / evidence_role），不是动作本身")


# ── 新库与存量库必须收敛到同一份列 ─────────────────────────────────────────

# 状态阶梯字段加入之前的建表语句。这里刻意抄一份历史 DDL 而不是从代码生成，
# 因为要模拟的正是「已经在生产上跑着的老库」。
#
# **这是冻结的历史快照，不要跟着 database.py 一起改。** 它复制自 2026-08-22
# 加列之前的 personal_memories 建表语句（commit f333283 时代）。以后再加列时，
# 正确做法是新增一份 _PRE_<新字段>_DDL，而不是修改这一份——一旦把它"同步"成
# 最新结构，下面那条升级测试就退化成拿新库和新库比，永远不会失败。
_PRE_LADDER_DDL = """
CREATE TABLE personal_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    quote TEXT,
    reason TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'superseded', 'archived')),
    superseded_by INTEGER REFERENCES personal_memories(id),
    curator_model TEXT NOT NULL,
    embedding TEXT,
    embedding_model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _columns(db, table: str) -> set[str]:
    conn = db._get_conn()
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_upgraded_database_gets_the_same_columns_as_a_fresh_one(tmp_path):
    """老库升级后的列集合必须与新建库完全一致。

    加字段要改两处：`CREATE TABLE`（给新库）和 `ALTER TABLE`（给存量库）。
    只改一处不会有任何报错——新库正常、老库缺列，或者反过来——直到某段
    代码去读那一列才炸，而那时候通常已经在生产上了。
    """
    import sqlite3

    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.executescript(_PRE_LADDER_DDL)
    conn.commit()
    conn.close()

    upgraded = Database(str(legacy_path))
    fresh = Database(str(tmp_path / "fresh.db"))

    assert _columns(upgraded, "personal_memories") == \
        _columns(fresh, "personal_memories")


def test_upgrade_preserves_existing_rows_and_leaves_new_columns_null(tmp_path):
    """升级不能动老数据；新列以 NULL 出现，读取侧必须容忍。

    老行没有 basis——它们是在这个字段存在之前写进去的，任何按 basis 分档的
    读取逻辑都要先想好 NULL 怎么办，不能假设每行都有。
    """
    import sqlite3

    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.executescript(_PRE_LADDER_DDL)
    conn.execute(
        "INSERT INTO personal_memories (summary, reason, memory_type, curator_model) "
        "VALUES ('老记忆', '升级前写入的', 'preference', 'old-model')")
    conn.commit()
    conn.close()

    db = Database(str(legacy_path))
    row = db._get_conn().execute(
        "SELECT * FROM personal_memories").fetchone()

    assert row["summary"] == "老记忆"
    # 旧 active 行保守映射到 hypothesis：它们没有 basis，算不出真正的
    # status，给高档位等于凭空发放断言权。
    assert row["status"] == "hypothesis"
    for column in ("basis", "scope", "stability", "gap", "alternatives"):
        assert row[column] is None
