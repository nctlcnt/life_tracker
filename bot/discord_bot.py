"""
Discord 机器人模块
负责接收和发送 Discord 消息，注册斜杠命令
"""
import hashlib
import re
import discord
from dataclasses import dataclass
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from bot.async_pipeline import (
    BatchCoordinator,
    DeliveryFailed,
    NullGenerationGate,
    OutboundQueue,
)
from bot.ai_engine import chat, simple_completion
from bot.card_summary import schedule_summary
from bot.cards import CardService, is_proactive, unique_source_id
from bot.memory import MemoryService
from bot.weather import get_weather_brief, get_weather_detailed, geocode_address
from bot.prompts import get_prompt_template
from bot.database import Database
from bot.tools import SET_TOOL_NAMES
from bot.logger import get_logger
import config

logger = get_logger(__name__)
_TYPING_COOLDOWN = timedelta(minutes=5)
_LOCALHOST_CALLBACK_RE = re.compile(r"https?://localhost:\d+/\?\S*")

@dataclass
class _CalendarAuthSession:
    flow: object
    created_at: datetime


def _is_rate_limited_error(exc: Exception) -> bool:
    return isinstance(exc, discord.RateLimited) or (
        isinstance(exc, discord.HTTPException) and exc.status == 429
    )


class LifeTrackerBot(commands.Bot):
    def __init__(self, db: Database, memory_service: MemoryService | None = None,
                 *, generation_gate=None,
                 outbound_queue: OutboundQueue | None = None,
                 batch_coordinator: BatchCoordinator | None = None,
                 tool_worker_apply: bool | None = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = db
        self.memory = memory_service or MemoryService(db)
        self.cards = CardService(db)
        self.generation_gate = generation_gate or NullGenerationGate()
        self.outbound_queue = outbound_queue
        self.batch_coordinator = batch_coordinator
        self.tool_worker_apply = (
            config.ASYNC_TOOL_APPLY
            if tool_worker_apply is None
            else bool(tool_worker_apply)
        )
        self.last_typing_at: datetime | None = None  # 目标用户最近 typing 时刻（UTC aware）
        # typing 被 429 限流后的冷却截止时间（按 channel_id），命中时直接跳过 typing 不再撞 API
        self._typing_cooldown_until: dict[int, datetime] = {}
        # 由 main.py 注入：chat 完成时调用，用于重置 scheduler 的 poll 基准时间
        self.on_ai_call_done = None
        self.calendar_auth_session: _CalendarAuthSession | None = None

    def set_outbound_queue(self, outbound_queue: OutboundQueue | None) -> None:
        self.outbound_queue = outbound_queue

    def set_batch_coordinator(
        self, batch_coordinator: BatchCoordinator | None
    ) -> None:
        self.batch_coordinator = batch_coordinator

    def _get_typing_cooldown_until(self, channel_id: int) -> datetime | None:
        until = self._typing_cooldown_until.get(channel_id)
        if not until:
            return None
        if until <= datetime.now(timezone.utc):
            self._typing_cooldown_until.pop(channel_id, None)
            return None
        return until

    def _is_typing_cooling_down(self, channel_id: int) -> bool:
        return self._get_typing_cooldown_until(channel_id) is not None

    def _mark_typing_cooldown(self, channel_id: int, reason: str) -> None:
        until = datetime.now(timezone.utc) + _TYPING_COOLDOWN
        current = self._typing_cooldown_until.get(channel_id)
        if current and current > until:
            until = current
        self._typing_cooldown_until[channel_id] = until
        logger.warning(f"⚠️ {reason}，进入 5min 冷却，直到 {until.isoformat()}")

    async def setup_hook(self):
        """注册斜杠命令并同步到 Discord"""
        self.tree.add_command(_calendar_group(self))
        self.tree.add_command(_conversation_group(self))
        self.tree.add_command(_weather_command(self))
        self.tree.add_command(_tz_command(self))
        await self.tree.sync()
        logger.info("✅ 斜杠命令已同步")

    async def on_ready(self):
        logger.info(f"✅ Discord Bot 已上线: {self.user} → channel {config.CHANNEL_ID}")

    async def on_typing(self, channel, user, when):
        """记录目标用户在目标频道的 typing 时刻，供随机轮询判断是否让路。"""
        if channel.id != config.CHANNEL_ID:
            return
        if config.ALLOWED_USER_ID and user.id != config.ALLOWED_USER_ID:
            return
        self.last_typing_at = when

    def is_user_typing(self, window_seconds: int = 10) -> bool:
        """最近 window_seconds 内目标用户是否在输入。"""
        if not self.last_typing_at:
            return False
        delta = (datetime.now(timezone.utc) - self.last_typing_at).total_seconds()
        return 0 <= delta <= window_seconds

    async def on_message(self, message: discord.Message):
        # 忽略自己的消息
        if message.author == self.user:
            return

        # 只响应指定用户
        if config.ALLOWED_USER_ID and message.author.id != config.ALLOWED_USER_ID:
            if message.channel.id == config.CHANNEL_ID:
                logger.info(
                    f"⏭️ 忽略消息：author_id={message.author.id} != allowed_user_id={config.ALLOWED_USER_ID}"
                )
            return

        # 只响应配置里指定的 channel（prod / staging 各自配置不同 id，多 bot 共存不会串台）
        if message.channel.id != config.CHANNEL_ID:
            logger.info(
                f"⏭️ 忽略消息：channel_id={message.channel.id} != configured_channel_id={config.CHANNEL_ID}"
            )
            return

        # 斜杠命令走 interaction，普通消息才到这里
        # 跳过斜杠命令的文本消息（防止重复处理）
        if message.content.startswith("/"):
            logger.info("⏭️ 忽略斜杠命令文本消息")
            return

        if await self._maybe_finish_calendar_auth(message):
            return

        logger.info(f"📨 收到消息: {message.author} ({message.author.id}): {message.content}")

        # 获取当前时间戳
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 处理回复（引用）消息 — 只对当前这条消息做富化，历史不追溯
        content_to_send = message.content
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg and ref_msg.content:
                    quote = ref_msg.content if len(ref_msg.content) < 200 else ref_msg.content[:200] + "..."
                    author_name = "你说过" if ref_msg.author == self.user else "我曾发过"
                    content_to_send = f'[回复 {author_name} 的消息: "{quote}"]\n{message.content}'
            except Exception as e:
                logger.warning(f"⚠️ 无法获取引用的消息: {e}")

        # 当前用户消息（带时间戳前缀，和历史消息格式一致）
        current_content = f"[{timestamp}] {content_to_send}"

        # 先确定这条消息属于哪张卡片。reply 的是一张 pending 卡片时，这一步会把
        # 卡片里 AI 的消息先写进对话历史，所以必须在写入用户消息之前完成。
        reply_to_id = (
            str(message.reference.message_id)
            if message.reference and message.reference.message_id
            else None
        )
        card_kwargs = {}
        try:
            card_kwargs["card_id"] = self.cards.card_for_user_message(
                str(message.channel.id), reply_to_id)
        except Exception as e:
            # 卡片出错不能挡住聊天本身：消息照常写入，只是不归属任何卡片
            logger.exception(f"⚠️ 卡片归属失败，消息不带卡片写入: {e}")

        # 备份到 DB（messages 表只作备份，AI 上下文走 DB conversation log）
        self.db.add_message("user", current_content)
        row_id = await self.memory.ingest_message(
            discord_message_id=str(message.id),
            channel_id=str(message.channel.id),
            guild_id=str(message.guild.id) if message.guild else None,
            author_id=str(message.author.id),
            author_name=str(message.author),
            role="user",
            content=message.content or "",
            created_at=message.created_at.isoformat(),
            reply_to_message_id=reply_to_id,
            metadata={
                "content_to_send": content_to_send,
                "current_content": current_content,
                "message_type": str(message.type),
            },
            **card_kwargs,
        )
        # 卡片有了新对话就在后台补总结。只在用户消息之后触发：卡片是被回复之后
        # 才转为 active 的，到这一步 AI 的开场白已经一起进了对话历史，总结读得到
        # 完整的一轮。失败不影响聊天，schedule_summary 自己吞掉异常。
        schedule_summary(self.db, card_kwargs.get("card_id"))

        if row_id is not None and self.batch_coordinator is not None:
            self.batch_coordinator.notify_user_message(
                str(message.channel.id), row_id
            )
        try:
            # 当前消息先持久化，再进入共享 gate 读取快照并生成。主动消息必须
            # 等这段结束后才能读取自己的上下文，因此不能抢在聊天回复前入队。
            async with self.generation_gate:
                try:
                    await self._generate_chat_response(message)
                except Exception as e:
                    error_msg = f"❌ {type(e).__name__}: {e}"
                    logger.exception(error_msg)
                    await self._send_or_enqueue_message(
                        message.channel,
                        error_msg[:2000],
                        source_type="chat_error",
                        source_id=str(message.id),
                        dedupe_key=f"chat:{message.id}:error",
                    )
        finally:
            # cache 钱已付，通知 scheduler 重置 poll 基准（45-55min 内不再轮询）
            if self.on_ai_call_done:
                self.on_ai_call_done()

    async def _generate_chat_response(self, message) -> None:
        """Generate one user-message response while the caller holds the gate."""
        # AI 上下文走 token 窗口（LT-135）：摘要 + 明文尾巴。
        window = self.memory.context_window(str(message.channel.id))
        ai_messages = window.messages
        delivery_index = 0

        async def send_reply(text):
            nonlocal delivery_index
            if "[SILENT]" in text:
                if self.batch_coordinator is not None:
                    outcome = self.batch_coordinator.force(
                        str(message.channel.id)
                    )
                    logger.info(
                        "🔇 chat [SILENT] 强制工具批次: %s",
                        outcome.get("action"),
                    )
                return
            delivery_index += 1
            await self._send_or_enqueue_message(
                message.channel,
                text,
                source_type="chat",
                source_id=str(message.id),
                dedupe_key=f"chat:{message.id}:message:{delivery_index}",
            )

        tool_called_flag = False

        async def on_tool_call(tool_names: list[str]):
            nonlocal tool_called_flag
            if tool_called_flag or not any(
                    name in SET_TOOL_NAMES for name in tool_names):
                return
            tool_called_flag = True
            try:
                await self._send_or_enqueue_reaction(
                    message,
                    "✅",
                    source_type="chat_tool_feedback",
                    source_id=str(message.id),
                    dedupe_key=f"chat:{message.id}:reaction:success",
                )
            except Exception as react_err:
                logger.warning(f"⚠️ 无法添加反馈 emoji: {react_err}")

        if self._is_typing_cooling_down(message.channel.id):
            # typing 限流冷却期，跳过 typing 直接 chat
            await chat(
                self.db, ai_messages, send_callback=send_reply,
                tool_callback=on_tool_call, memory_service=self.memory,
                window=window,
                tool_names=set() if self.tool_worker_apply else None,
            )
            return

        typing_cm = message.channel.typing()
        try:
            await typing_cm.__aenter__()
        except (discord.HTTPException, discord.RateLimited) as e:
            if not _is_rate_limited_error(e):
                raise
            self._mark_typing_cooldown(
                message.channel.id, f"Discord typing 入口限流: {e}")
            await chat(
                self.db, ai_messages, send_callback=send_reply,
                tool_callback=on_tool_call, memory_service=self.memory,
                window=window,
                tool_names=set() if self.tool_worker_apply else None,
            )
        else:
            try:
                await chat(
                    self.db, ai_messages, send_callback=send_reply,
                    tool_callback=on_tool_call, memory_service=self.memory,
                    window=window,
                    tool_names=set() if self.tool_worker_apply else None,
                )
            finally:
                await typing_cm.__aexit__(None, None, None)

    async def send_proactive_message(self, text: str, *,
                                     source_type: str = "scheduled",
                                     source_id: str = "unknown",
                                     dedupe_key: str | None = None):
        """主动发送 producer；outbox 开启时不直接接触 Discord transport。"""
        if not text or not text.strip():
            return
        if self.outbound_queue is not None:
            receipt = await self.outbound_queue.enqueue_message(
                channel_id=str(config.CHANNEL_ID),
                content=text,
                source_type=source_type,
                source_id=source_id,
                dedupe_key=dedupe_key or (
                    f"{source_type}:{source_id}:"
                    f"{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"),
            )
            if not receipt.delivered:
                raise DeliveryFailed(receipt)
            return receipt

        channel = await self._resolve_channel(str(config.CHANNEL_ID))
        logger.info(f"📨 主动发送到频道 {channel.id}")
        return await self._send_or_enqueue_message(
            channel,
            text,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=dedupe_key or f"direct:{source_type}:{source_id}",
        )

    async def _send_or_enqueue_message(self, target, text: str, *,
                                       source_type: str, source_id: str,
                                       dedupe_key: str):
        if self.outbound_queue is not None:
            receipt = await self.outbound_queue.enqueue_message(
                channel_id=str(target.id),
                content=text,
                source_type=source_type,
                source_id=source_id,
                dedupe_key=dedupe_key,
            )
            if not receipt.delivered:
                raise DeliveryFailed(receipt)
            return receipt

        return await _send_chat_chunks(
            target,
            text,
            db=self.db,
            memory_service=self.memory,
            role="assistant",
            source_type=source_type,
            source_id=source_id,
            use_typing=not self._is_typing_cooling_down(target.id),
            on_typing_rate_limited=lambda: self._mark_typing_cooldown(
                target.id, "Discord typing 发送限流"
            ),
        )

    async def _send_or_enqueue_reaction(self, message, reaction: str, *,
                                        source_type: str, source_id: str,
                                        dedupe_key: str):
        if self.outbound_queue is not None:
            receipt = await self.outbound_queue.enqueue_reaction(
                channel_id=str(message.channel.id),
                reaction=reaction,
                target_discord_message_id=str(message.id),
                source_type=source_type,
                source_id=source_id,
                dedupe_key=dedupe_key,
            )
            if not receipt.delivered:
                raise DeliveryFailed(receipt)
            return receipt
        await message.add_reaction(reaction)
        return None

    async def _resolve_channel(self, channel_id: str):
        channel = self.get_channel(int(channel_id))
        if channel is None:
            channel = await self.fetch_channel(int(channel_id))
        if not hasattr(channel, "send"):
            raise TypeError(
                f"频道类型不支持发送: {type(channel).__name__} ({channel_id})")
        return channel

    async def deliver_outbound(self, delivery: dict) -> list[str]:
        """Low-level transport used only by the OutboundQueue consumer."""
        await self.wait_until_ready()
        channel = await self._resolve_channel(str(delivery["channel_id"]))
        if delivery["kind"] == "message":
            return await _send_chat_chunks(
                channel,
                delivery["content"],
                db=self.db,
                memory_service=self.memory,
                role="assistant",
                source_type=delivery.get("source_type"),
                source_id=delivery.get("source_id"),
                use_typing=not self._is_typing_cooling_down(channel.id),
                on_typing_rate_limited=lambda: self._mark_typing_cooldown(
                    channel.id, "Discord typing OutboundQueue 限流"
                ),
            )
        if delivery["kind"] == "reaction":
            target_id = int(delivery["target_discord_message_id"])
            if hasattr(channel, "get_partial_message"):
                target = channel.get_partial_message(target_id)
            else:
                target = await channel.fetch_message(target_id)
            await target.add_reaction(delivery["reaction"])
            return []
        raise ValueError(f"未知 outbound kind: {delivery['kind']}")

    async def _record_sent_message(self, sent: discord.Message,
                                   role: str = "assistant") -> None:
        """Record a Discord message sent by Hiyori into the raw conversation log."""
        try:
            await self.memory.ingest_message(
                discord_message_id=str(sent.id),
                channel_id=str(sent.channel.id),
                guild_id=str(sent.guild.id) if sent.guild else None,
                author_id=str(sent.author.id) if sent.author else None,
                author_name=str(sent.author) if sent.author else None,
                role=role,
                content=sent.content or "",
                created_at=sent.created_at.isoformat(),
                reply_to_message_id=(
                    str(sent.reference.message_id)
                    if sent.reference and sent.reference.message_id
                    else None
                ),
                metadata={"message_type": str(sent.type)},
            )
        except Exception as e:
            logger.warning(f"⚠️ 写入 outbound conversation log 失败: {e}")

    async def _maybe_finish_calendar_auth(self, message: discord.Message) -> bool:
        """Consume a pasted Google OAuth localhost callback URL before it reaches AI."""
        if not self.calendar_auth_session:
            return False
        # Only the authorized user can complete the session we started.
        if config.ALLOWED_USER_ID and message.author.id != config.ALLOWED_USER_ID:
            return False
        match = _LOCALHOST_CALLBACK_RE.search(message.content or "")
        if not match:
            return False

        # The pasted URL carries the OAuth code; delete it before doing anything
        # else so the secret never lingers in the channel — no matter the outcome.
        try:
            await message.delete()
        except Exception:
            pass

        session = self.calendar_auth_session
        self.calendar_auth_session = None
        if datetime.now(timezone.utc) - session.created_at > timedelta(minutes=15):
            await message.channel.send("⚠️ Calendar 授权会话已过期，请重新运行 `/calendar auth`。")
            return True

        try:
            from bot.google_calendar import finish_oauth_flow, refresh_calendar_context
            token_file = finish_oauth_flow(session.flow, match.group(0))
        except Exception as e:
            logger.warning(f"⚠️ Google Calendar 授权失败: {e}")
            await message.channel.send(f"⚠️ Calendar 授权失败：{type(e).__name__}: {e}")
            return True

        try:
            refresh = refresh_calendar_context()
            refresh_line = f"\nCalendar 缓存已刷新：{refresh.get('count', 0)} events"
        except Exception as e:
            logger.warning(f"⚠️ Google Calendar 授权后刷新缓存失败: {e}")
            refresh_line = "\n缓存刷新失败；请稍后运行 `/calendar refresh`。"

        await message.channel.send(
            f"✅ Google Calendar 已授权，token 已写入 `{token_file}`{refresh_line}"
        )
        return True


def _calendar_group(bot: LifeTrackerBot) -> app_commands.Group:
    """创建 /calendar 命令组"""
    group = app_commands.Group(name="calendar", description="Google Calendar 授权与状态")

    @group.command(name="auth", description="生成 Google Calendar 授权链接")
    async def calendar_auth(interaction: discord.Interaction):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        try:
            from bot.google_calendar import begin_oauth_flow, begin_web_oauth_flow
            if config.GCAL_OAUTH_REDIRECT_URI:
                auth_url, _state = begin_web_oauth_flow()
                bot.calendar_auth_session = None
                message = (
                    "打开下面的链接完成 Google Calendar 授权。授权成功后页面会显示完成提示；"
                    "不需要再复制 localhost URL。\n\n"
                    f"{auth_url}"
                )
            else:
                flow, auth_url = begin_oauth_flow()
                bot.calendar_auth_session = _CalendarAuthSession(
                    flow=flow,
                    created_at=datetime.now(timezone.utc),
                )
                message = (
                    "打开下面的链接完成 Google Calendar 授权。授权后浏览器会跳到 "
                    "`http://localhost:58679/?...code=...`；把地址栏里的完整 URL 发回这个频道，我会自动换 token。\n\n"
                    f"{auth_url}"
                )
        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ 无法开始 Calendar 授权：{type(e).__name__}: {e}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(message, ephemeral=True)

    @group.command(name="status", description="查看 Google Calendar 授权状态")
    async def calendar_status(interaction: discord.Interaction):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        from bot.google_calendar import get_calendar_cache_status, is_authorized
        status = "已授权" if is_authorized() else "未授权"
        enabled = "enabled" if config.GCAL_ENABLED else "disabled"
        cache = get_calendar_cache_status()
        cache_line = (
            f"cache: `{cache['count']} events @ {cache['refreshed_at']}`"
            if cache.get("cached")
            else "cache: `empty`"
        )
        await interaction.response.send_message(
            f"Google Calendar: `{enabled}` / `{status}`\n"
            f"token: `{config.GCAL_TOKEN_FILE}`\n"
            f"{cache_line}",
            ephemeral=True,
        )

    @group.command(name="refresh", description="手动刷新今天起未来 7 天的日历缓存")
    async def calendar_refresh(interaction: discord.Interaction):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            from bot.google_calendar import refresh_calendar_context
            result = refresh_calendar_context()
        except Exception as e:
            await interaction.followup.send(f"⚠️ Calendar 刷新失败：{type(e).__name__}: {e}")
            return
        await interaction.followup.send(
            f"✅ Calendar 缓存已刷新：{result.get('count', 0)} events\n"
            f"refreshed_at: `{result.get('refreshed_at')}`"
        )

    @group.command(name="list", description="列出可读取的 Google calendars")
    async def calendar_list(interaction: discord.Interaction):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            from bot.google_calendar import list_calendars
            calendars = list_calendars()
        except Exception as e:
            await interaction.followup.send(f"⚠️ Calendar 列表读取失败：{type(e).__name__}: {e}")
            return
        if not calendars:
            await interaction.followup.send("没有读到任何 calendar。")
            return
        lines = []
        for cal in calendars:
            marker = "🚫" if cal.get("disabled") else "✅"
            primary = " primary" if cal.get("primary") else ""
            selected = "" if cal.get("selected", True) else " hidden-in-google-ui"
            lines.append(
                f"{marker} `{cal['id']}` — {cal.get('summary', cal['id'])}{primary}{selected}"
            )
        text = "Google Calendars\n" + "\n".join(lines)
        await interaction.followup.send(text[:2000])

    @group.command(name="disable", description="隐藏一个 calendar id，不再注入/查询")
    @app_commands.describe(calendar_id="从 /calendar list 复制 calendar id")
    async def calendar_disable(interaction: discord.Interaction, calendar_id: str):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        try:
            from bot.google_calendar import disable_calendar
            disable_calendar(calendar_id)
        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ 禁用失败：{type(e).__name__}: {e}",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"🚫 已隐藏 calendar `{calendar_id}`",
            ephemeral=True,
        )

    @group.command(name="enable", description="重新显示一个被隐藏的 calendar id")
    @app_commands.describe(calendar_id="从 /calendar list 复制 calendar id")
    async def calendar_enable(interaction: discord.Interaction, calendar_id: str):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        try:
            from bot.google_calendar import enable_calendar
            enable_calendar(calendar_id)
        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ 启用失败：{type(e).__name__}: {e}",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"✅ 已重新显示 calendar `{calendar_id}`",
            ephemeral=True,
        )

    return group


