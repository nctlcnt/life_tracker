"""
主入口：同时启动 Discord Bot + 定时调度器 + FastAPI 服务
三个模块跑在同一个 asyncio 事件循环中

用法：
  python main.py             正常启动
  python main.py --test      开启测试模式（记录所有日志和 AI prompt 到 data/test_logs/）
  python main.py --api-only  只起 FastAPI（不连 Discord、不跑调度器），用于本地前端调试
"""
import os
import sys

# --api-only 必须在 import config 之前生效，让 config 跳过 Discord/AI 字段校验
if "--api-only" in sys.argv:
    os.environ["LIFE_TRACKER_API_ONLY"] = "1"

import argparse
import asyncio
import contextlib
import uvicorn
import config
from bot.logger import setup_logging, get_logger

# 日志必须最先初始化，确保后续 import 的模块拿到的 logger 已配置
setup_logging(config.LOG_LEVEL, config.LOG_FILE)
logger = get_logger(__name__)

# 进程时区在所有 datetime.now() 调用前固定下来
from bot.timezone_state import init_timezone
init_timezone(config.TIMEZONE)

from bot.database import Database
from bot.memory import MarkdownMemoryRepository, MemoryService
from api.server import (
    app as fastapi_app,
    set_check_in_changed_callback,
    set_check_in_trigger,
    set_database,
)
from bot import test_mode


