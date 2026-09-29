"""对话卡片的读取接口（/api/cards）。

App 的聊天卡片界面靠它取数：卡片列表要能做渐隐效果，消息要是用户真正打出来的
原文，而不是给模型准备的那份带前缀、带引用块的表示。

HTTP 层沿用 tests/test_memos_api.py 的 httpx + IsolatedAsyncioTestCase 写法。
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx

import config
from api import server
from api.auth import API_KEY_ENV
from bot.cards import CardService
from bot.database import Database
from bot.memory import MemoryService

TEST_KEY = "k" * 32
CHANNEL = "123"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta


class CardsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmpdir.name, "cards_api.db"))
        self._original_db = server.db
        self._original_memory = server.memory
        server.set_database(self.db)

        self._env_patcher = patch.dict(os.environ, {API_KEY_ENV: TEST_KEY}, clear=True)
        self._env_patcher.start()
        self._channel_patcher = patch.object(config, "CHANNEL_ID", int(CHANNEL))
        self._channel_patcher.start()

        self.clock = Clock()
        self.cards = CardService(self.db, now=self.clock)
        self.memory = MemoryService(self.db)

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self._channel_patcher.stop()
        self._env_patcher.stop()
        server.db = self._original_db
        server.memory = self._original_memory
        self._tmpdir.cleanup()

    # --- 构造数据的辅助 ---------------------------------------------------

    def _msg(self, discord_id, content, role="assistant", reply_to=None):
        return dict(
            discord_message_id=str(discord_id), channel_id=CHANNEL, role=role,
            content=content, created_at="2026-09-27T10:00:00+00:00",
            reply_to_message_id=reply_to,
        )

    async def _send(self, discord_id, content, source_type, source_id):
        await self.cards.record_outbound(
            self._msg(discord_id, content), source_type=source_type,
            source_id=source_id, ingest=self.memory.ingest_message,
        )

    def _reply(self, discord_id, content, reply_to=None, current_content=None):
        card_id = self.cards.card_for_user_message(CHANNEL, reply_to)
        metadata = {"current_content": current_content} if current_content else None
        self.db.add_conversation_message(
            **self._msg(discord_id, content, "user", reply_to),
            metadata=metadata, card_id=card_id,
        )
        return card_id

    async def _get(self, path):
        return await self.client.get(path, headers={"X-API-Key": TEST_KEY})

    # --- 列表 -------------------------------------------------------------

    async def test_unanswered_card_is_not_listed(self):
        """没被回复过的卡片还没进对话历史，不应该出现在列表里。"""
        await self._send(1, "今天打算做什么？", "check_in", "c1")

        body = (await self._get("/api/cards")).json()

        self.assertEqual(body["items"], [])
        self.assertIsNone(body["current_card_id"])

    async def test_replied_card_is_listed_with_label_and_counts(self):
        await self._send(1, "今天打算做什么？", "check_in", "c1")
        card_id = self._reply(2, "下午去爬山", reply_to="1")

        body = (await self._get("/api/cards")).json()

        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["id"], card_id)
        self.assertEqual(item["label"], "check_in1")
        self.assertEqual(item["source_type"], "check_in")
        # AI 的开场白在卡片被回复时一起进了历史，所以是两条
        self.assertEqual(item["message_count"], 2)
        self.assertFalse(item["faded"])
        self.assertTrue(item["is_current"])
        self.assertEqual(body["current_card_id"], card_id)

    async def test_faded_card_is_returned_with_a_flag(self):
        """过期隐去的卡片仍要返回，App 靠 faded 做渐隐，不是直接不给。

        接口自己构造 CardService，用的是真实时间，所以这里直接把过期时刻
        改到过去，而不是拨测试时钟。
        """
        await self._send(1, "今天打算做什么？", "check_in", "c1")
        old = self._reply(2, "下午去爬山", reply_to="1")
        self.db.touch_card(old, now="2026-09-20T10:00:00+00:00",
                           expires_at="2026-09-23T10:00:00+00:00")

        body = (await self._get("/api/cards")).json()

        faded = [i for i in body["items"] if i["id"] == old]
        self.assertEqual(len(faded), 1)
        self.assertTrue(faded[0]["faded"])
        self.assertIn("expires_at", faded[0])

    async def test_limit_is_clamped(self):
        response = await self._get("/api/cards?limit=99999")
        self.assertEqual(response.status_code, 200)

    # --- 单张卡片 ---------------------------------------------------------

    async def test_get_single_card(self):
        await self._send(1, "今天打算做什么？", "check_in", "c1")
        card_id = self._reply(2, "下午去爬山", reply_to="1")

        body = (await self._get(f"/api/cards/{card_id}")).json()

        self.assertEqual(body["id"], card_id)
        self.assertEqual(body["label"], "check_in1")

    async def test_missing_card_is_404(self):
        self.assertEqual((await self._get("/api/cards/9999")).status_code, 404)

    async def test_unanswered_card_detail_is_404(self):
        await self._send(1, "今天打算做什么？", "check_in", "c1")
        pending = self.db.get_card_by_source("check_in", "c1")["id"]

        self.assertEqual((await self._get(f"/api/cards/{pending}")).status_code, 404)

    # --- 卡片里的消息 -----------------------------------------------------

    async def test_messages_are_raw_not_the_model_facing_representation(self):
        """界面要显示用户真正打出来的那句话。

        给模型的那份表示会加 [时间] 前缀，并且把用户消息换成 current_content，
        里面还带着 discord_bot 内联的 `[回复 …]` 引用块。接口不能返回那一份。
        """
        await self._send(1, "今天打算做什么？", "check_in", "c1")
        card_id = self._reply(
            2, "下午去爬山", reply_to="1",
            current_content='[2026-09-27 10:05] [回复 你说过 的消息: "今天打算做什么？"]\n下午去爬山',
        )

        items = (await self._get(f"/api/cards/{card_id}/messages")).json()["items"]

        self.assertEqual([i["role"] for i in items], ["assistant", "user"])
        self.assertEqual(items[1]["content"], "下午去爬山")
        self.assertNotIn("[回复 ", items[1]["content"])
        self.assertFalse(items[0]["content"].startswith("["))
        # 正序，并且带上界面需要的字段
        self.assertLess(items[0]["id"], items[1]["id"])
        self.assertEqual(items[1]["reply_to_message_id"], "1")

    async def test_messages_of_a_missing_card_is_404(self):
        self.assertEqual(
            (await self._get("/api/cards/9999/messages")).status_code, 404)

    async def test_messages_require_auth(self):
        response = await self.client.get("/api/cards")
        self.assertEqual(response.status_code, 401)
