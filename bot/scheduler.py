"""
定时调度模块
两个并发循环 + 一个 asyncio.Lock 防止并发 AI 调用：

1. Timer 循环：随机轮询 + 睡前提醒，纯内存倒计时
2. Reminder 循环：数据库提醒，倒计时到下一条 + asyncio.Event 响应新增
"""
import asyncio
import random
from datetime import datetime, timedelta
from bot.ai_engine import scheduled_action
from bot.database import Database
from bot.logger import get_logger
from bot.prompts import PROACTIVE_PROMPT, REMINDER_PROMPT, BEDTIME_PROMPT
import config

logger = get_logger(__name__)

# Prompt 模板统一在 bot/prompts.py 里定义，避免多处重复维护同一条规则


class Scheduler:
    def __init__(self, db: Database, send_callback, fetch_history_callback):
        """
        send_callback: 一个 async 函数，用于发送消息到 Discord
            例如 bot.send_proactive_message
        fetch_history_callback: 一个 async 函数 (limit: int) -> list[dict]，
            从 Discord 拉对话历史交给 AI 引擎作上下文
            例如 bot.fetch_history_for_scheduler
        """
        self.db = db
        self.send = send_callback
        self.fetch_history = fetch_history_callback
        self._running = False
        self._ai_lock = asyncio.Lock()  # 防止 timer 和 reminder 循环同时调用 AI
        self._reminder_event = asyncio.Event()  # 新增提醒时唤醒 reminder 循环

    def notify_new_reminder(self):
        """外部调用：通知 reminder 循环有新提醒插入，重新计算倒计时"""
        self._reminder_event.set()

    async def start(self):
        """启动所有定时任务"""
        self._running = True
        logger.info("⏰ 定时调度器已启动")
        await asyncio.gather(
            self._timer_loop(),
            self._reminder_loop(),
        )

    async def stop(self):
        self._running = False
        self._reminder_event.set()  # 唤醒可能在 sleep 的 reminder 循环

    # ── Timer 循环：随机轮询 + 睡前提醒 ──────────────────────

    async def _timer_loop(self):
        """
        内存倒计时循环，负责：
        - 随机轮询（1-60 分钟随机间隔）
        - 睡前提醒（每晚 22:30-23:30 和 23:30-00:00 各一次）
        """
        while self._running:
            now = datetime.now()

            # 计算下一个随机轮询时间
            poll_seconds = random.randint(config.POLL_MIN_SECONDS, config.POLL_MAX_SECONDS)
            next_poll = now + timedelta(seconds=poll_seconds)

            # 计算今晚的睡前提醒时间
            bedtimes = self._calc_bedtimes(now)

            # 合并所有待触发时间，取最早的
            all_times = [(next_poll, "poll")] + [(t, "bedtime") for t in bedtimes]
            all_times.sort(key=lambda x: x[0])

            next_time, action_type = all_times[0]
            wait = (next_time - datetime.now()).total_seconds()

            if wait <= 0:
                wait = 1  # 避免负数

            label = "轮询" if action_type == "poll" else "睡前提醒"
            logger.info(f"🔄 下次{label}在 {next_time.strftime('%H:%M:%S')} ({int(wait)}s 后)")
            await asyncio.sleep(wait)

            if not self._running:
                break

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

            if action_type == "poll":
                await self._do_proactive_check(timestamp)
            else:
                await self._do_bedtime_reminder(timestamp)

    def _calc_bedtimes(self, now: datetime) -> list[datetime]:
        """计算今晚的睡前提醒时间（过了的不返回）"""
        today = now.date()

        t1_start = datetime.combine(today, datetime.min.time().replace(hour=22, minute=30))
        t1_end = datetime.combine(today, datetime.min.time().replace(hour=23, minute=30))
        t2_start = datetime.combine(today, datetime.min.time().replace(hour=23, minute=30))
        t2_end = datetime.combine(today + timedelta(days=1), datetime.min.time())

        times = []
        t1 = t1_start + timedelta(seconds=random.randint(0, int((t1_end - t1_start).total_seconds())))
        t2 = t2_start + timedelta(seconds=random.randint(0, int((t2_end - t2_start).total_seconds())))

        for t in [t1, t2]:
            if t > now:
                times.append(t)
        return times

    async def _do_proactive_check(self, timestamp: str):
        """执行随机轮询"""
        async with self._ai_lock:
            try:
                prompt = PROACTIVE_PROMPT.format(timestamp=timestamp)
                history = await self.fetch_history(limit=20)
                reply = await scheduled_action(
                    self.db, prompt, timestamp, history,
                    send_callback=self.send, allow_silent=True
                )
                if reply:
                    logger.info(f"📤 主动发送: {reply[:50]}...")
            except Exception as e:
                logger.exception(f"❌ 轮询出错: {e}")

    async def _do_bedtime_reminder(self, timestamp: str):
        """执行睡前提醒"""
        async with self._ai_lock:
            try:
                prompt = BEDTIME_PROMPT.format(timestamp=timestamp)
                history = await self.fetch_history(limit=20)
                reply = await scheduled_action(
                    self.db, prompt, timestamp, history,
                    send_callback=self.send
                )
                if reply:
                    logger.info(f"😴 睡前提醒: {reply[:50]}...")
            except Exception as e:
                logger.exception(f"❌ 睡前提醒出错: {e}")

    # ── Reminder 循环：数据库提醒，倒计时 + Event 唤醒 ────────

    async def _reminder_loop(self):
        """
        数据库提醒循环：查下一条最早的 reminder，sleep 到那个时刻。
        新增提醒时通过 _reminder_event 唤醒重新计算。
        """
        while self._running:
            # 先处理所有已到期的
            await self._process_due_reminders()

            # 查下一条最早的 trigger_time
            next_time_str = self.db.get_next_reminder_time()

            if next_time_str:
                next_time = datetime.fromisoformat(next_time_str)
                wait = max((next_time - datetime.now()).total_seconds(), 0)
                logger.info(f"⏰ 下条提醒在 {next_time_str} ({int(wait)}s 后)")
            else:
                # 没有待触发的提醒，等 event 唤醒
                wait = 3600  # 最多等 1 小时再检查一次
                logger.info("⏰ 暂无提醒，等待新增...")

            # 等 sleep 到期 或 event 触发（新增提醒）
            self._reminder_event.clear()
            try:
                await asyncio.wait_for(self._reminder_event.wait(), timeout=wait)
                # event 触发了 → 有新提醒插入，回到循环顶部重新算
                logger.info("⏰ 收到新提醒通知，重新计算...")
            except asyncio.TimeoutError:
                # sleep 到期 → 继续处理到期提醒
                pass

    async def _process_due_reminders(self):
        """处理所有已到期的提醒"""
        pending = self.db.get_pending_reminders()
        for reminder in pending:
            if not self._running:
                break

            # 先标记完成，防止重复触发
            self.db.mark_reminder_done(reminder["id"])

            # 查同 group 的信息
            group_info = ""
            if reminder.get('group_id'):
                remaining = self.db.get_pending_reminders_by_group(reminder['group_id'])
                total = self.db.count_reminders_in_group(reminder['group_id'])
                group_info = f"（这是关于此事的第{total - len(remaining)}条提醒，共{total}条）"

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

            context_action = (
                f"{reminder['action']}\n"
                f"优先级: {reminder.get('priority', 'normal')}\n"
                f"{group_info}"
            ).strip()

            async with self._ai_lock:
                try:
                    prompt = REMINDER_PROMPT.format(
                        timestamp=timestamp, action=context_action
                    )
                    history = await self.fetch_history(limit=20)
                    reply = await scheduled_action(
                        self.db, prompt, timestamp, history,
                        send_callback=self.send
                    )
                    if reply and "[SILENT]" not in reply:
                        logger.info(f"🔔 提醒发送: {reply[:50]}...")
                except Exception as e:
                    logger.exception(f"❌ 提醒处理出错: {e}")
