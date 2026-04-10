"""
定时调度模块
1. 随机轮询：每隔随机时间让 AI 决定是否主动发消息
2. 提醒检查：每分钟检查是否有到期的提醒
3. 睡前提醒：每晚 22:30-23:30 和 23:30-00:00 各随机一次提醒睡觉
"""
import asyncio
import random
from datetime import datetime, timedelta
from bot.ai_engine import proactive_check, reminder_action
from bot.database import Database
import config


class Scheduler:
    def __init__(self, db: Database, send_callback):
        """
        send_callback: 一个 async 函数，用于发送消息到 Discord
        例如 bot.send_proactive_message
        """
        self.db = db
        self.send = send_callback
        self._running = False

    async def start(self):
        """启动所有定时任务"""
        self._running = True
        print("⏰ 定时调度器已启动")
        await asyncio.gather(
            self._random_poll_loop(),
            self._reminder_check_loop(),
            self._bedtime_reminder_loop()
        )

    async def stop(self):
        self._running = False

    async def _random_poll_loop(self):
        """随机轮询：每 1-60 分钟触发一次"""
        while self._running:
            # 随机等待
            wait_seconds = random.randint(
                config.POLL_MIN_SECONDS,
                config.POLL_MAX_SECONDS
            )
            print(f"🔄 下次轮询在 {wait_seconds}s 后")
            await asyncio.sleep(wait_seconds)

            if not self._running:
                break

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            try:
                reply = await proactive_check(self.db, timestamp,
                                              send_callback=self.send)
                if reply:
                    print(f"📤 主动发送: {reply[:50]}...")
                    # reply 已在各轮次中通过 callback 发出
            except Exception as e:
                print(f"❌ 轮询出错: {e}")

    async def _reminder_check_loop(self):
        """每 30 秒检查一次是否有到期的提醒"""
        while self._running:
            await asyncio.sleep(30)

            if not self._running:
                break

            try:
                pending = self.db.get_pending_reminders()
                for reminder in pending:
                    # 先标记完成，防止下一轮检查重复触发
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
                    
                    reply = await reminder_action(
                        self.db, context_action, timestamp
                    )
                    if reply and "[SILENT]" not in reply:
                        await self.send(reply)
            except Exception as e:
                print(f"❌ 提醒检查出错: {e}")

    async def _bedtime_reminder_loop(self):
        """
        睡前提醒：每晚两次
        - 第一次：22:30 ~ 23:30 之间随机时间
        - 第二次：23:30 ~ 00:00 之间随机时间
        """
        while self._running:
            # 计算今晚两个提醒的随机触发时间
            now = datetime.now()
            today = now.date()

            # 第一次：22:30 ~ 23:30
            t1_start = datetime.combine(today, datetime.min.time().replace(hour=22, minute=30))
            t1_end = datetime.combine(today, datetime.min.time().replace(hour=23, minute=30))

            # 第二次：23:30 ~ 00:00
            t2_start = datetime.combine(today, datetime.min.time().replace(hour=23, minute=30))
            t2_end = datetime.combine(today + timedelta(days=1), datetime.min.time())  # 次日 00:00

            # 随机生成触发时间
            t1 = t1_start + timedelta(seconds=random.randint(0, int((t1_end - t1_start).total_seconds())))
            t2 = t2_start + timedelta(seconds=random.randint(0, int((t2_end - t2_start).total_seconds())))

            triggers = [t1, t2]

            # 过滤掉已经过去的时间
            triggers = [t for t in triggers if t > now]

            if not triggers:
                # 今晚的都过了，等到明天 22:30
                tomorrow_start = datetime.combine(today + timedelta(days=1), datetime.min.time().replace(hour=22, minute=30))
                wait = (tomorrow_start - now).total_seconds()
                print(f"😴 今晚睡前提醒已过，等待明天 ({int(wait)}s 后)")
                await asyncio.sleep(wait)
                continue

            for trigger_time in triggers:
                if not self._running:
                    break

                wait = (trigger_time - datetime.now()).total_seconds()
                if wait <= 0:
                    continue

                print(f"😴 下次睡前提醒在 {trigger_time.strftime('%H:%M')} ({int(wait)}s 后)")
                await asyncio.sleep(wait)

                if not self._running:
                    break

                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                try:
                    reply = await reminder_action(
                        self.db, "提醒用户该睡觉了，关心一下用户今天过得怎么样", timestamp
                    )
                    if reply:
                        print(f"😴 睡前提醒: {reply[:50]}...")
                        await self.send(reply)
                except Exception as e:
                    print(f"❌ 睡前提醒出错: {e}")

            # 今晚的提醒全部完成，等到明天 22:30
            now = datetime.now()
            tomorrow_start = datetime.combine(now.date() + timedelta(days=1), datetime.min.time().replace(hour=22, minute=30))
            wait = (tomorrow_start - now).total_seconds()
            print(f"😴 今晚提醒完毕，明天 22:30 再来 ({int(wait)}s 后)")
            await asyncio.sleep(wait)