def _card_choice_label(card: dict) -> str:
    """autocomplete 选项的文字。Discord 限制 100 字符，超出会整条被拒。"""
    marker = "▶ " if card.get("is_current") else ""
    summary = (card.get("summary") or "还没有总结").strip()
    text = f"{marker}{card['label']} · {summary}"
    return text[:100]


def _conversation_group(bot: LifeTrackerBot) -> app_commands.Group:
    """/conversation — 手动管理消息卡片（开新卡片、看列表、切换）。"""
    group = app_commands.Group(name="conversation", description="管理对话卡片")

    async def card_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # 这里不能调模型：autocomplete 必须 3 秒内返回，总结是后台预先写好的。
        try:
            cards = bot.cards.list_active(str(interaction.channel_id))
        except Exception as e:
            logger.warning(f"⚠️ 卡片 autocomplete 读取失败: {type(e).__name__}: {e}")
            return []
        keyword = current.lower()
        if keyword:
            cards = [
                c for c in cards
                if keyword in c["label"].lower()
                or keyword in (c.get("summary") or "").lower()
            ]
        return [
            app_commands.Choice(name=_card_choice_label(c), value=str(c["id"]))
            for c in cards[:25]
        ]

    @group.command(name="new", description="开一张新的对话卡片，之后的消息都归到它下面")
    async def conversation_new(interaction: discord.Interaction):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        try:
            card_id = bot.cards.start_new_card(str(interaction.channel_id))
        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ 开新卡片失败：{type(e).__name__}: {e}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ 已经开了一张新卡片（#{card_id}），接下来的消息都记在它下面。",
            ephemeral=True,
        )

    @group.command(name="list", description="看看现在有哪些活跃的对话卡片")
    async def conversation_list(interaction: discord.Interaction):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        try:
            cards = bot.cards.list_active(str(interaction.channel_id))
        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ 卡片列表读取失败：{type(e).__name__}: {e}", ephemeral=True)
            return
        if not cards:
            await interaction.response.send_message(
                "现在没有活跃的卡片。直接说话或者用 `/conversation new` 开一张。",
                ephemeral=True,
            )
            return
        lines = []
        for card in cards:
            marker = "▶" if card["is_current"] else "　"
            summary = (card.get("summary") or "还没有总结").strip()
            lines.append(f"{marker} `{card['label']}` (#{card['id']}) — {summary}")
        text = "活跃的对话卡片（▶ 是当前卡片）\n" + "\n".join(lines)
        await interaction.response.send_message(text[:2000], ephemeral=True)

    @group.command(name="switch", description="切换到另一张对话卡片")
    @app_commands.describe(card="从列表里选一张卡片")
    @app_commands.autocomplete(card=card_autocomplete)
    async def conversation_switch(interaction: discord.Interaction, card: str):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        try:
            switched = bot.cards.switch_to(int(card))
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        except Exception as e:
            await interaction.response.send_message(
                f"⚠️ 切换失败：{type(e).__name__}: {e}", ephemeral=True)
            return
        summary = (switched.get("summary") or "还没有总结").strip()
        await interaction.response.send_message(
            f"✅ 已经切换到卡片 #{switched['id']} — {summary}", ephemeral=True)

    return group


_COMMON_TZS = [
    "Australia/Sydney",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Singapore",
    "Asia/Bangkok",
    "Asia/Seoul",
    "Asia/Taipei",
    "Asia/Kuala_Lumpur",
    "Asia/Dubai",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "America/Los_Angeles",
    "America/New_York",
    "America/Chicago",
    "Pacific/Auckland",
    "UTC",
]


async def _tz_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    filtered = [n for n in _COMMON_TZS if current.lower() in n.lower()] or _COMMON_TZS
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


def _tz_command(bot: LifeTrackerBot) -> app_commands.Command:
    """/tz [name] — 无参显示当前 TZ；带 name 切换并持久化（用于 travel）。"""

    @app_commands.command(name="tz", description="查看或切换进程时区（用于 travel）")
    @app_commands.describe(name="IANA 时区名（如 Asia/Tokyo），留空显示当前")
    @app_commands.autocomplete(name=_tz_autocomplete)
    async def tz(interaction: discord.Interaction, name: str | None = None):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        from bot import timezone_state
        if name is None:
            current = timezone_state.get_timezone()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await interaction.response.send_message(
                f"🕐 当前时区: `{current}`\n本地时间: {now_str}"
            )
            return
        try:
            timezone_state.set_timezone(name)
        except ValueError:
            await interaction.response.send_message(f"⚠️ 未知时区: `{name}`")
            return
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await interaction.response.send_message(
            f"✅ 已切换时区 → `{name}`\n本地时间: {now_str}"
        )

    return tz


def _weather_command(bot: LifeTrackerBot) -> app_commands.Command:
    """创建 /weather 命令"""
    @app_commands.command(name="weather", description="查看今日天气和穿衣建议；可选传入地址")
    @app_commands.describe(address="要查询的地址（留空=默认地点）")
    async def weather(interaction: discord.Interaction, address: str | None = None):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return

        await interaction.response.defer()

        location: str | None = None
        location_label: str | None = None
        geocode_header: str | None = None
        if address:
            geo = await geocode_address(address)
            if not geo:
                await interaction.followup.send(
                    f"没找到这个地址：`{address}`，换个写法再试试"
                )
                return
            location, location_label = geo
            geocode_header = (
                f"📍 `{address}` →\n"
                f"   {location_label}\n"
                f"   ({location})"
            )

        weather_data = await get_weather_detailed(
            location=location,
            location_label=location_label,
        )
        if not weather_data:
            await interaction.followup.send("天气查询失败了，等会再试试吧")
            return

        prompt = get_prompt_template(
            "weather_report",
            bot.db.get_prompt_sections(),
        ).format(weather_data=weather_data)
        reply = await simple_completion(prompt)
        if geocode_header:
            reply = f"{geocode_header}\n\n{reply}"
        await interaction.followup.send(reply)

    return weather


