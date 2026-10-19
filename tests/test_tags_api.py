"""tags 表与 /api/tags 接口的契约测试。

Database 层用 pytest 函数 + tmp_path；
HTTP 层用 httpx + IsolatedAsyncioTestCase 测 FastAPI 端点。
"""
import os
import re
import tempfile
import unittest
from itertools import count
from unittest.mock import patch

import httpx

from api import server
from api.auth import API_KEY_ENV
import bot.database as database_module
from bot.database import Database


TEST_KEY = "k" * 32


def _db(tmp_path) -> Database:
    return Database(str(tmp_path / "tags.db"))


# ────────────────────────────────────────────────────────────────
# Database 层
# ────────────────────────────────────────────────────────────────

def test_upsert_tag_is_idempotent_on_client_id(tmp_path):
    db = _db(tmp_path)
    first = db.upsert_tag(client_id="t1", name="娱乐", color="#E8A0BF")
    second = db.upsert_tag(client_id="t1", name="改过的名称", color="#000000")
    assert first == second
    assert second["name"] == "娱乐"
    assert second["color"] == "#E8A0BF"


def test_list_tags_includes_soft_deleted_and_parent_client_id(tmp_path):
    db = _db(tmp_path)
    p = db.upsert_tag(client_id="p1", name="娱乐")
    c = db.upsert_tag(client_id="c1", name="电影", parent_id=int(p["id"]))
    db.soft_delete_tag("c1")

    items = db.list_tags()
    assert len(items) == 2
    c_item = next(it for it in items if it["client_id"] == "c1")
    assert c_item["deleted_at"] is not None
    assert c_item["parent_client_id"] == "p1"


def test_soft_delete_tag_returns_false_on_repeat(tmp_path):
    db = _db(tmp_path)
    db.upsert_tag(client_id="t1", name="工作")
    assert db.soft_delete_tag("t1") is True
    assert db.soft_delete_tag("t1") is False
    assert db.soft_delete_tag("nonexistent") is False


# ────────────────────────────────────────────────────────────────
# HTTP 层：/api/tags
# ────────────────────────────────────────────────────────────────

class TagsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmpdir.name, "tags_api.db"))
        self._original_db = server.db
        self._original_memory = server.memory
        server.set_database(self.db)

        self._env_patcher = patch.dict(os.environ, {API_KEY_ENV: TEST_KEY}, clear=True)
        self._env_patcher.start()

        ticks = count()
        self._clock_patcher = patch.object(
            database_module, "utc_now_iso",
            side_effect=lambda: f"2026-10-06T08:00:{next(ticks):02d}.000Z",
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

    async def test_post_tag_and_idempotent(self):
        body = {
            "client_id": "tag-ent",
            "name": "娱乐",
            "color": "#E8A0BF",
        }
        res = await self.client.post("/api/tags", json=body, headers={"X-API-Key": TEST_KEY})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["client_id"], "tag-ent")
        self.assertEqual(data["name"], "娱乐")
        self.assertEqual(data["color"], "#E8A0BF")
        self.assertIsNone(data["parent_client_id"])
        self.assertFalse(data["is_timed"])

        # 同一个 client_id 再 POST 一次，返回第一次的内容
        res2 = await self.client.post(
            "/api/tags",
            json={"client_id": "tag-ent", "name": "新名称", "color": "#112233"},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["name"], "娱乐")
        self.assertEqual(res2.json()["color"], "#E8A0BF")

    async def _post_tag(self, **body):
        return await self.client.post("/api/tags", json=body, headers={"X-API-Key": TEST_KEY})

    async def _patch_tag(self, client_id, **body):
        return await self.client.patch(
            f"/api/tags/{client_id}", json=body, headers={"X-API-Key": TEST_KEY},
        )

    async def _make_tag(self, **body):
        res = await self._post_tag(**body)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    async def test_post_tag_name_validation(self):
        for bad in ("   ", "a" * 31, "娱乐/电影", None, 123):
            res = await self._post_tag(name=bad)
            self.assertEqual(res.status_code, 400, bad)
        self.assertEqual((await self._post_tag(name="a" * 30)).status_code, 200)

    async def test_post_tag_color_validation(self):
        for bad in ("red", "#FFF", "E8A0BF", "#GGGGGG"):
            res = await self._post_tag(name=f"c-{bad}", color=bad)
            self.assertEqual(res.status_code, 400, bad)

    async def test_post_tag_is_timed_must_be_bool(self):
        res = await self._post_tag(name="计时", is_timed=1, started_at="2026-10-01T00:00:00Z")
        self.assertEqual(res.status_code, 400)

    async def test_post_tag_timed_requires_started_at(self):
        res = await self._post_tag(name="计时", is_timed=True)
        self.assertEqual(res.status_code, 400)
        ok = await self._make_tag(name="计时", is_timed=True, started_at="2026-10-01T10:00:00+10:00")
        self.assertEqual(ok["started_at"], "2026-10-01T00:00:00.000Z")

    async def test_post_tag_untimed_forces_started_at_null(self):
        tag = await self._make_tag(name="非计时", started_at="2026-10-01T00:00:00Z")
        self.assertIsNone(tag["started_at"])

    async def test_post_tag_non_string_client_id_returns_400(self):
        res = await self._post_tag(client_id=123, name="x")
        self.assertEqual(res.status_code, 400)

    async def test_duplicate_name_under_same_parent_returns_409(self):
        await self._make_tag(client_id="p1", name="一级")
        self.assertEqual((await self._post_tag(client_id="p2", name="一级")).status_code, 409)

        await self._make_tag(client_id="c1", name="二级", parent_client_id="p1")
        self.assertEqual(
            (await self._post_tag(client_id="c2", name="二级", parent_client_id="p1")).status_code, 409,
        )
        # 不同父标签下同名是允许的
        await self._make_tag(client_id="p3", name="另一个一级")
        await self._make_tag(client_id="c3", name="二级", parent_client_id="p3")

    async def test_duplicate_name_allowed_after_soft_delete(self):
        await self._make_tag(client_id="d1", name="复用")
        await self.client.delete("/api/tags/d1", headers={"X-API-Key": TEST_KEY})
        await self._make_tag(client_id="d2", name="复用")

    async def test_cannot_nest_three_levels(self):
        await self._make_tag(client_id="p1", name="一级")
        await self._make_tag(client_id="sub1", name="二级", parent_client_id="p1")
        res = await self._post_tag(client_id="sub2", name="三级", parent_client_id="sub1")
        self.assertEqual(res.status_code, 400)

    async def test_parent_must_exist_and_not_be_deleted(self):
        self.assertEqual(
            (await self._post_tag(name="孤儿", parent_client_id="nope")).status_code, 400,
        )
        await self._make_tag(client_id="gone", name="已删")
        await self.client.delete("/api/tags/gone", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(
            (await self._post_tag(name="孤儿2", parent_client_id="gone")).status_code, 400,
        )

    async def test_patch_parent_to_self_returns_400(self):
        await self._make_tag(client_id="self", name="自己")
        res = await self._patch_tag("self", parent_client_id="self")
        self.assertEqual(res.status_code, 400)

    async def test_patch_parent_on_tag_with_children_returns_400(self):
        await self._make_tag(client_id="a", name="A")
        await self._make_tag(client_id="a-child", name="A的子", parent_client_id="a")
        await self._make_tag(client_id="b", name="B")
        res = await self._patch_tag("a", parent_client_id="b")
        self.assertEqual(res.status_code, 400)

    async def test_patch_move_tag_between_parents_and_to_top_level(self):
        await self._make_tag(client_id="a", name="A")
        await self._make_tag(client_id="b", name="B")
        await self._make_tag(client_id="x", name="X", parent_client_id="a")

        moved = await self._patch_tag("x", parent_client_id="b")
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json()["parent_client_id"], "b")

        top = await self._patch_tag("x", parent_client_id=None)
        self.assertEqual(top.status_code, 200)
        self.assertIsNone(top.json()["parent_client_id"])

    async def test_patch_is_timed_false_clears_started_at(self):
        await self._make_tag(client_id="t", name="计时", is_timed=True, started_at="2026-10-01T00:00:00Z")
        res = await self._patch_tag("t", is_timed=False)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["is_timed"])
        self.assertIsNone(res.json()["started_at"])

    async def test_patch_keeps_unmentioned_fields(self):
        await self._make_tag(client_id="p", name="父")
        await self._make_tag(
            client_id="k", name="保持", parent_client_id="p", color="#112233",
            is_timed=True, started_at="2026-10-01T00:00:00Z",
        )
        res = await self._patch_tag("k", color="#445566")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["name"], "保持")
        self.assertEqual(data["parent_client_id"], "p")
        self.assertTrue(data["is_timed"])
        self.assertEqual(data["started_at"], "2026-10-01T00:00:00.000Z")
        self.assertEqual(data["color"], "#445566")

    async def test_patch_rename_to_sibling_name_returns_409(self):
        await self._make_tag(client_id="n1", name="甲")
        await self._make_tag(client_id="n2", name="乙")
        self.assertEqual((await self._patch_tag("n2", name="甲")).status_code, 409)
        # 改成自己现在的名字不算重名
        self.assertEqual((await self._patch_tag("n2", name="乙")).status_code, 200)

    async def test_patch_deleted_tag_returns_404(self):
        await self._make_tag(client_id="del", name="删")
        await self.client.delete("/api/tags/del", headers={"X-API-Key": TEST_KEY})
        self.assertEqual((await self._patch_tag("del", name="复活")).status_code, 404)

    async def test_get_tags_includes_deleted(self):
        await self._make_tag(client_id="live", name="在")
        await self._make_tag(client_id="dead", name="删")
        await self.client.delete("/api/tags/dead", headers={"X-API-Key": TEST_KEY})
        res = await self.client.get("/api/tags", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(res.status_code, 200)
        by_id = {t["client_id"]: t for t in res.json()["items"]}
        self.assertIsNone(by_id["live"]["deleted_at"])
        self.assertIsNotNone(by_id["dead"]["deleted_at"])

    async def test_patch_tag_not_found(self):
        res = await self.client.patch("/api/tags/not-exist", json={"name": "新名"}, headers={"X-API-Key": TEST_KEY})
        self.assertEqual(res.status_code, 404)

    async def test_patch_tag_success(self):
        created = (await self.client.post(
            "/api/tags",
            json={"client_id": "t-patch", "name": "原名", "color": "#112233"},
            headers={"X-API-Key": TEST_KEY},
        )).json()

        res = await self.client.patch(
            "/api/tags/t-patch",
            json={"name": "改名", "color": "#445566"},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["name"], "改名")
        self.assertEqual(data["color"], "#445566")
        self.assertGreater(data["updated_at"], created["updated_at"])

    async def test_delete_tag_hierarchy_conflict_and_idempotence(self):
        # DELETE 还有子标签的父标签 → 409；先删子标签、再删父标签 → 两次都是 204；再删一次 → 204
        await self.client.post("/api/tags", json={"client_id": "del-p", "name": "父标签"}, headers={"X-API-Key": TEST_KEY})
        await self.client.post(
            "/api/tags",
            json={"client_id": "del-c", "name": "子标签", "parent_client_id": "del-p"},
            headers={"X-API-Key": TEST_KEY},
        )

        # 删父标签 → 409
        res = await self.client.delete("/api/tags/del-p", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(res.status_code, 409)

        # 删子标签 → 204
        res_c = await self.client.delete("/api/tags/del-c", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(res_c.status_code, 204)

        # 再次删父标签 → 204
        res_p = await self.client.delete("/api/tags/del-p", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(res_p.status_code, 204)

        # 再删一次父标签 → 204 幂等
        res_p2 = await self.client.delete("/api/tags/del-p", headers={"X-API-Key": TEST_KEY})
        self.assertEqual(res_p2.status_code, 204)


if __name__ == "__main__":
    unittest.main()
