"""
Discord 机器人模块
负责接收和发送 Discord 消息，注册斜杠命令
"""
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from bot.ai_engine import chat, simple_completion
from bot.weather import get_weather_brief, WEATHER_REPORT_PROMPT
from bot.database import Database
import config


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
        print("✅ 斜杠命令已同步")

    async def on_ready(self):
        print(f"✅ Discord Bot 已上线: {self.user}")

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

        print(f"📨 收到消息: {message.author} ({message.author.id}): {message.content}")

        # 记住频道 ID，用于主动发消息
        self.target_channel_id = message.channel.id

        # 获取当前时间戳
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 处理回复（引用）消息
        content_to_send = message.content
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg and ref_msg.content:
                    quote = ref_msg.content if len(ref_msg.content) < 200 else ref_msg.content[:200] + "..."
                    author_name = "你说过" if ref_msg.author == self.user else "我曾发过"
                    content_to_send = f'[回复 {author_name} 的消息: "{quote}"]\n{message.content}'
            except Exception as e:
                print(f"⚠️ 无法获取引用的消息: {e}")

        try:
            async def send_reply(text):
                for chunk in _split_message(text):
                    await message.channel.send(chunk)

            await chat(self.db, content_to_send, timestamp, send_callback=send_reply)
        except Exception as e:
            error_msg = f"❌ {type(e).__name__}: {e}"
            print(error_msg)
            await message.channel.send(error_msg[:2000])

    async def send_proactive_message(self, text: str):
        """主动发送消息（由定时器触发）"""
        if not text or not text.strip():
            return
        if not self.target_channel_id:
            return
        channel = self.get_channel(self.target_channel_id)
        if channel and isinstance(channel, (discord.TextChannel, discord.DMChannel)):
            for chunk in _split_message(text):
                await channel.send(chunk)


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


def _split_message(text: str, limit: int = 2000) -> list[str]:
    """将长消息拆分为 Discord 允许的长度"""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks
