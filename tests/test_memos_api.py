"""memos 表、/api/memos、以及 prompt 注入的取数/格式化契约。

参考 tests/test_api_auth.py 的 httpx + IsolatedAsyncioTestCase 写法测 HTTP 层；
纯逻辑（时间窗口、分页、格式化）用 pytest 函数 + tmp_path 直接测 Database/bot.memos。
"""
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from itertools import count
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from api import server
from api.auth import API_KEY_ENV
import bot.database as database_module
from bot import timezone_state
from bot.database import Database
from bot.memos import get_today_memos_for_prompt, today_memo_range_utc
from bot.prompts import LABEL_TODAY_MEMOS, _format_today_memos


TEST_KEY = "k" * 32


def _db(tmp_path) -> Database:
    return Database(str(tmp_path / "memos.db"))


# ────────────────────────────────────────────────────────────────
# Database 层
# ────────────────────────────────────────────────────────────────

def test_upsert_memo_is_idempotent_on_client_id(tmp_path):
    db = _db(tmp_path)
    first = db.upsert_memo(client_id="c1", content="原文",
                            occurred_at="2026-09-18T04:20:00.000Z")
    second = db.upsert_memo(client_id="c1", content="改过的内容",
                             occurred_at="2026-09-18T05:00:00.000Z")
    assert first == second
    assert second["content"] == "原文"


def test_memo_id_is_string_and_timestamps_are_utc_milliseconds(tmp_path):
    db = _db(tmp_path)
    memo = db.upsert_memo(client_id="c2", content="test",
                           occurred_at="2026-09-18T04:20:00.000Z")
    assert isinstance(memo["id"], str)
    pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
    assert re.match(pattern, memo["created_at"])
    assert re.match(pattern, memo["updated_at"])


