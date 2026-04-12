"""
Discord 机器人模块
负责接收和发送 Discord 消息，注册斜杠命令
"""
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from bot.ai_engine import chat, simple_completion
from bot.weather import get_weather_brief
from bot.prompts import WEATHER_REPORT_PROMPT
from bot.database import Database
from bot.tools import SET_TOOL_NAMES
from bot.logger import get_logger
import config

logger = get_logger(__name__)


class LifeTrackerBot(commands.Bot):
    def __init__(self, db: Database):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = db
        self.target_channel_id: int | None = None

    async def setup_hook(self):
        """注册斜杠命令并同步到 Discord"""
        self.tree.add_command(_todo_group(self))
        self.tree.add_command(_weather_command(self))
        await self.tree.sync()
        logger.info("✅ 斜杠命令已同步")

    async def on_ready(self):
        logger.info(f"✅ Discord Bot 已上线: {self.user}")
        # 从 DB 恢复上次的目标频道，让 scheduler 在冷启动也能主动发消息
        saved = self.db.get_state("target_channel_id")
        if saved:
            try:
                self.target_channel_id = int(saved)
                logger.info(f"📍 恢复目标频道: {saved}")
            except ValueError:
                logger.warning(f"⚠️ DB 里的 target_channel_id 不是数字: {saved!r}")

    async def on_message(self, message: discord.Message):
        # 忽略自己的消息
        if message.author == self.user:
            return

        # 只响应指定用户
        if config.ALLOWED_USER_ID and message.author.id != config.ALLOWED_USER_ID:
            return

        # 斜杠命令走 interaction，普通消息才到这里
        # 跳过斜杠命令的文本消息（防止重复处理）
        if message.content.startswith("/"):
            return

        logger.info(f"📨 收到消息: {message.author} ({message.author.id}): {message.content}")

        # 记住频道 ID，用于主动发消息 + 定时调度拉历史
        # 切换到新频道时持久化到 DB，重启后 on_ready 能恢复
        if self.target_channel_id != message.channel.id:
            self.target_channel_id = message.channel.id
            self.db.set_state("target_channel_id", str(message.channel.id))

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
                await _send_chat_chunks(message.channel, text)

            tool_called_flag = False
            async def on_tool_call(tool_names: list[str]):
                nonlocal tool_called_flag
                if not tool_called_flag and any(n in SET_TOOL_NAMES for n in tool_names):
                    tool_called_flag = True
                    try:
                        await message.add_reaction("✅")
                    except Exception as react_err:
                        logger.warning(f"⚠️ 无法添加反馈 emoji: {react_err}")

            async with message.channel.typing():
                await chat(self.db, ai_messages, send_callback=send_reply, tool_callback=on_tool_call)
        except Exception as e:
            error_msg = f"❌ {type(e).__name__}: {e}"
            logger.exception(error_msg)
            await message.channel.send(error_msg[:2000])

    async def send_proactive_message(self, text: str):
        """主动发送消息（由定时器触发）"""
        if not text or not text.strip():
            return
        if not self.target_channel_id:
            return
        channel = self.get_channel(self.target_channel_id)
        if channel and isinstance(channel, (discord.TextChannel, discord.DMChannel)):
            await _send_chat_chunks(channel, text)

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
                ts = m.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
                content = f"[{ts}] {m.content}" if m.content else f"[{ts}] "
                
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
        """
        给 Scheduler 的历史拉取入口：用记忆中的 target_channel_id 定位频道。
        Bot 启动后没人聊过任何消息的话，target_channel_id 还没设置，返回空历史。
        """
        if not self.target_channel_id:
            return []
        channel = self.get_channel(self.target_channel_id)
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


def _weather_command(bot: LifeTrackerBot) -> app_commands.Command:
    """创建 /weather 命令"""
    @app_commands.command(name="weather", description="查看今日天气和穿衣建议")
    async def weather(interaction: discord.Interaction):
        if config.ALLOWED_USER_ID and interaction.user.id != config.ALLOWED_USER_ID:
            return

        await interaction.response.defer()

        weather_data = await get_weather_brief()
        if not weather_data:
            await interaction.followup.send("天气查询失败了，等会再试试吧")
            return

        prompt = WEATHER_REPORT_PROMPT.format(weather_data=weather_data)
        reply = await simple_completion(prompt)
        await interaction.followup.send(reply)

    return weather


def _split_for_chat(text: str, limit: int = 2000) -> list[str]:
    """
    把一段 AI 回复拆成多条 Discord 消息，模拟真实聊天节奏：
    1. 先按换行符 \\n 切分，每一行当作一条独立消息
    2. 去掉空行和首尾空白
    3. 超长行再按 Discord 2000 字符上限兜底切分
    """
    chunks = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        chunks.append(line)
    return chunks


async def _send_chat_chunks(target, text: str) -> None:
    """
    按 _split_for_chat 拆分后依次发送，并在消息之间用 typing 指示器 + 小延迟
    模拟打字节奏。第一条立刻发送，后续消息根据长度计算思考/打字时间。
    target: 任何支持 .send() 和 .typing() 的 Discord 通道对象
    """
    chunks = _split_for_chat(text)
    for i, chunk in enumerate(chunks):
        if i > 0:
            # 粗略模拟：基础 0.4s 思考 + 每字符 0.03s 打字，上限 2s
            delay = min(0.4 + len(chunk) * 0.03, 2.0)
            try:
                async with target.typing():
                    await asyncio.sleep(delay)
            except Exception:
                # typing() 某些通道类型可能不支持，退化为纯 sleep
                await asyncio.sleep(delay)
        await target.send(chunk)
