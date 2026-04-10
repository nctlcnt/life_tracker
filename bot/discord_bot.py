"""
Discord 机器人模块
负责接收和发送 Discord 消息
"""
import discord
from datetime import datetime
from bot.ai_engine import chat
from bot.database import Database
import config


class LifeTrackerBot(discord.Client):
    def __init__(self, db: Database):
        intents = discord.Intents.default()
        intents.message_content = True  # 需要在 Discord Developer Portal 开启
        super().__init__(intents=intents)
        self.db = db
        self.target_channel_id: int | None = None  # 用于主动发消息的频道

    async def on_ready(self):
        print(f"✅ Discord Bot 已上线: {self.user}")

    async def on_message(self, message: discord.Message):
        print(f"📨 收到消息: {message.author} ({message.author.id}): {message.content}")
        # 忽略自己的消息
        if message.author == self.user:
            print("⏭️ 跳过：是自己的消息")
            return

        # 只响应指定用户（你自己）
        if config.ALLOWED_USER_ID and message.author.id != config.ALLOWED_USER_ID:
            print(f"⏭️ 跳过：用户ID不匹配，期望 {config.ALLOWED_USER_ID}，实际 {message.author.id}")
            return

        print("✅ 通过过滤，开始调用 AI...")

        # 记住频道 ID，用于之后主动发消息
        self.target_channel_id = message.channel.id

        # 获取当前时间戳
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # 处理回复（引用）消息
        content_to_send = message.content
        if message.reference and message.reference.message_id:
            try:
                # 获取被引用的原消息
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg and ref_msg.content:
                    # 截断引用消息防止过长
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
            return  # 还没和用户对话过，不知道发到哪

        channel = self.get_channel(self.target_channel_id)
        if channel and isinstance(channel, (discord.TextChannel, discord.DMChannel)):
            for chunk in _split_message(text):
                await channel.send(chunk)


def _split_message(text: str, limit: int = 2000) -> list[str]:
    """将长消息拆分为 Discord 允许的长度"""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks



