"""
主入口：同时启动 Discord Bot + 定时调度器 + FastAPI 服务
三个模块跑在同一个 asyncio 事件循环中
"""
import asyncio
import uvicorn
import config
from bot.logger import setup_logging, get_logger

# 日志必须最先初始化，确保后续 import 的模块拿到的 logger 已配置
setup_logging(config.LOG_LEVEL, config.LOG_FILE)
logger = get_logger(__name__)

from bot.discord_bot import LifeTrackerBot
from bot.scheduler import Scheduler
from bot.database import Database
from api.server import app as fastapi_app, set_database


async def main():
    # 1. 初始化数据库
    db = Database(config.DB_PATH)
    logger.info(f"📦 数据库已就绪: {config.DB_PATH}")

    # 2. 注入数据库到 FastAPI
    set_database(db)

    # 3. 初始化 Discord Bot
    bot = LifeTrackerBot(db)

    # 4. 初始化定时调度器
    scheduler = Scheduler(db, bot.send_proactive_message)
    db._on_reminder_added = scheduler.notify_new_reminder

    # 5. 启动 FastAPI（在后台线程中运行，不阻塞事件循环）
    api_config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=config.API_PORT,
        log_level="info"
    )
    api_server = uvicorn.Server(api_config)

    logger.info("🚀 正在启动所有服务...")
    logger.info("   - Discord Bot")
    logger.info(f"   - 定时调度器 (轮询间隔: {config.POLL_MIN_SECONDS}-{config.POLL_MAX_SECONDS}s)")
    logger.info(f"   - FastAPI 接口 (端口: {config.API_PORT})")

    # 三个任务并发运行
    await asyncio.gather(
        bot.start(config.DISCORD_TOKEN),   # Discord Bot
        scheduler.start(),                  # 定时调度器
        api_server.serve(),                 # FastAPI
    )


if __name__ == "__main__":
    asyncio.run(main())
