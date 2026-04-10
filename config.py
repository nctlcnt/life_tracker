import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
API_PORT = int(os.getenv("API_PORT", "8000"))

AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.anthropic.com")

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "life_tracker.db")

# 随机轮询间隔范围（秒）
POLL_MIN_SECONDS = 60      # 最短 1 分钟
POLL_MAX_SECONDS = 3600    # 最长 60 分钟
