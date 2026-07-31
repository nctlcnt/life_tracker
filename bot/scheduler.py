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
from datetime import datetime, time, timedelta
from bot.ai_engine import scheduled_action
from bot.database import Database
from bot.memory import MemoryService
from bot.prompts import get_prompt_template
from bot.logger import get_logger
import config

# random_poll 的小窗口预算（token）：主动闲聊不需要长上下文，
# 对应窗口化之前「只取最近 8 条」的省 token 特例
POLL_WINDOW_MAX_TOKENS = 2000

logger = get_logger(__name__)

CALENDAR_REFRESH_HOUR = 6
CALENDAR_REFRESH_MINUTE = 5
REMINDER_BATCH_WINDOW = timedelta(minutes=5)

# Prompt 模板统一在 bot/prompts.py 里定义，避免多处重复维护同一条规则


class Scheduler:
    def __init__(self, db: Database, send_callback, is_user_typing_callback=None,
                 memory_service: MemoryService | None = None):
        """
        send_callback: 一个 async 函数，用于发送消息到 Discord
            例如 bot.send_proactive_message
        is_user_typing_callback: 同步函数 () -> bool，
            返回用户当前是否在输入；仅随机轮询会用来决定是否让路
        """
        self.db = db
        self.memory = memory_service or MemoryService(db)
        self.send = send_callback
        self.is_user_typing = is_user_typing_callback or (lambda: False)
        self._running = False
        self._ai_lock = asyncio.Lock()  # 防止 timer 和 reminder 循环同时调用 AI
        self._reminder_event = asyncio.Event()  # 新增提醒时唤醒 reminder 循环
        # 上次 AI 调用完成的时刻（用于计算下次 poll）；启动时设为 now
        self._last_ai_call_ts: datetime = datetime.now()
        # 唤醒 timer 循环重新计算下次 poll（chat 调用完成时使用）
        self._timer_event = asyncio.Event()
        self._last_calendar_alert_date = None

    def notify_new_reminder(self):
        """外部调用：通知 reminder 循环有新提醒插入，重新计算倒计时"""
        self._reminder_event.set()

    def notify_ai_call_done(self):
        """外部调用：任何 AI 调用完成（chat / poll / reminder / bedtime）后调用，
        重置 poll 基准时间。chat 路径需要 Discord Bot 显式调用。"""
        self._last_ai_call_ts = datetime.now()
        for check_in in self.db.list_check_ins(enabled_only=True):
            if check_in.get("schedule_type") == "after_ai_call":
                self.db.set_check_in_last_scheduled(check_in["id"], None)
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
                from bot.google_calendar import CalendarNotConfigured, refresh_calendar_context
                result = refresh_calendar_context()
                logger.info(
                    f"📅 Google Calendar 缓存已刷新: {result.get('count', 0)} events"
                )
            except CalendarNotConfigured as e:
                logger.warning(f"⚠️ Google Calendar 需要重新授权/配置: {e}")
                await self._send_calendar_health_alert(e)
            except Exception as e:
                logger.warning(f"⚠️ Google Calendar 缓存刷新失败: {e}")
                if self._should_alert_calendar_error(e):
                    await self._send_calendar_health_alert(e)

    def _should_alert_calendar_error(self, error: Exception) -> bool:
        text = f"{type(error).__name__}: {error!r} {error}".lower()
        markers = (
            "invalid_grant",
            "expired or revoked",
            "accessnotconfigured",
            "calendar api has not been used",
            "calendar api",
            "disabled",
            "redirect_uri_mismatch",
            "scope has changed",
            "missing code verifier",
        )
        return any(marker in text for marker in markers)

    async def _send_calendar_health_alert(self, error: Exception) -> None:
        today = datetime.now().date()
        if self._last_calendar_alert_date == today:
            return
        self._last_calendar_alert_date = today
        message = (
            "⚠️ Google Calendar 自动刷新失败，日程上下文可能已经不可用。\n"
            "请运行 `/calendar auth` 重新授权；如果刚换了 Google Cloud 项目，也确认已启用 Google Calendar API。\n"
            f"错误：`{type(error).__name__}: {str(error)[:1200]}`"
        )
        try:
            await self.send(message)
        except Exception as send_error:
            logger.warning(f"⚠️ Google Calendar 健康提醒发送失败: {send_error}")

    def poll_enabled(self) -> bool:
        """随机轮询开关（app_state 持久化，/poll 命令切换）。缺省为开。"""
        check_in = self.db.get_check_in("random_poll")
        if check_in is not None:
            return bool(check_in.get("enabled"))
        return self.db.get_state("poll_enabled") != "0"

    async def _timer_loop(self):
        """
        内存倒计时循环，负责：
        - 随机轮询：以"上次 AI 调用 + 45-55min 随机"为基准（可用 /poll 开关）
        - 睡前提醒（每晚 22:30-23:30 和 23:30-00:00 各一次，不受开关影响）

        notify_ai_call_done() 触发的 _timer_event 会唤醒 sleep，
        让循环根据新的基准时间重算下次 poll；/poll 切换后也靠它即时生效。
        """
        while self._running:
            all_times = self._calc_checkin_times(datetime.now())

            all_times.sort(key=lambda x: x[0])

            if not all_times:
                # 轮询关闭且今晚睡前提醒都已过：干等到被唤醒或半小时后重查
                self._timer_event.clear()
                try:
                    await asyncio.wait_for(self._timer_event.wait(), timeout=1800)
                except asyncio.TimeoutError:
                    pass
                continue

            next_time, check_in = all_times[0]
            wait = max((next_time - datetime.now()).total_seconds(), 1)

            label = check_in.get("label") or check_in.get("name") or "check-in"
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

            await self._do_check_in(check_in, timestamp)

    def _calc_checkin_times(self, now: datetime) -> list[tuple[datetime, dict]]:
        """Calculate future fire times for enabled configurable check-ins."""
        out: list[tuple[datetime, dict]] = []
        for check_in in self.db.list_check_ins(enabled_only=True):
            schedule_type = check_in.get("schedule_type")
            if schedule_type == "after_ai_call":
                if not self.db.ttl_followup_enabled():
                    continue
                min_minutes = int(check_in.get("interval_min_minutes") or 45)
                max_minutes = int(check_in.get("interval_max_minutes") or min_minutes)
                if max_minutes < min_minutes:
                    max_minutes = min_minutes
                next_time = self._scheduled_after_ai_call_time(check_in)
                if next_time is None:
                    seconds = random.randint(min_minutes * 60, max_minutes * 60)
                    next_time = self._last_ai_call_ts + timedelta(seconds=seconds)
                    self.db.set_check_in_last_scheduled(
                        check_in["id"], next_time.isoformat(timespec="seconds")
                    )
                if next_time > now:
                    out.append((next_time, check_in))
                continue

            if schedule_type == "window":
                next_time = self._next_window_checkin_time(check_in, now)
                if next_time is not None:
                    out.append((next_time, check_in))
        return out

    def _scheduled_after_ai_call_time(self, check_in: dict) -> datetime | None:
        raw = check_in.get("last_scheduled_for")
        if not raw:
            return None
        try:
            scheduled = Database.normalize_local_time(raw)
        except (TypeError, ValueError):
            return None
        if scheduled <= self._last_ai_call_ts:
            return None
        return scheduled

    def _next_window_checkin_time(self, check_in: dict, now: datetime) -> datetime | None:
        days = check_in.get("days_of_week")
        start_raw = check_in.get("time_start") or "09:00"
        end_raw = check_in.get("time_end") or start_raw
        for offset in range(0, 8):
            day = now.date() + timedelta(days=offset)
            if days is not None and day.weekday() not in days:
                continue
            start = datetime.combine(day, self._parse_hhmm(start_raw))
            end = datetime.combine(day, self._parse_hhmm(end_raw))
            if end <= start:
                end += timedelta(days=1)
            if self._already_fired_for_window(check_in, start, end):
                continue
            scheduled = self._scheduled_for_window(check_in, start, end)
            if scheduled is None:
                span = max(int((end - start).total_seconds()), 0)
                scheduled = start + timedelta(seconds=random.randint(0, span))
                self.db.set_check_in_last_scheduled(
                    check_in["id"], scheduled.isoformat(timespec="seconds")
                )
                check_in["last_scheduled_for"] = scheduled.isoformat(timespec="seconds")
            if scheduled > now:
                return scheduled
        return None

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))

    @staticmethod
    def _scheduled_for_window(check_in: dict, start: datetime, end: datetime) -> datetime | None:
        raw = check_in.get("last_scheduled_for")
        if not raw:
            return None
        try:
            scheduled = Database.normalize_local_time(raw)
        except (TypeError, ValueError):
            return None
        if start <= scheduled <= end:
            return scheduled
        return None

    @staticmethod
    def _already_fired_for_window(check_in: dict, start: datetime, end: datetime) -> bool:
        raw = check_in.get("last_fired_at")
        if not raw:
            return False
        try:
            fired = Database.normalize_local_time(raw)
        except (TypeError, ValueError):
            return False
        return start <= fired <= end

    async def _do_proactive_check(self, timestamp: str):
        """执行随机轮询。AI 忙或用户正在输入时直接跳过，进入下一轮倒计时。"""
        check_in = self.db.get_check_in("random_poll")
        if check_in:
            await self._do_check_in(check_in, timestamp)

    async def _do_bedtime_reminder(self, timestamp: str):
        """执行睡前提醒"""
        check_in = self.db.get_check_in("bedtime_1")
        if check_in:
            await self._do_check_in(check_in, timestamp)

    async def _do_check_in(self, check_in: dict, timestamp: str):
        """Execute a configurable system check-in."""
        name = check_in.get("name") or "check_in"
        if not check_in.get("enabled"):
            logger.info(f"⏭️ check-in 已关闭，跳过: {name}")
            return
        if name == "random_poll" and self.is_user_typing():
            logger.info("⏭️ 用户正在输入，跳过本次随机 check-in")
            return
        should_mark_fired = False
        async with self._ai_lock:
            try:
                prompt = self._render_check_in_prompt(check_in, timestamp)
                # random_poll 保留小窗口省 token（原 8 条特例的 token 版）；
                # 其余 check-in 用完整 token 窗口
                window = self.memory.context_window(
                    str(config.CHANNEL_ID),
                    max_tokens=POLL_WINDOW_MAX_TOKENS if name == "random_poll" else None,
                )
                reply = await scheduled_action(
                    self.db, prompt, timestamp, window.messages,
                    send_callback=self.send,
                    allow_silent=bool(check_in.get("allow_silent", True)),
                    trigger="check_in",
                    tool_profile=check_in.get("tool_profile") or "poll",
                    check_in_name=name,
                    context_config=check_in.get("context_config") or {},
                    window=window,
                )
                should_mark_fired = True
                if reply:
                    logger.info(f"📋 check-in {name}: {reply[:50]}...")
            except Exception as e:
                logger.exception(f"❌ check-in 出错 [{name}]: {e}")
            finally:
                if should_mark_fired:
                    self.db.mark_check_in_fired(check_in["id"])
                self.notify_ai_call_done()

    @staticmethod
    def _render_check_in_prompt(check_in: dict, timestamp: str) -> str:
        template = check_in.get("prompt_template") or ""
        values = {
            "timestamp": timestamp,
            "name": check_in.get("name") or "",
            "label": check_in.get("label") or check_in.get("name") or "",
            "instructions": check_in.get("instructions") or "",
        }
        return template.format(**values)

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
                window = self.memory.context_window(str(config.CHANNEL_ID))
                reply = await scheduled_action(
                    self.db, prompt, timestamp, window.messages,
                    send_callback=self.send,
                    trigger="reminder",
                    tool_profile="reminder_safe",
                    window=window,
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
                # 当前这条要等发送成功后才 mark done，此刻仍算在 remaining 里，故 +1
                group_info = f"（这是关于此事的第{total - len(remaining) + 1}条提醒，共{total}条）"
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