async def main(test: bool = False, api_only: bool = False):
    if test:
        ts = test_mode.start()
        logger.info(f"🧪 测试模式已开启，日志：data/test_logs/test_{ts}.jsonl")

    try:
        # 1. 初始化数据库
        db = Database(config.DB_PATH)
        durable_memory = MarkdownMemoryRepository(
            config.MEMORY_PATH,
            legacy_repository=db,
            token_budget=config.MEMORY_TOKEN_BUDGET,
        )
        memory = MemoryService(db, durable_repository=durable_memory)
        logger.info(f"📦 数据库已就绪: {config.DB_PATH}")

        # 2. 注入数据库到 FastAPI
        set_database(db, memory)

        # 5. 启动 FastAPI（在后台线程中运行，不阻塞事件循环）
        api_config = uvicorn.Config(
            fastapi_app,
            host="0.0.0.0",
            port=config.API_PORT,
            log_level="info"
        )
        api_server = uvicorn.Server(api_config)

        if api_only:
            logger.info("🚀 api-only 模式：只启动 FastAPI（不连 Discord、不跑调度器）")
            logger.info(f"   - FastAPI 接口 (端口: {config.API_PORT})")
            await api_server.serve()
            return

        # Resume best-effort embedding only for history already covered by compact.
        from bot.memory.context_window import load_summary_state
        from bot.memory.history_embedding import schedule_compacted_embeddings
        summary_state = load_summary_state(db, str(config.CHANNEL_ID))
        if summary_state:
            schedule_compacted_embeddings(
                db, str(config.CHANNEL_ID),
                int(summary_state.get("upto_message_id", 0)),
            )

        # 这些 import 放在这里，以便 --api-only 时无需安装 discord.py 等重依赖也能起来
        import discord
        from bot.async_pipeline import (
            BatchCoordinator,
            BatchHeartbeat,
            GenerationGate,
            OutboundDeliveryRepository,
            OutboundQueue,
            ToolBatchRepository,
            ToolResultExpresser,
            ToolWorker,
        )
        from bot.ai_engine import express_tool_result, run_tool_worker
        from bot.discord_bot import LifeTrackerBot
        from bot.scheduler import Scheduler

        outbound_repository = OutboundDeliveryRepository(db)
        tool_repository = ToolBatchRepository(db)
        reconciled_deliveries = tool_repository.reconcile_queued_deliveries()
        if reconciled_deliveries:
            logger.info(
                "♻️ 启动时修复工具结果投递状态: %s",
                reconciled_deliveries,
            )
        execution_mode = "apply" if config.ASYNC_TOOL_APPLY else "shadow"
        if not config.ASYNC_OUTBOUND_ENABLED:
            stranded_delivery_ids = outbound_repository.non_terminal_ids()
            if stranded_delivery_ids:
                raise RuntimeError(
                    "cannot disable outbound queue with non-terminal deliveries: "
                    + ", ".join(str(item) for item in stranded_delivery_ids)
                )
        if not config.ASYNC_TOOL_WORKER_ENABLED:
            stranded_batch_ids = [
                *tool_repository.non_terminal_ids(),
                *tool_repository.unsettled_delivery_ids(),
            ]
            if stranded_batch_ids:
                raise RuntimeError(
                    "cannot disable tool worker with unfinished batches/deliveries: "
                    + ", ".join(stranded_batch_ids)
                )
        else:
            previous_mode = (
                "shadow" if execution_mode == "apply" else "apply"
            )
            stranded_previous_mode_ids = [
                *tool_repository.non_terminal_ids(
                    execution_mode=previous_mode
                ),
                *tool_repository.unsettled_delivery_ids(
                    execution_mode=previous_mode
                ),
            ]
            if stranded_previous_mode_ids:
                raise RuntimeError(
                    f"cannot switch tool worker to {execution_mode} with "
                    f"unfinished {previous_mode} batches/deliveries: "
                    + ", ".join(stranded_previous_mode_ids)
                )
        tool_cutovers = tool_repository.prepare_runtime(
            enabled=config.ASYNC_TOOL_WORKER_ENABLED,
            channel_ids=[str(config.CHANNEL_ID)],
        )
        if tool_cutovers:
            logger.info(
                "✂️ 执行轨从当前消息尾部开始，不回放旧机制消息: %s",
                tool_cutovers,
            )

        # 3. 初始化 Discord Bot
        generation_gate = (
            GenerationGate() if config.ASYNC_OUTBOUND_ENABLED else None)
        bot = LifeTrackerBot(
            db, memory, generation_gate=generation_gate)
        outbound_queue = None
        if config.ASYNC_OUTBOUND_ENABLED:
            outbound_queue = OutboundQueue(
                outbound_repository,
                bot.deliver_outbound,
            )
            bot.set_outbound_queue(outbound_queue)

        batch_coordinator = None
        tool_worker = None
        heartbeat = None
        if config.ASYNC_TOOL_WORKER_ENABLED:
            # Flag validation guarantees outbound_queue exists here.
            batch_coordinator = BatchCoordinator(
                db,
                tool_repository,
                channel_ids=[str(config.CHANNEL_ID)],
                execution_mode=execution_mode,
            )
            expresser = ToolResultExpresser(
                db,
                express_tool_result,
                generation_gate=generation_gate,
                memory_service=memory,
            )
            tool_worker = ToolWorker(
                db,
                tool_repository,
                run_tool_worker,
                expresser,
                memory_service=memory,
            )
            heartbeat = BatchHeartbeat(
                db,
                tool_repository,
                outbound_queue,
                channel_ids=[str(config.CHANNEL_ID)],
                execution_mode=execution_mode,
                plan_callback=batch_coordinator.plan_channel,
            )
            batch_coordinator.set_batch_ready_callback(tool_worker.wake)

            def _batch_finished(batch):
                batch_coordinator.notify_batch_finished(batch)
                heartbeat.wake()

            tool_worker.set_finished_callback(_batch_finished)

            def _delivery_terminal(delivery, status):
                if delivery.get("source_type") != "tool_batch":
                    return
                tool_repository.advance_delivery(
                    delivery["source_id"],
                    from_status="queued",
                    to_status=status,
                )

            outbound_queue.set_terminal_callback(_delivery_terminal)
            bot.set_batch_coordinator(batch_coordinator)

        # 4. 初始化定时调度器
        scheduler = Scheduler(
            db,
            bot.send_proactive_message,
            is_user_typing_callback=bot.is_user_typing,
            memory_service=memory,
            generation_gate=generation_gate,
            batch_coordinator=batch_coordinator,
            tool_worker_apply=config.ASYNC_TOOL_APPLY,
        )
        db._on_reminder_added = scheduler.notify_new_reminder
        set_check_in_changed_callback(scheduler.notify_schedule_changed)
        set_check_in_trigger(scheduler.trigger_check_in_now)
        # chat 完成后通知 scheduler 重置 poll 基准（避免一个间隔内再轮询）
        bot.on_ai_call_done = scheduler.notify_ai_call_done

        logger.info("🚀 正在启动所有服务...")
        logger.info("   - Discord Bot")
        logger.info("   - 定时调度器 (随机轮询基于上次 AI 调用时刻，间隔见各 check-in 配置)")
        logger.info(
            f"   - 统一发送队列: "
            f"{'enabled' if outbound_queue else 'disabled (direct send)'}")
        logger.info(
            f"   - 工具 worker: "
            f"{'apply' if config.ASYNC_TOOL_APPLY else 'shadow' if tool_worker else 'disabled'}")
        logger.info(f"   - FastAPI 接口 (端口: {config.API_PORT})")

        # Bot token 失效时不要让 Docker 反复重启整个容器刷 Discord 登录接口。
        bot_task = asyncio.create_task(bot.start(config.DISCORD_TOKEN))
        scheduler_task = asyncio.create_task(scheduler.start())
        api_task = asyncio.create_task(api_server.serve())
        outbound_task = (
            asyncio.create_task(outbound_queue.run())
            if outbound_queue else None
        )
        batcher_task = (
            asyncio.create_task(batch_coordinator.run())
            if batch_coordinator else None
        )
        worker_task = (
            asyncio.create_task(tool_worker.run()) if tool_worker else None
        )
        heartbeat_task = (
            asyncio.create_task(heartbeat.run()) if heartbeat else None
        )
        tasks = {bot_task, scheduler_task, api_task}
        tasks.update(
            task for task in (
                outbound_task, batcher_task, worker_task, heartbeat_task
            )
            if task is not None
        )
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )
        login_failed = any(
            task is bot_task
            and isinstance(task.exception(), discord.LoginFailure)
            for task in done
            if not task.cancelled()
        )
        if login_failed:
            logger.error("❌ Discord token 无效；停止 Bot/调度器，仅保留 FastAPI，避免重启风暴")
            await scheduler.stop()
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            if outbound_queue and outbound_task:
                await outbound_queue.stop()
                outbound_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await outbound_task
            for component, task in (
                (batch_coordinator, batcher_task),
                (tool_worker, worker_task),
                (heartbeat, heartbeat_task),
            ):
                if component and task:
                    await component.stop()
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            await bot.close()
            await api_task
        else:
            for task in done:
                task.result()
            await asyncio.gather(*pending)
    finally:
        if test_mode.is_active():
            final_path = test_mode.stop()
            if final_path:
                logger.info(f"🏁 测试模式已结束，日志已保存：{final_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="开启测试模式")
    parser.add_argument("--api-only", action="store_true", help="只启动 FastAPI（本地前端调试用）")
    args = parser.parse_args()
    asyncio.run(main(test=args.test, api_only=args.api_only))