def test_update_memo_bumps_updated_at_and_404s_when_deleted(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ticks = count()
    monkeypatch.setattr(
        database_module, "utc_now_iso",
        lambda: f"2026-09-18T04:20:{next(ticks):02d}.000Z",
    )
    memo = db.upsert_memo(client_id="c3", content="test",
                           occurred_at="2026-09-18T04:20:00.000Z")
    updated = db.update_memo(int(memo["id"]), content="改了")
    assert updated is not None
    assert updated["updated_at"] > memo["updated_at"]
    assert updated["content"] == "改了"

    assert db.soft_delete_memo(int(memo["id"])) is True
    assert db.update_memo(int(memo["id"]), content="再改") is None


def test_delete_memo_is_idempotent_and_get_still_returns_the_row(tmp_path):
    db = _db(tmp_path)
    memo = db.upsert_memo(client_id="c4", content="test",
                           occurred_at="2026-09-18T04:20:00.000Z")
    memo_id = int(memo["id"])
    assert db.soft_delete_memo(memo_id) is True
    assert db.soft_delete_memo(memo_id) is False

    row = db.get_memo(memo_id)
    assert row is not None
    assert row["deleted_at"] is not None


def test_list_memos_between_excludes_deleted(tmp_path):
    db = _db(tmp_path)
    kept = db.upsert_memo(client_id="k1", content="留着",
                           occurred_at="2026-09-18T05:00:00.000Z")
    gone = db.upsert_memo(client_id="k2", content="删掉",
                           occurred_at="2026-09-18T06:00:00.000Z")
    db.soft_delete_memo(int(gone["id"]))

    rows = db.list_memos_between("2026-09-18T04:00:00.000Z", "2026-09-19T04:00:00.000Z")
    assert [r["id"] for r in rows] == [kept["id"]]


def test_soft_delete_tag_cascades_to_memos(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ticks = count()
    monkeypatch.setattr(
        database_module, "utc_now_iso",
        lambda: f"2026-09-18T04:20:{next(ticks):02d}.000Z",
    )
    t = db.upsert_tag(client_id="t-del", name="将删除的标签")
    tag_id = int(t["id"])
    m_with_tag = db.upsert_memo(
        client_id="m-tag-1", content="挂了标签的", occurred_at="2026-09-18T04:00:00.000Z",
        tag_ids=[tag_id],
    )
    m_without_tag = db.upsert_memo(
        client_id="m-tag-2", content="没挂标签的", occurred_at="2026-09-18T04:00:00.000Z",
    )
    old_updated_at_with = m_with_tag["updated_at"]
    old_updated_at_without = m_without_tag["updated_at"]

    assert db.soft_delete_tag("t-del") is True
    # 重复删除返回 False、不报错
    assert db.soft_delete_tag("t-del") is False

    # 标签 deleted_at 有值
    t_row = db.get_tag_by_client_id("t-del")
    assert t_row["deleted_at"] is not None

    # 挂了它的 memo updated_at 变大，tag_client_ids 里不再有它
    m_with_after = db.get_memo(int(m_with_tag["id"]))
    assert m_with_after["updated_at"] > old_updated_at_with
    assert m_with_after["tag_client_ids"] == []

    # 没挂它的 memo updated_at 不变
    m_without_after = db.get_memo(int(m_without_tag["id"]))
    assert m_without_after["updated_at"] == old_updated_at_without


def test_upsert_memo_with_tag_ids_and_idempotence(tmp_path):
    db = _db(tmp_path)
    t1 = db.upsert_tag(client_id="t-idemp-1", name="标签1")
    t2 = db.upsert_tag(client_id="t-idemp-2", name="标签2")
    m1 = db.upsert_memo(
        client_id="m-idemp", content="测试", occurred_at="2026-09-18T04:00:00.000Z",
        tag_ids=[int(t1["id"])],
    )
    assert m1["tag_client_ids"] == ["t-idemp-1"]

    # client_id 重复、tag_ids 不同时，标签保持第一次的
    m2 = db.upsert_memo(
        client_id="m-idemp", content="新测试", occurred_at="2026-09-18T04:00:00.000Z",
        tag_ids=[int(t2["id"])],
    )
    assert m2["tag_client_ids"] == ["t-idemp-1"]


def test_update_memo_tags_only_replaces_tags_and_bumps_updated_at(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ticks = count()
    monkeypatch.setattr(
        database_module, "utc_now_iso",
        lambda: f"2026-09-18T04:20:{next(ticks):02d}.000Z",
    )
    t1 = db.upsert_tag(client_id="t-u1", name="旧标签")
    t2 = db.upsert_tag(client_id="t-u2", name="新标签")
    m = db.upsert_memo(
        client_id="m-u", content="内容", occurred_at="2026-09-18T04:00:00.000Z",
        tag_ids=[int(t1["id"])],
    )
    old_updated = m["updated_at"]

    # 只传 tag_ids：整体替换，updated_at 变大
    updated = db.update_memo(int(m["id"]), tag_ids=[int(t2["id"])])
    assert updated["tag_client_ids"] == ["t-u2"]
    assert updated["updated_at"] > old_updated


def test_legacy_database_without_tags_table_compatibility(tmp_path):
    # 旧库兼容：用一个已有 memos 表、没有 tags 表的库打开 Database(path)，
    # 新表被建好，旧 memo 返回 tag_client_ids == []；再打开一次也不报错
    db_file = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            images_json TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'app',
            latitude REAL,
            longitude REAL,
            place_name TEXT
        )
    """)
    conn.execute(
        "INSERT INTO memos (client_id, content, occurred_at, created_at, updated_at) "
        "VALUES ('old-1', '旧数据', '2026-09-18T04:00:00.000Z', '2026-09-18T04:00:00.000Z', '2026-09-18T04:00:00.000Z')"
    )
    conn.commit()
    conn.close()

    # 打开 Database(path)
    db1 = Database(db_file)
    memo = db1.get_memo(1)
    assert memo is not None
    assert memo["tag_client_ids"] == []

    # 再打开一次也不报错
    db2 = Database(db_file)
    memo2 = db2.get_memo(1)
    assert memo2 is not None
    assert memo2["tag_client_ids"] == []


def test_list_memos_after_pagination_with_equal_updated_at_is_gapless(tmp_path, monkeypatch):
    """插入多条 updated_at 相同的行，limit=1 逐页拉取，不重复、不遗漏。"""
    db = _db(tmp_path)
    monkeypatch.setattr(database_module, "utc_now_iso",
                         lambda: "2026-09-18T04:00:00.000Z")
    ids = [
        db.upsert_memo(client_id=f"p{i}", content=f"memo{i}",
                        occurred_at="2026-09-18T04:00:00.000Z")["id"]
        for i in range(5)
    ]

    seen = []
    cursor_updated_at, cursor_id = None, 0
    for _ in range(len(ids) + 2):
        page = db.list_memos_after(cursor_updated_at, cursor_id, 1)
        if not page:
            break
        item = page[0]
        seen.append(item["id"])
        cursor_updated_at, cursor_id = item["updated_at"], int(item["id"])
    assert seen == ids


def test_list_memos_after_can_miss_an_update_that_lands_behind_an_already_issued_cursor(
    tmp_path, monkeypatch,
):
    """已知限制，不是本次要修的 bug：如果一条 memo 被再次更新，但那次更新的
    updated_at 落在某个游标已经翻过的位置之前或等于该位置（例如系统时钟没有
    前进、或两次写入落在同一个时间戳里），下一次增量拉取会跳过它，直到它
    以后再被改一次、updated_at 真正超过当前游标为止。"""
    db = _db(tmp_path)
    monkeypatch.setattr(database_module, "utc_now_iso",
                         lambda: "2026-09-18T04:00:00.000Z")
    early = db.upsert_memo(client_id="e1", content="early",
                            occurred_at="2026-09-18T04:00:00.000Z")
    later = db.upsert_memo(client_id="e2", content="later",
                            occurred_at="2026-09-18T04:00:00.000Z")

    # App 已经翻页翻过 later 这一条
    cursor_updated_at, cursor_id = later["updated_at"], int(later["id"])

    # early 随后被更新，但时钟没有前进：updated_at 仍停留在游标已经越过的位置
    db.update_memo(int(early["id"]), content="early edited")

    page = db.list_memos_after(cursor_updated_at, cursor_id, 10)
    assert early["id"] not in [item["id"] for item in page]


def test_upsert_memo_with_location_returns_location(tmp_path):
    db = _db(tmp_path)
    memo = db.upsert_memo(
        client_id="loc-1", content="带地点", occurred_at="2026-09-18T04:20:00.000Z",
        latitude=-33.9173, longitude=151.2313, place_name="Kensington",
    )
    assert memo["latitude"] == -33.9173
    assert memo["longitude"] == 151.2313
    assert memo["place_name"] == "Kensington"


def test_upsert_memo_without_location_has_none_location_fields(tmp_path):
    db = _db(tmp_path)
    memo = db.upsert_memo(
        client_id="loc-2", content="无地点", occurred_at="2026-09-18T04:20:00.000Z",
    )
    assert "latitude" in memo and memo["latitude"] is None
    assert "longitude" in memo and memo["longitude"] is None
    assert "place_name" in memo and memo["place_name"] is None


def test_upsert_memo_duplicate_client_id_with_different_location_keeps_first(tmp_path):
    db = _db(tmp_path)
    first = db.upsert_memo(
        client_id="loc-dup", content="第一次", occurred_at="2026-09-18T04:20:00.000Z",
        latitude=-33.9173, longitude=151.2313, place_name="Kensington",
    )
    second = db.upsert_memo(
        client_id="loc-dup", content="第二次", occurred_at="2026-09-18T05:00:00.000Z",
        latitude=31.2304, longitude=121.4737, place_name="Shanghai",
    )
    assert first == second
    assert second["latitude"] == -33.9173
    assert second["place_name"] == "Kensington"


def test_update_memo_can_clear_location_and_bumps_updated_at(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ticks = count()
    monkeypatch.setattr(
        database_module, "utc_now_iso",
        lambda: f"2026-09-18T04:20:{next(ticks):02d}.000Z",
    )
    memo = db.upsert_memo(
        client_id="loc-clear", content="test", occurred_at="2026-09-18T04:20:00.000Z",
        latitude=-33.9173, longitude=151.2313, place_name="Kensington",
    )
    updated = db.update_memo(
        int(memo["id"]), latitude=None, longitude=None, place_name=None,
    )
    assert updated is not None
    assert updated["updated_at"] > memo["updated_at"]
    assert updated["latitude"] is None
    assert updated["longitude"] is None
    assert updated["place_name"] is None


def test_memos_migration_from_old_schema(tmp_path):
    import sqlite3
    db_file = tmp_path / "old_memos.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        CREATE TABLE memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            images_json TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'app'
        )
    """)
    conn.execute(
        "INSERT INTO memos (client_id, content, occurred_at, created_at, updated_at) "
        "VALUES ('old-1', '旧数据', '2026-09-18T04:20:00.000Z', '2026-09-18T04:20:00.000Z', '2026-09-18T04:20:00.000Z')"
    )
    conn.commit()
    conn.close()

    # 打开数据库，应自动执行加列迁移
    db = Database(str(db_file))
    memo = db.get_memo(1)
    assert memo is not None
    assert "latitude" in memo and memo["latitude"] is None
    assert "longitude" in memo and memo["longitude"] is None
    assert "place_name" in memo and memo["place_name"] is None

    # 再次打开，确认迁移幂等不报错
    db2 = Database(str(db_file))
    memo2 = db2.get_memo(1)
    assert memo2 == memo


# ────────────────────────────────────────────────────────────────
# bot.memos：今天的范围 + prompt 用的取数
# ────────────────────────────────────────────────────────────────

def test_today_memo_range_utc_day_cutoff_boundary(monkeypatch):
    monkeypatch.setattr(timezone_state, "get_timezone", lambda: "UTC")
    before = datetime(2026, 9, 18, 3, 59, 0)
    after = datetime(2026, 9, 18, 4, 0, 0)

    start_before, end_before = today_memo_range_utc(before)
    start_after, end_after = today_memo_range_utc(after)

    assert (start_before, end_before) == (
        "2026-09-17T04:00:00.000Z", "2026-09-18T04:00:00.000Z",
    )
    assert (start_after, end_after) == (
        "2026-09-18T04:00:00.000Z", "2026-09-19T04:00:00.000Z",
    )


def _find_dst_transition_day(tz_name: str, year: int) -> date:
    """在 zoneinfo 里现场找一个真实的夏令时切换日，不硬编码具体日期。

    比较同一天 00:00 和 23:59 的 offset（而不是逐日比较午夜 offset）：
    悉尼的切换发生在当天凌晨 2-3 点，午夜时 offset 还没变，逐日比较午夜会把
    "转换发生的那一天"晚报一天。
    """
    tz = ZoneInfo(tz_name)
    day = date(year, 1, 1)
    for _ in range(370):
        start_offset = datetime.combine(day, datetime.min.time(), tzinfo=tz).utcoffset()
        end_offset = datetime.combine(day, datetime.max.time(), tzinfo=tz).utcoffset()
        if start_offset != end_offset:
            return day
        day += timedelta(days=1)
    raise AssertionError(f"未在 {tz_name} {year} 年找到夏令时切换日")


def test_today_memo_range_utc_dst_transition_is_not_24_hours(monkeypatch):
    """悉尼夏令时切换日：当天本地 4 点到次日本地 4 点之间的 UTC 时长
    不是 24 小时（差 1 小时），证明用的是按日期查 offset 而不是固定 offset。

    切换本身发生在当天凌晨（早于 04:00 的 cutoff），所以要用切换日凌晨的
    时刻（此时按 4 点切分还算"前一天"）才会让算出的窗口跨过切换瞬间；
    用当天白天的时刻，切换已经结束，窗口反而落在切换之后、看不出差异。
    """
    monkeypatch.setattr(timezone_state, "get_timezone", lambda: "Australia/Sydney")
    transition_day = _find_dst_transition_day("Australia/Sydney", 2026)
    now = datetime(transition_day.year, transition_day.month, transition_day.day, 1, 0, 0)

    start_str, end_str = today_memo_range_utc(now)
    start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

    assert (end - start) != timedelta(hours=24)


def test_get_today_memos_for_prompt_adds_local_time(tmp_path, monkeypatch):
    monkeypatch.setattr(timezone_state, "get_timezone", lambda: "UTC")
    db = _db(tmp_path)
    db.upsert_memo(client_id="m1", content="喝咖啡",
                   occurred_at="2026-09-18T05:30:00.000Z")
    now = datetime(2026, 9, 18, 10, 0, 0)

    memos = get_today_memos_for_prompt(db, now=now)

    assert len(memos) == 1
    assert memos[0]["local_time"] == "05:30"
    assert memos[0]["content"] == "喝咖啡"


def test_get_today_memos_for_prompt_omits_location_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(timezone_state, "get_timezone", lambda: "UTC")
    db = _db(tmp_path)
    db.upsert_memo(
        client_id="m-loc", content="在公园散步", occurred_at="2026-09-18T05:30:00.000Z",
        latitude=-33.9173, longitude=151.2313, place_name="Centennial Park",
    )
    now = datetime(2026, 9, 18, 10, 0, 0)
    memos = get_today_memos_for_prompt(db, now=now)
    assert len(memos) == 1
    assert "latitude" not in memos[0]
    assert "longitude" not in memos[0]
    assert "place_name" not in memos[0]
    assert memos[0]["content"] == "在公园散步"


def test_get_today_memos_for_prompt_omits_tag_client_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(timezone_state, "get_timezone", lambda: "UTC")
    db = _db(tmp_path)
    t = db.upsert_tag(client_id="t-prompt", name="日记")
    db.upsert_memo(
        client_id="m-tag", content="写了一篇日记", occurred_at="2026-09-18T05:30:00.000Z",
        tag_ids=[int(t["id"])],
    )
    now = datetime(2026, 9, 18, 10, 0, 0)
    memos = get_today_memos_for_prompt(db, now=now)
    assert len(memos) == 1
    assert "tag_client_ids" not in memos[0]
    assert memos[0]["content"] == "写了一篇日记"


# ────────────────────────────────────────────────────────────────
# bot.prompts._format_today_memos
# ────────────────────────────────────────────────────────────────

def test_format_today_memos_empty_vanishes_and_data_has_label():
    assert _format_today_memos(None) == ""
    assert _format_today_memos([]) == ""

    rendered = _format_today_memos([{"local_time": "09:30", "content": "写代码"}])
    assert rendered.startswith(LABEL_TODAY_MEMOS)
    assert "09:30 | 写代码" in rendered


def test_format_today_memos_does_not_contain_location_info(tmp_path, monkeypatch):
    monkeypatch.setattr(timezone_state, "get_timezone", lambda: "UTC")
    db = _db(tmp_path)
    db.upsert_memo(
        client_id="m-loc2", content="吃午饭", occurred_at="2026-09-18T05:30:00.000Z",
        latitude=-33.9173, longitude=151.2313, place_name="Kensington",
    )
    now = datetime(2026, 9, 18, 10, 0, 0)
    rendered = _format_today_memos(get_today_memos_for_prompt(db, now=now))
    assert "Kensington" not in rendered
    assert "-33.9173" not in rendered
    assert "151.2313" not in rendered
    assert "吃午饭" in rendered


# ────────────────────────────────────────────────────────────────
# HTTP 层：/api/memos
# ────────────────────────────────────────────────────────────────

class MemosApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmpdir.name, "memos_api.db"))
        self._original_db = server.db
        self._original_memory = server.memory
        server.set_database(self.db)

        self._env_patcher = patch.dict(os.environ, {API_KEY_ENV: TEST_KEY}, clear=True)
        self._env_patcher.start()

        # 固定、单调递增的假时钟：避免 PATCH 紧跟 POST 落在同一毫秒导致的偶发失败。
        ticks = count()
        self._clock_patcher = patch.object(
            database_module, "utc_now_iso",
            side_effect=lambda: f"2026-09-18T04:20:{next(ticks):02d}.000Z",
        )
        self._clock_patcher.start()

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self._clock_patcher.stop()
        self._env_patcher.stop()
        server.db = self._original_db
        server.memory = self._original_memory
        self._tmpdir.cleanup()

    async def _post_memo(self, **overrides):
        body = {
            "client_id": "c-1",
            "content": "测试 memo #咖啡",
            "occurred_at": "2026-09-18T04:20:00.000Z",
        }
        body.update(overrides)
        return await self.client.post(
            "/api/memos", json=body, headers={"X-API-Key": TEST_KEY},
        )

    async def test_post_same_client_id_twice_is_idempotent(self):
        first = await self._post_memo(client_id="dup-1")
        second = await self._post_memo(client_id="dup-1", content="改过的内容")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(second.json()["content"], "测试 memo #咖啡")

    async def test_post_without_timezone_is_rejected(self):
        response = await self._post_memo(client_id="no-tz", occurred_at="2026-09-18T04:20:00")
        self.assertEqual(response.status_code, 400)

    async def test_response_shape_has_string_id_and_millisecond_utc_timestamps(self):
        memo = (await self._post_memo(client_id="shape-1")).json()
        self.assertIsInstance(memo["id"], str)
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
        self.assertRegex(memo["created_at"], pattern)
        self.assertRegex(memo["updated_at"], pattern)

    async def test_patch_updates_content_and_bumps_updated_at(self):
        created = (await self._post_memo(client_id="patch-1")).json()
        response = await self.client.patch(
            f"/api/memos/{created['id']}", json={"content": "改了"},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(response.status_code, 200)
        patched = response.json()
        self.assertEqual(patched["content"], "改了")
        self.assertGreater(patched["updated_at"], created["updated_at"])

    async def test_patch_after_delete_returns_404(self):
        created = (await self._post_memo(client_id="patch-404")).json()
        await self.client.delete(f"/api/memos/{created['id']}", headers={"X-API-Key": TEST_KEY})

        response = await self.client.patch(
            f"/api/memos/{created['id']}", json={"content": "再改"},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(response.status_code, 404)

    async def test_delete_is_idempotent_and_get_still_returns_the_row(self):
        created = (await self._post_memo(client_id="delete-1")).json()
        memo_id = created["id"]

        first = await self.client.delete(f"/api/memos/{memo_id}", headers={"X-API-Key": TEST_KEY})
        second = await self.client.delete(f"/api/memos/{memo_id}", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 204)
        self.assertEqual(first.content, b"")

        listing = await self.client.get("/api/memos", headers={"X-API-Key": TEST_KEY})
        items = listing.json()["items"]
        match = next(item for item in items if item["id"] == memo_id)
        self.assertIsNotNone(match["deleted_at"])

    async def test_get_paginates_without_duplicates_or_gaps(self):
        for i in range(5):
            await self._post_memo(client_id=f"page-{i}")

        seen = []
        cursor = None
        for _ in range(10):
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            response = await self.client.get(
                "/api/memos", params=params, headers={"X-API-Key": TEST_KEY},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            seen.extend(item["id"] for item in payload["items"])
            if not payload["has_more"]:
                break
            cursor = payload["next_cursor"]

        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 5)

    async def test_get_with_invalid_cursor_returns_400(self):
        response = await self.client.get(
            "/api/memos", params={"cursor": "not-valid-base64!!"},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(response.status_code, 400)

    async def test_requires_api_key(self):
        response = await self.client.get("/api/memos")
        self.assertEqual(response.status_code, 401)

    async def test_post_with_location_returns_200_and_location_fields(self):
        response = await self._post_memo(
            client_id="loc-post-1",
            latitude=-33.9173,
            longitude=151.2313,
            place_name="Kensington",
        )
        self.assertEqual(response.status_code, 200)
        memo = response.json()
        self.assertEqual(memo["latitude"], -33.9173)
        self.assertEqual(memo["longitude"], 151.2313)
        self.assertEqual(memo["place_name"], "Kensington")

    async def test_post_without_location_has_null_location_fields(self):
        response = await self._post_memo(client_id="loc-post-2")
        self.assertEqual(response.status_code, 200)
        memo = response.json()
        self.assertIn("latitude", memo)
        self.assertIsNone(memo["latitude"])
        self.assertIn("longitude", memo)
        self.assertIsNone(memo["longitude"])
        self.assertIn("place_name", memo)
        self.assertIsNone(memo["place_name"])

    async def test_post_latitude_without_longitude_returns_400(self):
        response = await self._post_memo(client_id="loc-post-3", latitude=-33.9173)
        self.assertEqual(response.status_code, 400)

    async def test_post_invalid_location_values_return_400(self):
        r1 = await self._post_memo(client_id="inv-1", latitude=91, longitude=100)
        self.assertEqual(r1.status_code, 400)

        r2 = await self._post_memo(client_id="inv-2", latitude=0, longitude=-181)
        self.assertEqual(r2.status_code, 400)

        r3 = await self._post_memo(client_id="inv-3", latitude=True, longitude=100)
        self.assertEqual(r3.status_code, 400)

    async def test_post_place_name_with_null_coordinates_forces_place_name_to_null(self):
        response = await self._post_memo(
            client_id="loc-post-4",
            place_name="Kensington",
        )
        self.assertEqual(response.status_code, 200)
        memo = response.json()
        self.assertIsNone(memo["latitude"])
        self.assertIsNone(memo["longitude"])
        self.assertIsNone(memo["place_name"])

    async def test_patch_clear_location_returns_200_and_bumps_updated_at(self):
        created = (await self._post_memo(
            client_id="loc-patch-1",
            latitude=-33.9173,
            longitude=151.2313,
            place_name="Kensington",
        )).json()
        response = await self.client.patch(
            f"/api/memos/{created['id']}",
            json={"latitude": None, "longitude": None, "place_name": None},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(response.status_code, 200)
        patched = response.json()
        self.assertIsNone(patched["latitude"])
        self.assertIsNone(patched["longitude"])
        self.assertIsNone(patched["place_name"])
        self.assertGreater(patched["updated_at"], created["updated_at"])

    async def test_patch_content_only_preserves_location(self):
        created = (await self._post_memo(
            client_id="loc-patch-2",
            latitude=-33.9173,
            longitude=151.2313,
            place_name="Kensington",
        )).json()
        response = await self.client.patch(
            f"/api/memos/{created['id']}",
            json={"content": "仅修改内容"},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(response.status_code, 200)
        patched = response.json()
        self.assertEqual(patched["content"], "仅修改内容")
        self.assertEqual(patched["latitude"], -33.9173)
        self.assertEqual(patched["longitude"], 151.2313)
        self.assertEqual(patched["place_name"], "Kensington")

    async def test_get_memos_incremental_includes_location_fields(self):
        await self._post_memo(
            client_id="loc-get-1",
            latitude=-33.9173,
            longitude=151.2313,
            place_name="Kensington",
        )
        response = await self.client.get("/api/memos", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        match = next(item for item in items if item["client_id"] == "loc-get-1")
        self.assertEqual(match["latitude"], -33.9173)
        self.assertEqual(match["longitude"], 151.2313)
        self.assertEqual(match["place_name"], "Kensington")

    async def test_post_memo_with_and_without_tags(self):
        # 先建一个标签
        await self.client.post(
            "/api/tags",
            json={"client_id": "tag-http-1", "name": "标签1"},
            headers={"X-API-Key": TEST_KEY},
        )
        # 带 tag_client_ids 新建
        res1 = await self._post_memo(client_id="m-tag-http", tag_client_ids=["tag-http-1"])
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["tag_client_ids"], ["tag-http-1"])

        # 不带 tag_client_ids 新建
        res2 = await self._post_memo(client_id="m-no-tag")
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["tag_client_ids"], [])

    async def test_post_memo_with_nonexistent_tag_returns_400(self):
        res = await self._post_memo(client_id="m-bad-tag", tag_client_ids=["nonexistent-tag"])
        self.assertEqual(res.status_code, 400)
        self.assertIn("nonexistent-tag", res.json()["detail"])

    async def test_post_memo_ignores_deleted_tag(self):
        for cid, name in (("tag-live", "还在"), ("tag-to-del", "将被删")):
            await self.client.post(
                "/api/tags", json={"client_id": cid, "name": name},
                headers={"X-API-Key": TEST_KEY},
            )
        await self.client.delete("/api/tags/tag-to-del", headers={"X-API-Key": TEST_KEY})
        res = await self._post_memo(client_id="m-del-tag", tag_client_ids=["tag-live", "tag-to-del"])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["tag_client_ids"], ["tag-live"])

    async def test_retry_post_after_tag_deleted_returns_existing_memo(self):
        """首次 POST 已写入但响应丢失，重试前标签在别处被删：重试必须成功，不能卡住同步队列。"""
        await self.client.post(
            "/api/tags", json={"client_id": "tag-retry", "name": "重试"},
            headers={"X-API-Key": TEST_KEY},
        )
        first = (await self._post_memo(client_id="m-retry", tag_client_ids=["tag-retry"])).json()
        await self.client.delete("/api/tags/tag-retry", headers={"X-API-Key": TEST_KEY})

        retry = await self._post_memo(client_id="m-retry", tag_client_ids=["tag-retry"])
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["id"], first["id"])
        self.assertEqual(retry.json()["tag_client_ids"], [])

    async def test_patch_memo_ignores_deleted_tag(self):
        await self.client.post(
            "/api/tags", json={"client_id": "tag-patch-del", "name": "离线时被删"},
            headers={"X-API-Key": TEST_KEY},
        )
        await self.client.delete("/api/tags/tag-patch-del", headers={"X-API-Key": TEST_KEY})
        created = (await self._post_memo(client_id="m-patch-del")).json()
        res = await self.client.patch(
            f"/api/memos/{created['id']}",
            json={"tag_client_ids": ["tag-patch-del"]},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["tag_client_ids"], [])

    async def test_tag_client_ids_shape_validation(self):
        for bad in (None, "tag-a", [1], ["ok", None]):
            res = await self._post_memo(client_id="m-shape", tag_client_ids=bad)
            self.assertEqual(res.status_code, 400, bad)
        too_many = [f"t{i}" for i in range(21)]
        res = await self._post_memo(client_id="m-too-many", tag_client_ids=too_many)
        self.assertEqual(res.status_code, 400)

    async def test_patch_memo_clear_tags_only(self):
        await self.client.post(
            "/api/tags",
            json={"client_id": "tag-clear", "name": "待清空"},
            headers={"X-API-Key": TEST_KEY},
        )
        created = (await self._post_memo(client_id="m-clear-tags", tag_client_ids=["tag-clear"])).json()
        self.assertEqual(created["tag_client_ids"], ["tag-clear"])

        # 只发送 {"tag_client_ids": []} → 200，标签被清空，updated_at 变大
        response = await self.client.patch(
            f"/api/memos/{created['id']}",
            json={"tag_client_ids": []},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(response.status_code, 200)
        patched = response.json()
        self.assertEqual(patched["tag_client_ids"], [])
        self.assertGreater(patched["updated_at"], created["updated_at"])

    async def test_patch_memo_content_only_preserves_tags(self):
        await self.client.post(
            "/api/tags",
            json={"client_id": "tag-keep", "name": "保持"},
            headers={"X-API-Key": TEST_KEY},
        )
        created = (await self._post_memo(client_id="m-keep-tags", tag_client_ids=["tag-keep"])).json()

        response = await self.client.patch(
            f"/api/memos/{created['id']}",
            json={"content": "改内容"},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(response.status_code, 200)
        patched = response.json()
        self.assertEqual(patched["content"], "改内容")
        self.assertEqual(patched["tag_client_ids"], ["tag-keep"])

    async def test_get_memos_incremental_includes_tag_client_ids(self):
        await self.client.post(
            "/api/tags",
            json={"client_id": "tag-inc", "name": "增量"},
            headers={"X-API-Key": TEST_KEY},
        )
        await self._post_memo(client_id="m-inc-tag", tag_client_ids=["tag-inc"])
        response = await self.client.get("/api/memos", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        match = next(item for item in items if item["client_id"] == "m-inc-tag")
        self.assertEqual(match["tag_client_ids"], ["tag-inc"])

    async def test_incremental_sync_after_tag_soft_deleted(self):
        # 删除一个标签之后，用删除前的 cursor 增量拉取，能拉到挂过这个标签的 memo，而且里面已经没有这个标签
        await self.client.post(
            "/api/tags",
            json={"client_id": "tag-sync-del", "name": "拉取删标签"},
            headers={"X-API-Key": TEST_KEY},
        )
        await self._post_memo(client_id="m-sync-del", tag_client_ids=["tag-sync-del"])
        # 获取当前的 cursor
        res_before = await self.client.get("/api/memos", headers={"X-API-Key": TEST_KEY})
        cursor_before = res_before.json()["next_cursor"]

        # 删除标签
        await self.client.delete("/api/tags/tag-sync-del", headers={"X-API-Key": TEST_KEY})

        # 用删除前的 cursor 增量拉取
        res_after = await self.client.get(f"/api/memos?cursor={cursor_before}", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(res_after.status_code, 200)
        items_after = res_after.json()["items"]
        match = next((item for item in items_after if item["client_id"] == "m-sync-del"), None)
        self.assertIsNotNone(match)
        self.assertEqual(match["tag_client_ids"], [])


if __name__ == "__main__":
    unittest.main()
