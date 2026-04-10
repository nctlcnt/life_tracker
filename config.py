import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")

# AI 提供商：claude / relay / gemini
# claude = Anthropic 原生 API
# relay  = OpenAI 兼容的中转站（用 AI_BASE_URL）
# gemini = Google Gemini 原生 API
AI_PROVIDER = os.getenv("AI_PROVIDER", "claude")

# 聊天模型和轮询模型，根据 provider 填对应的模型名
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-4-6")
POLL_MODEL = os.getenv("POLL_MODEL", "claude-3-5-sonnet-latest")

# 中转站地址（仅 relay 模式需要）
AI_BASE_URL = os.getenv("AI_BASE_URL", "")

ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
API_PORT = int(os.getenv("API_PORT", "8765"))

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "life_tracker.db")

# 随机轮询间隔范围（秒）
POLL_MIN_SECONDS = 60      # 最短 1 分钟
POLL_MAX_SECONDS = 3600    # 最长 60 分钟
