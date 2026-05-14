"""
Discord 机器人模块
负责接收和发送 Discord 消息，注册斜杠命令
"""
import asyncio
import re
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from bot.ai_engine import chat, simple_completion
from bot.weather import get_weather_brief, get_weather_detailed, geocode_address
from bot.prompts import get_prompt_template
from bot.database import Database
from bot.tools import SET_TOOL_NAMES
from bot.logger import get_logger
import config

logger = get_logger(__name__)
_TYPING_COOLDOWN = timedelta(minutes=5)


def _is_rate_limited_error(exc: Exception) -> bool:
    return isinstance(exc, discord.RateLimited) or (
        isinstance(exc, discord.HTTPException) and exc.status == 429
    )


class LifeTrackerBot(commands.Bot):
    def __init__(self, db: Database):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = db
        self.last_typing_at: datetime | None = None  # 目标用户最近 typing 时刻（UTC aware）
        # typing 被 429 限流后的冷却截止时间（按 channel_id），命中时直接跳过 typing 不再撞 API
        self._typing_cooldown_until: dict[int, datetime] = {}
        # 由 main.py 注入：chat 完成时调用，用于重置 scheduler 的 poll 基准时间
        self.on_ai_call_done = None

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
        self.tree.add_command(_todo_group(self))
        self.tree.add_command(_weather_command(self))
        self.tree.add_command(_model_command(self))
        self.tree.add_command(_fallback_command(self))
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
            return

        # 只响应配置里指定的 channel（prod / staging 各自配置不同 id，多 bot 共存不会串台）
        if message.channel.id != config.CHANNEL_ID:
            return

        # 斜杠命令走 interaction，普通消息才到这里
        # 跳过斜杠命令的文本消息（防止重复处理）
        if message.content.startswith("/"):
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

        # 备份到 DB（messages 表只作备份，AI 上下文走 Discord 历史）
        self.db.add_message("user", current_content)

        # 从 Discord 拉历史（排除当前这条，等下单独 append 富化版本）
        history = await self._fetch_history_as_messages(
            message.channel, limit=20, exclude_id=message.id
        )
        ai_messages = history + [{"role": "user", "content": current_content}]

        try:
            async def send_reply(text):
                await _send_chat_chunks(
                    message.channel,
                    text,
                    use_typing=not self._is_typing_cooling_down(message.channel.id),
                    on_typing_rate_limited=lambda: self._mark_typing_cooldown(
                        message.channel.id, "Discord typing 发送限流"
                    ),
                )

            tool_called_flag = False
            async def on_tool_call(tool_names: list[str]):
                nonlocal tool_called_flag
                if not tool_called_flag and any(n in SET_TOOL_NAMES for n in tool_names):
                    tool_called_flag = True
                    try:
                        await message.add_reaction("✅")
                    except Exception as react_err:
                        logger.warning(f"⚠️ 无法添加反馈 emoji: {react_err}")

            if self._is_typing_cooling_down(message.channel.id):
                # typing 限流冷却期，跳过 typing 直接 chat
                await chat(self.db, ai_messages, send_callback=send_reply, tool_callback=on_tool_call)
            else:
                typing_cm = message.channel.typing()
                try:
                    await typing_cm.__aenter__()
                except (discord.HTTPException, discord.RateLimited) as e:
                    if not _is_rate_limited_error(e):
                        raise
                    self._mark_typing_cooldown(message.channel.id, f"Discord typing 入口限流: {e}")
                    await chat(self.db, ai_messages, send_callback=send_reply, tool_callback=on_tool_call)
                else:
                    try:
                        await chat(self.db, ai_messages, send_callback=send_reply, tool_callback=on_tool_call)
                    finally:
                        await typing_cm.__aexit__(None, None, None)
        except Exception as e:
            error_msg = f"❌ {type(e).__name__}: {e}"
            logger.exception(error_msg)
            await message.channel.send(error_msg[:2000])
        finally:
            # cache 钱已付，通知 scheduler 重置 poll 基准（45-55min 内不再轮询）
            if self.on_ai_call_done:
                self.on_ai_call_done()

    async def send_proactive_message(self, text: str):
        """主动发送消息（由定时器触发）"""
        if not text or not text.strip():
            return
        channel = self.get_channel(config.CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(config.CHANNEL_ID)
            except Exception as e:
                logger.warning(f"⚠️ 主动发送失败：无法取回频道 {config.CHANNEL_ID}: {e}")
                return

        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                f"⚠️ 主动发送失败：频道类型不支持: {type(channel).__name__} ({config.CHANNEL_ID})"
            )
            return

        logger.info(f"📨 主动发送到频道 {channel.id}")
        try:
            await _send_chat_chunks(
                channel,
                text,
                use_typing=not self._is_typing_cooling_down(channel.id),
                on_typing_rate_limited=lambda: self._mark_typing_cooldown(
                    channel.id, "Discord typing 主动发送限流"
                ),
            )
        except Exception as e:
            logger.exception(f"❌ 主动发送失败: {e}")

    async def _fetch_history_as_messages(
        self, channel, limit: int = 20, exclude_id: int | None = None
    ) -> list[dict]:
        """
        从 Discord 频道拉历史消息，转换为 AI 引擎期望的 [{role, content}] 格式。
        - 时间顺序：从旧到新（Discord API 默认是新→旧，这里反转）
        - 角色映射：bot 自己 → assistant；允许的用户 → user；其他忽略
        - 消息类型：只保留 default 和 reply，过滤 pin/join 等系统条目
        - 每条消息前缀 [timestamp]，格式和 on_message 里当前消息保持一致
        - 斜杠命令的响应（/todo、/weather）也会保留在历史里作为 assistant 角色
          —— 这是故意的，让 AI 看到用户刚刚查询的上下文；prompt 里已明确告知如何识别
        """
        try:
            fetch_limit = limit + (1 if exclude_id else 0)
            raw_messages = []
            async for m in channel.history(limit=fetch_limit):
                if exclude_id and m.id == exclude_id:
                    continue
                if m.type not in (discord.MessageType.default, discord.MessageType.reply):
                    continue

                # 角色映射
                if self.user and m.author.id == self.user.id:
                    role = "assistant"
                elif config.ALLOWED_USER_ID and m.author.id == config.ALLOWED_USER_ID:
                    role = "user"
                else:
                    continue  # 其他用户忽略（单用户限制）

                # 时间戳前缀（转本地时区）
                # 只给 user 消息加，assistant 消息不加——避免 AI 模仿时间戳格式回复。
                # AI 可以通过 user 消息的时间戳推算时间间隔，无需在 assistant 侧重复。
                ts = m.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
                if role == "user":
                    content = f"[{ts}] {m.content}" if m.content else f"[{ts}] "
                else:
                    content = m.content or ""
                
                # 如果消息上有 ✅ 标记（代表曾被工具处理过），给 AI 增加一个已执行提示
                for r in m.reactions:
                    if str(r.emoji) == "✅":
                        content += " [已执行✅]"
                        break

                raw_messages.append({"role": role, "content": content})

            # Discord 返回的是新→旧，反转成时间顺序
            raw_messages.reverse()
            return raw_messages[-limit:]  # 保证不超过 limit
        except Exception as e:
            logger.warning(f"⚠️ 拉 Discord 历史失败，返回空历史: {e}")
            return []

    async def fetch_history_for_scheduler(self, limit: int = 20) -> list[dict]:
        """给 Scheduler 的历史拉取入口：从配置里指定的 channel 取历史。"""
        channel = self.get_channel(config.CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(config.CHANNEL_ID)
            except Exception as e:
                logger.warning(f"⚠️ 拉历史失败：无法取回频道 {config.CHANNEL_ID}: {e}")
                return []
        if not channel:
            return []
        return await self._fetch_history_as_messages(channel, limit=limit)


def _todo_group(bot: LifeTrackerBot) -> app_commands.Group:
    """创建 /todo 命令组"""
    group = app_commands.Group(name="todo", description="待办事项管理")

    @group.command(name="add", description="添加一条待办")
    @app_commands.describe(content="待办内容")
    async def todo_add(interaction: discord.Interaction, content: str):
        todo_id = bot.db.add_todo(content)
        await interaction.response.send_message(f"📝 已添加 #{todo_id}：{content}")

    @group.command(name="list", description="查看未完成的待办")
    async def todo_list(interaction: discord.Interaction):
        todos = bot.db.get_todos()
        if not todos:
            await interaction.response.send_message("📋 待办清空了！")
            return
        lines = [f"{'✅' if t['done'] else '⬜'} `{t['id']}` {t['content']}" for t in todos]
        await interaction.response.send_message("📋 **待办列表**\n" + "\n".join(lines))

    @group.command(name="all", description="查看全部待办（含已完成）")
    async def todo_all(interaction: discord.Interaction):
        todos = bot.db.get_todos(include_done=True)
        if not todos:
            await interaction.response.send_message("📋 没有任何待办")
            return
        lines = [f"{'✅' if t['done'] else '⬜'} `{t['id']}` {t['content']}" for t in todos]
        await interaction.response.send_message("📋 **全部待办**\n" + "\n".join(lines))

    @group.command(name="done", description="完成一条待办")
    @app_commands.describe(id="待办 ID")
    async def todo_done(interaction: discord.Interaction, id: int):
        if bot.db.complete_todo(id):
            await interaction.response.send_message(f"✅ 已完成 #{id}")
        else:
            await interaction.response.send_message(f"⚠️ 找不到未完成的 #{id}")

    @group.command(name="del", description="删除一条待办")
    @app_commands.describe(id="待办 ID")
    async def todo_del(interaction: discord.Interaction, id: int):
        if bot.db.delete_todo(id):
            await interaction.response.send_message(f"🗑️ 已删除 #{id}")
        else:
            await interaction.response.send_message(f"⚠️ 找不到 #{id}")

    return group


async def _preset_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    names = config.list_presets()
    filtered = [n for n in names if current.lower() in n.lower()] or names
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


def _format_preset_status() -> str:
    active = config.get_active()
    fb = config.get_fallback()
    lines = ["🧠 **AI Preset 当前状态**"]
    lines.append(f"  主: `{active.name}` — {active.provider} / {active.model}")
    if fb:
        lines.append(f"  备: `{fb.name}` — {fb.provider} / {fb.model}")
    else:
        lines.append("  备: (未配置)")
    lines.append("")
    lines.append("📋 **可用 presets**")
    for name in config.list_presets():
        p = config.PRESETS[name]
        tag = ""
        if name == active.name:
            tag = " ← 主"
        elif fb and name == fb.name:
            tag = " ← 备"
        lines.append(f"  • `{name}` ({p.provider} / {p.model}){tag}")
    return "\n".join(lines)


def _model_command(bot: LifeTrackerBot) -> app_commands.Command:
    """/model [name] — 无参列出状态；带 name 切换主 preset。"""

    @app_commands.command(name="model", description="查看或切换主 preset（不传参数即列出所有可用）")
    @app_commands.describe(name="preset 名称（留空则列出当前状态和所有可用）")
    @app_commands.autocomplete(name=_preset_autocomplete)
    async def model(interaction: discord.Interaction, name: str | None = None):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        if name is None:
            await interaction.response.send_message(_format_preset_status())
            return
        try:
            config.set_active(name)
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ 未知 preset：`{name}`\n可用: {', '.join(config.list_presets())}"
            )
            return
        p = config.get_active()
        logger.info(f"🔀 切换主 preset → {p.name} ({p.provider}/{p.model})")
        await interaction.response.send_message(
            f"✅ 已切换主 preset → `{p.name}` ({p.provider} / {p.model})"
        )

    return model


def _fallback_command(bot: LifeTrackerBot) -> app_commands.Command:
    """/fallback <name|off> — 切换/关闭 fallback preset。"""

    async def _fallback_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        names = ["off"] + config.list_presets()
        filtered = [n for n in names if current.lower() in n.lower()] or names
        return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]

    @app_commands.command(name="fallback", description="切换 fallback preset；传 off 关闭")
    @app_commands.describe(name="preset 名称，或 'off' 关闭 fallback")
    @app_commands.autocomplete(name=_fallback_autocomplete)
    async def fallback(interaction: discord.Interaction, name: str):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return
        if name.lower() == "off":
            config.set_fallback(None)
            logger.info("🔀 已关闭 fallback preset")
            await interaction.response.send_message("✅ 已关闭 fallback")
            return
        try:
            config.set_fallback(name)
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ 未知 preset：`{name}`\n可用: {', '.join(config.list_presets())}（或 'off'）"
            )
            return
        p = config.get_fallback()
        logger.info(f"🔀 切换 fallback preset → {p.name} ({p.provider}/{p.model})")
        await interaction.response.send_message(
            f"✅ 已切换 fallback preset → `{p.name}` ({p.provider} / {p.model})"
        )

    return fallback


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
            bot.db.get_prompt_overrides(),
        ).format(weather_data=weather_data)
        reply = await simple_completion(prompt)
        if geocode_header:
            reply = f"{geocode_header}\n\n{reply}"
        await interaction.followup.send(reply)

    return weather


_RE_TS_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?\]\s*")


async def _send_chat_chunks(target, text: str, *,
                            use_typing: bool = True,
                            on_typing_rate_limited=None) -> None:
    """
    一次性发送整段 AI 回复，仅在超过 Discord 2000 字符上限时按长度兜底切分。
    发送期间保留 typing indicator。
    target: 任何支持 .send() 和 .typing() 的 Discord 通道对象
    """
    if "[SILENT]" in text:
        return
    text = _RE_TS_PREFIX.sub("", text).strip()
    if not text:
        return
    limit = 2000
    chunks = [text[i:i + limit] for i in range(0, len(text), limit)]
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
                    await target.send(chunk)
            finally:
                await typing_cm.__aexit__(None, None, None)
            logger.info("✅ 消息发送完成")
            return

    for chunk in chunks:
        await target.send(chunk)
    logger.info("✅ 消息发送完成")
