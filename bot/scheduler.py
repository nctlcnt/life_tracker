"""
定时调度模块
两个并发循环 + 一个 asyncio.Lock 防止并发 AI 调用：

1. Timer 循环：随机轮询（基于"上次 AI 调用 + 45-55min"）+ 睡前提醒
2. Reminder 循环：数据库提醒，倒计时到下一条 + asyncio.Event 响应新增

轮询策略（省钱核心）：
不再固定每 1-60min 随机倒计时，而是以"上一次 AI 调用完成时刻"为基准，
等 45-55min 再触发。任何 chat / poll / reminder / bedtime 都会重置基准——
因为 prompt 都进了 Anthropic，cache 已经付过钱。
"""
import asyncio
import random
from datetime import datetime, timedelta
from bot.ai_engine import scheduled_action
from bot.database import Database
from bot.logger import get_logger
from bot.prompts import get_proactive_prompt, get_prompt_template
import config

logger = get_logger(__name__)

# 随机轮询间隔（秒）：上次 AI 调用后 45-55min 再发起下一次 poll
POLL_INTERVAL_MIN = 45 * 60
POLL_INTERVAL_MAX = 55 * 60

# Prompt 模板统一在 bot/prompts.py 里定义，避免多处重复维护同一条规则


class Scheduler:
    def __init__(self, db: Database, send_callback, fetch_history_callback,
                 is_user_typing_callback=None):
        """
        send_callback: 一个 async 函数，用于发送消息到 Discord
            例如 bot.send_proactive_message
        fetch_history_callback: 一个 async 函数 (limit: int) -> list[dict]，
            从 Discord 拉对话历史交给 AI 引擎作上下文
            例如 bot.fetch_history_for_scheduler
        is_user_typing_callback: 同步函数 () -> bool，
            返回用户当前是否在输入；仅随机轮询会用来决定是否让路
        """
        self.db = db
        self.send = send_callback
        self.fetch_history = fetch_history_callback
        self.is_user_typing = is_user_typing_callback or (lambda: False)
        self._running = False
        self._ai_lock = asyncio.Lock()  # 防止 timer 和 reminder 循环同时调用 AI
        self._reminder_event = asyncio.Event()  # 新增提醒时唤醒 reminder 循环
        # 上次 AI 调用完成的时刻（用于计算下次 poll）；启动时设为 now
        self._last_ai_call_ts: datetime = datetime.now()
        # 唤醒 timer 循环重新计算下次 poll（chat 调用完成时使用）
        self._timer_event = asyncio.Event()

    def notify_new_reminder(self):
        """外部调用：通知 reminder 循环有新提醒插入，重新计算倒计时"""
        self._reminder_event.set()

    def notify_ai_call_done(self):
        """外部调用：任何 AI 调用完成（chat / poll / reminder / bedtime）后调用，
        重置 poll 基准时间。chat 路径需要 Discord Bot 显式调用。"""
        self._last_ai_call_ts = datetime.now()
        self._timer_event.set()

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
        - 随机轮询：以"上次 AI 调用 + 45-55min 随机"为基准
        - 睡前提醒（每晚 22:30-23:30 和 23:30-00:00 各一次）

        notify_ai_call_done() 触发的 _timer_event 会唤醒 sleep，
        让循环根据新的基准时间重算下次 poll。
        """
        while self._running:
            # 下次 poll = 上次 AI 调用时刻 + 45-55min 随机
            poll_seconds = random.randint(POLL_INTERVAL_MIN, POLL_INTERVAL_MAX)
            next_poll = self._last_ai_call_ts + timedelta(seconds=poll_seconds)

            # 计算今晚的睡前提醒时间
            bedtimes = self._calc_bedtimes(datetime.now())

            # 合并所有待触发时间，取最早的
            all_times = [(next_poll, "poll")] + [(t, "bedtime") for t in bedtimes]
            all_times.sort(key=lambda x: x[0])

            next_time, action_type = all_times[0]
            wait = max((next_time - datetime.now()).total_seconds(), 1)

            label = "轮询" if action_type == "poll" else "睡前提醒"
            logger.info(f"🔄 下次{label}在 {next_time.strftime('%H:%M:%S')} ({int(wait)}s 后)")

            # sleep 到时机；期间若有 AI 调用完成会通过 _timer_event 提前唤醒重算
            self._timer_event.clear()
            try:
                await asyncio.wait_for(self._timer_event.wait(), timeout=wait)
                # 被唤醒：上次 AI 调用刚刚完成，基准更新了 → 重算下次 poll
                logger.info("🔄 收到 AI 调用完成通知，重算下次轮询时间")
                continue
            except asyncio.TimeoutError:
                pass  # sleep 到期，正常触发

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
        """执行随机轮询。AI 忙或用户正在输入时直接跳过，进入下一轮倒计时。"""
        if self._ai_lock.locked():
            logger.info("⏭️ AI 正在思考，跳过本次轮询")
            return
        if self.is_user_typing():
            logger.info("⏭️ 用户正在输入，跳过本次轮询")
            return
        async with self._ai_lock:
            try:
                overrides = self.db.get_prompt_overrides()
                prompt = get_proactive_prompt(
                    config.get_active().provider,
                    overrides,
                ).format(timestamp=timestamp)
                # poll 路径只判断"要不要说话"，历史拉短一点省 token
                history = await self.fetch_history(limit=8)
                reply = await scheduled_action(
                    self.db, prompt, timestamp, history,
                    send_callback=self.send, allow_silent=True,
                    trigger="poll"
                )
                if reply:
                    logger.info(f"📤 主动发送: {reply[:50]}...")
            except Exception as e:
                logger.exception(f"❌ 轮询出错: {e}")
            finally:
                # 不管 AI 说没说话，cache 钱已付，重置基准
                self.notify_ai_call_done()

    async def _do_bedtime_reminder(self, timestamp: str):
        """执行睡前提醒"""
        async with self._ai_lock:
            try:
                prompt = get_prompt_template(
                    "bedtime",
                    self.db.get_prompt_overrides(),
                ).format(timestamp=timestamp)
                history = await self.fetch_history(limit=20)
                reply = await scheduled_action(
                    self.db, prompt, timestamp, history,
                    send_callback=self.send,
                    trigger="bedtime"
                )
                if reply:
                    logger.info(f"😴 睡前提醒: {reply[:50]}...")
            except Exception as e:
                logger.exception(f"❌ 睡前提醒出错: {e}")
            finally:
                self.notify_ai_call_done()

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
                # AI 偶尔会写带时区偏移的 ISO 串（如 ...+10:00），此时
                # fromisoformat 返回 tz-aware datetime；但本代码库其它地方
                # 都用 naive local 时间（datetime.now()），直接相减会抛
                # TypeError。统一剥掉 tzinfo 当 naive local 用。
                next_time = datetime.fromisoformat(next_time_str)
                if next_time.tzinfo is not None:
                    next_time = next_time.replace(tzinfo=None)
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
                    prompt = get_prompt_template(
                        "reminder",
                        self.db.get_prompt_overrides(),
                    ).format(
                        timestamp=timestamp, action=context_action
                    )
                    history = await self.fetch_history(limit=20)
                    reply = await scheduled_action(
                        self.db, prompt, timestamp, history,
                        send_callback=self.send,
                        trigger="reminder"
                    )
                    if reply and "[SILENT]" not in reply:
                        logger.info(f"🔔 提醒发送: {reply[:50]}...")
                except Exception as e:
                    logger.exception(f"❌ 提醒处理出错: {e}")
                finally:
                    self.notify_ai_call_done()
