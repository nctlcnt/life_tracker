"""
主入口：同时启动 Discord Bot + 定时调度器 + FastAPI 服务
三个模块跑在同一个 asyncio 事件循环中

用法：
  python main.py           正常启动
  python main.py --test    开启测试模式（记录所有日志和 AI prompt 到 data/test_logs/）
"""
import argparse
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
from bot import test_mode


async def main(test: bool = False):
    if test:
        ts = test_mode.start()
        logger.info(f"🧪 测试模式已开启，日志：data/test_logs/test_{ts}.jsonl")

    try:
        # 1. 初始化数据库
        db = Database(config.DB_PATH)
        logger.info(f"📦 数据库已就绪: {config.DB_PATH}")

        # 2. 注入数据库到 FastAPI
        set_database(db)

        # 3. 初始化 Discord Bot
        bot = LifeTrackerBot(db)

        # 4. 初始化定时调度器
        scheduler = Scheduler(
            db,
            bot.send_proactive_message,
            bot.fetch_history_for_scheduler,
            is_user_typing_callback=bot.is_user_typing,
        )
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
    finally:
        if test_mode.is_active():
            final_path = test_mode.stop()
            if final_path:
                logger.info(f"🏁 测试模式已结束，日志已保存：{final_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="开启测试模式")
    args = parser.parse_args()
    asyncio.run(main(test=args.test))