_RE_TS_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?\]\s*")


async def _send_chat_chunks(target, text: str, *,
                            db: Database | None = None,
                            memory_service: MemoryService | None = None,
                            role: str = "assistant",
                            source_type: str | None = None,
                            source_id: str | None = None,
                            use_typing: bool = True,
                            on_typing_rate_limited=None) -> list[str]:
    """
    一次性发送整段 AI 回复，仅在超过 Discord 2000 字符上限时按长度兜底切分。
    发送期间保留 typing indicator。
    target: 任何支持 .send() 和 .typing() 的 Discord 通道对象
    """
    if "[SILENT]" in text:
        return []
    text = _RE_TS_PREFIX.sub("", text).strip()
    if not text:
        return []
    limit = 2000
    chunks = [text[i:i + limit] for i in range(0, len(text), limit)]
    # 同一次发送的所有分段必须落进同一张卡片，所以缺省的 source_id 在这里统一替换
    if is_proactive(source_type):
        source_id = unique_source_id(source_id)
    card_source = {"source_type": source_type, "source_id": source_id}
    sent_ids: list[str] = []
    logger.info(f"📤 准备发送 {len(chunks)} 段消息到 {type(target).__name__}（{len(text)} 字符）")
    if use_typing:
        typing_cm = target.typing()
        try:
            await typing_cm.__aenter__()
        except (discord.HTTPException, discord.RateLimited) as e:
            if not _is_rate_limited_error(e):
                raise
            if on_typing_rate_limited:
                on_typing_rate_limited()
        else:
            try:
                for chunk in chunks:
                    sent = await target.send(chunk)
                    await _record_sent_chunk(db, sent, role, memory_service, **card_source)
                    sent_ids.append(str(sent.id))
            finally:
                await typing_cm.__aexit__(None, None, None)
            logger.info("✅ 消息发送完成")
            return sent_ids

    for chunk in chunks:
        sent = await target.send(chunk)
        await _record_sent_chunk(db, sent, role, memory_service, **card_source)
        sent_ids.append(str(sent.id))
    logger.info("✅ 消息发送完成")
    return sent_ids


async def _record_sent_chunk(db: Database | None, sent: discord.Message, role: str,
                             memory_service: MemoryService | None = None, *,
                             source_type: str | None = None,
                             source_id: str | None = None) -> None:
    """Persist a sent Discord message when a DB is available.

    带 source_type 时交给卡片规则决定去向：主动消息先进 pending 卡片，
    回复类消息进当前卡片。不带时保持原行为，直接写进对话历史。
    """
    if db is None and memory_service is None:
        return
    try:
        service = memory_service or MemoryService(db)
        message = dict(
            discord_message_id=str(sent.id),
            channel_id=str(sent.channel.id),
            guild_id=str(sent.guild.id) if sent.guild else None,
            author_id=str(sent.author.id) if sent.author else None,
            author_name=str(sent.author) if sent.author else None,
            role=role,
            content=sent.content or "",
            created_at=sent.created_at.isoformat(),
            reply_to_message_id=(
                str(sent.reference.message_id)
                if sent.reference and sent.reference.message_id
                else None
            ),
            metadata={"message_type": str(sent.type)},
        )
        cards_db = db if db is not None else getattr(service, "repository", None)
        if source_type is not None and hasattr(cards_db, "create_card"):
            await CardService(cards_db).record_outbound(
                message, source_type=source_type, source_id=source_id,
                ingest=service.ingest_message,
            )
        else:
            await service.ingest_message(**message)
    except Exception as e:
        logger.warning(f"⚠️ 写入 outbound conversation log 失败: {e}")
