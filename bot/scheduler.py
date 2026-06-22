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
CALENDAR_REFRESH_HOUR = 6
CALENDAR_REFRESH_MINUTE = 5
REMINDER_BATCH_WINDOW = timedelta(minutes=5)

# Prompt 模板统一在 bot/prompts.py 里定义，避免多处重复维护同一条规则


class Scheduler:
    def __init__(self, db: Database, send_callback, is_user_typing_callback=None):
        """
        send_callback: 一个 async 函数，用于发送消息到 Discord
            例如 bot.send_proactive_message
        is_user_typing_callback: 同步函数 () -> bool，
            返回用户当前是否在输入；仅随机轮询会用来决定是否让路
        """
        self.db = db
        self.send = send_callback
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
            self._calendar_refresh_loop(),
        )

    async def stop(self):
        self._running = False
        self._reminder_event.set()  # 唤醒可能在 sleep 的 reminder 循环

    # ── Timer 循环：随机轮询 + 睡前提醒 ──────────────────────

    async def _calendar_refresh_loop(self):
        """Refresh Google Calendar prompt cache once every morning."""
        while self._running:
            now = datetime.now()
            next_refresh = datetime.combine(
                now.date(),
                datetime.min.time().replace(
                    hour=CALENDAR_REFRESH_HOUR,
                    minute=CALENDAR_REFRESH_MINUTE,
                ),
            )
            if next_refresh <= now:
                next_refresh += timedelta(days=1)
            wait = max((next_refresh - now).total_seconds(), 1)
            logger.info(
                f"📅 下次 Google Calendar 缓存在 {next_refresh.strftime('%Y-%m-%d %H:%M:%S')} 刷新"
            )
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise
            if not self._running:
                break
            try:
                from bot.google_calendar import refresh_calendar_context
                result = refresh_calendar_context()
                logger.info(
                    f"📅 Google Calendar 缓存已刷新: {result.get('count', 0)} events"
                )
            except Exception as e:
                logger.warning(f"⚠️ Google Calendar 缓存刷新失败: {e}")

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
                sections = self.db.get_prompt_sections()
                prompt = get_proactive_prompt(
                    config.get_active().provider,
                    sections,
                ).format(timestamp=timestamp)
                # poll 路径只判断"要不要说话"，历史拉短一点省 token
                history = self.db.get_recent_ai_messages(str(config.CHANNEL_ID), limit=8)
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
                    self.db.get_prompt_sections(),
                ).format(timestamp=timestamp)
                history = self.db.get_recent_ai_messages(str(config.CHANNEL_ID), limit=20)
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
                # DB 会把新 reminder 规范化成本地 naive ISO；旧数据里若仍有
                # Z/+offset，也统一按本地 naive 域解析。
                next_time = self.db.normalize_local_time(next_time_str)
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
        due = self.db.get_pending_reminders()
        if not due:
            return

        first_due = min(
            self.db.normalize_local_time(r["trigger_time"])
            for r in due
        )
        cutoff = (first_due + REMINDER_BATCH_WINDOW).isoformat()
        batch = self.db.get_pending_reminders_until(cutoff)
        if not batch:
            return

        if not self._running:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        context_action = self._format_reminder_batch(batch)
        delivered = False

        async with self._ai_lock:
            try:
                prompt = get_prompt_template(
                    "reminder",
                    self.db.get_prompt_sections(),
                ).format(
                    timestamp=timestamp, action=context_action
                )
                history = self.db.get_recent_ai_messages(str(config.CHANNEL_ID), limit=20)
                reply = await scheduled_action(
                    self.db, prompt, timestamp, history,
                    send_callback=self.send,
                    trigger="reminder"
                )
                if reply and "[SILENT]" not in reply:
                    logger.info(f"🔔 提醒发送: {reply[:50]}...")
                    delivered = True
            except Exception as e:
                logger.exception(f"❌ 提醒处理出错: {e}")
                delivered = await self._send_reminder_fallback(context_action)
            finally:
                if delivered:
                    for reminder in batch:
                        self.db.mark_reminder_done(reminder["id"])
                self.notify_ai_call_done()

    async def _send_reminder_fallback(self, context_action: str) -> bool:
        """AI 调用失败时的兜底提醒。发送成功才允许标记 reminder done。"""
        text = f"提醒到了：\n{context_action}"
        try:
            await self.send(text)
            logger.info("🔔 已发送兜底提醒")
            return True
        except Exception as send_err:
            logger.exception(f"❌ 兜底提醒发送失败: {send_err}")
            return False

    def _format_reminder_batch(self, reminders: list[dict]) -> str:
        """把同一触发窗口内的 reminders 合并成给 AI 的上下文。"""
        if len(reminders) == 1:
            reminder = reminders[0]
            group_info = ""
            if reminder.get("group_id"):
                remaining = self.db.get_pending_reminders_by_group(reminder["group_id"])
                total = self.db.count_reminders_in_group(reminder["group_id"])
                group_info = f"（这是关于此事的第{total - len(remaining)}条提醒，共{total}条）"
            return (
                f"{reminder['action']}\n"
                f"优先级: {reminder.get('priority', 'normal')}\n"
                f"{group_info}"
            ).strip()

        lines = ["以下 reminders 在同一触发窗口内一起到期："]
        for reminder in reminders:
            group = f", group={reminder['group_id']}" if reminder.get("group_id") else ""
            lines.append(
                f"- [id={reminder['id']}{group}] {reminder['trigger_time']} | "
                f"{reminder.get('priority', 'normal')} | {reminder['action']}"
            )
        return "\n".join(lines)
