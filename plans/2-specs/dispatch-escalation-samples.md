# Dispatch POC 步骤 0：escalation 触发样本标注表

> 来源：`scripts/extract_dispatch_samples.py`
> spec：[`dispatch-poc.md`](dispatch-poc.md) 实施步骤 0

## 标注方式

把每条前面的 `[ ]` 改成：
- `[Y]` — SMALL_DECIDE 应该 escalate（需要工具/状态查询/记忆操作）
- `[N]` — SMALL_DECIDE 应该直接闲聊回复（情绪、闲扯、persona 反应）
- `[?]` — 不确定，留待讨论

**不必参考 `tool_use_proxy`**——那是 prod AI 当时的实际行为，仅供参考。我们要的是「**理想**情况下小模型该不该 escalate」的判断，可能与 prod 当时不一致。

## 标尺（写 SMALL_DECIDE prompt 前先想清楚）

应当 escalate 的典型场景：
- 涉及具体时间/日期推理 → 要 set_reminder / log_timeline_event / add_deadline
- 涉及历史状态查询 → 要 query_timeline / list_reminders / 调记忆
- 涉及多轮信息收集（如「明天去看医生」需要追问几点哪里）
- 状态信号（深度专注/迈不出第一步/高耗宕机/时间感偏移）→ 大模型决定要不要落工具

不该 escalate 的典型场景：
- 纯情绪倾诉 / 闲聊 / 调侃
- 对前一轮 AI 回复的回应（没有新工具意图）
- 简短确认（嗯、好、知道了）

---

## 必标层：关键词正则命中（共 33 条）

```
[ ] #001  ts=2026-04-15T13:48:38  source=20260415_150022.jsonl  tool_use=NO
    msg: [2026-04-15 13:48] 今天晚上得重新看一遍今天的课了，完全没听，明天还有一节课接着讲的

[ ] #002  ts=2026-04-18T12:13:55  source=20260418_162714.jsonl  tool_use=YES (2 轮: update_timeline_event)
    msg: [2026-04-18 12:13] 就是很……焦虑 / 感觉要开始复习考试了

[ ] #003  ts=2026-04-18T16:12:44  source=20260418_162714.jsonl  tool_use=YES (3 轮: save_memory,delete_memory,set_reminder)
    msg: [2026-04-18 16:12] 今晚8点，两小时

[ ] #004  ts=2026-04-18T16:17:54  source=20260418_162714.jsonl  tool_use=YES (2 轮: query_timeline)
    msg: [2026-04-18 16:17] 不对诶，今天是周六，你再查一查

[ ] #005  ts=2026-04-18T16:19:11  source=20260418_162714.jsonl  tool_use=YES (2 轮: query_timeline)
    msg: [2026-04-18 16:19] 但是定的是今天下午吃anita，为什么说是明天？

[ ] #006  ts=2026-04-18T16:20:13  source=20260418_162714.jsonl  tool_use=YES (3 轮: save_memory,delete_memory)
    msg: [2026-04-18 16:20] 今天周六，今天4月18日，都有什么安排？

[ ] #007  ts=2026-04-18T16:25:03  source=20260418_162714.jsonl  tool_use=YES (4 轮: save_memory,update_memory)
    msg: [2026-04-18 16:25] 对，然后周日会去kiama，大概7:30就要出门

[ ] #008  ts=2026-04-18T16:33:50  source=20260418_164122.jsonl  tool_use=NO
    msg: [2026-04-18 16:33] 记得提醒我起床

[ ] #009  ts=2026-04-18T23:42:42  source=20260419_000550.jsonl  tool_use=YES (2 轮: update_memory)
    msg: [2026-04-18 23:42] 明天早上8:00集合…可以坐7:33的343车或者7:27的370，那之前还要洗澡（对我今晚没洗，好累啊～）然后要准备个东西去吃…

[ ] #010  ts=2026-04-18T23:48:57  source=20260419_000550.jsonl  tool_use=YES (2 轮: update_memory)
    msg: [2026-04-18 23:48] 等等，我朋友改行程了…我明天坐370倒t4去hurstville，赶8:49那趟火车…我查了要不只能提前去，8:10到，要不就8:40到…

[ ] #011  ts=2026-04-18T23:50:10  source=20260419_000550.jsonl  tool_use=YES (3 轮: update_memory,update_memory)
    msg: [2026-04-18 23:50] ok，那我就要坐7:27的370

[ ] #012  ts=2026-04-18T23:55:31  source=20260419_000550.jsonl  tool_use=NO
    msg: [2026-04-18 23:55] 明天穿什么呢…

[ ] #013  ts=2026-04-19T07:37:14  source=20260420_113549.jsonl  tool_use=YES (2 轮: update_timeline_event,log_timeline_event)
    msg: [2026-04-19 07:37] 出门了！我被公交车算计了！ / 7:30那车就跑了…

[ ] #014  ts=2026-04-19T07:38:22  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 07:38] 是我记错了…记成了7:33…

[ ] #015  ts=2026-04-19T07:42:20  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 07:42] 到公交站了… / 等一会倒车7:59的火车，希望能赶上

[ ] #016  ts=2026-04-19T08:01:56  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 08:01] 坐上前一班火车了！我将在8:20到达火车站～然后赶上8:49的火车！

[ ] #017  ts=2026-04-19T08:05:11  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 08:05] 我们约在火车站见，我是觉得去那边两小时的车程，单独坐吗？

[ ] #018  ts=2026-04-19T14:06:59  source=20260420_113549.jsonl  tool_use=YES (2 轮: list_reminders)
    msg: [2026-04-19 14:06] 我今天有什么安排？

[ ] #019  ts=2026-04-19T14:08:02  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 14:08] 后天师傅上门

[ ] #020  ts=2026-04-19T19:21:34  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 19:21] 嗯！明天周一，我有什么事情要做？

[ ] #021  ts=2026-04-19T19:23:07  source=20260420_113549.jsonl  tool_use=YES (2 轮: save_memory,set_reminder,set_reminder,set_reminder)
    msg: [2026-04-19 19:23] 今晚或者明晚想泡澡～然后明天开始看5916的作业吧 / 然后5916逃了两节课，要在周三下午1点上课前补上

[ ] #022  ts=2026-04-19T19:24:09  source=20260420_113549.jsonl  tool_use=YES (2 轮: save_memory)
    msg: [2026-04-19 19:24] 周二下午要去游泳课，是3:30的

[ ] #023  ts=2026-04-19T19:25:38  source=20260420_113549.jsonl  tool_use=YES (3 轮: set_reminder,set_reminder,set_reminder,delete_reminder)
    msg: [2026-04-19 19:25] 明天好像一天都空闲，我准备专心搞学习！如果我特别出格，你要提醒我！

[ ] #024  ts=2026-04-19T19:35:00  source=20260420_113549.jsonl  tool_use=YES (2 轮: update_timeline_event)
    msg: [2026-04-19 19:34] 好～ / [2026-04-19 19:34] 我还在火车上呢…

[ ] #025  ts=2026-04-19T19:38:52  source=20260420_113549.jsonl  tool_use=YES (2 轮: save_memory)
    msg: [2026-04-19 19:38] 帮我记住下周五我会去看歌剧魅影下午7:30

[ ] #026  ts=2026-04-19T23:25:52  source=20260420_113549.jsonl  tool_use=NO
    msg: [睡前提醒 2026-04-19 23:25] 提醒她该睡了，顺便关心一下今天过得怎么样，语气自然温柔，不说教。

[ ] #027  ts=2026-04-20T13:39:15  source=20260420_172620.jsonl  tool_use=NO
    msg: [2026-04-20 13:39] 马上就要考试了…焦虑

[ ] #028  ts=2026-04-20T14:06:23  source=20260420_172620.jsonl  tool_use=YES (2 轮: update_memory)
    msg: [2026-04-20 14:06] 没呢～ / 明天洗碗机的回信：Hi, confirming your appointment with The Appliance Guys for tomorrow between 7am and 9am.  Our technician will be attending and will call on approach. If you need to cancel or reschedule, please reply to this message or call us on 1300 567 637

[ ] #029  ts=2026-04-21T00:40:18  source=20260421_124310.jsonl  tool_use=YES (2 轮: set_reminder)
    msg: [2026-04-21 00:40] 明早上第一个事项前给我发个提醒～

[ ] #030  ts=2026-04-21T07:05:06  source=20260421_124310.jsonl  tool_use=YES (2 轮: save_memory)
    msg: [2026-04-21 07:05] 今天晚上6:00-800要去上课

[ ] #031  ts=2026-04-21T11:22:03  source=20260421_124310.jsonl  tool_use=NO
    msg: [2026-04-21 11:22] 我也不知道…嗯…帮我考试？

[ ] #032  ts=2026-04-21T13:17:45  source=20260421_132008.jsonl  tool_use=YES (4 轮: update_timeline_event,log_timeline_event,log_timeline_event)
    msg: [2026-04-21 13:01] 吃了早午饭所以没关系了 [已执行✅] / [2026-04-21 13:17] 我也不知道为什么，感觉有时候很陌生 / 比如躺在床上看手机写代码，是很习惯很“我”的一件事 / 但是打开课件打开作业或想想去学校教室，是很陌生很不习惯的一件事，但又必须要做

[ ] #033  ts=2026-04-21T14:15:42  source=20260421_142622.jsonl  tool_use=YES (2 轮: update_timeline_event,update_memory)
    msg: [2026-04-21 14:15] Assessment    Type    Issue Date    Weighting    Length    Aligned CLOs*    Due Date** / 1. Assignment    Individual    Week 1    10%    TBA    CLOs 1-3, 6    Thursday 18:00 in Week 3 / 2. Mid-term Test    Individual    Week 7    20%    75 minutes    CLOs 1–4, 6    Thursday 13:00 in W…[截断]

```

## 灰区采样：随机抽 30 条

```
[ ] #034  ts=2026-04-15T13:40:56  source=20260415_150022.jsonl  tool_use=NO
    msg: [2026-04-15 13:40] 晚上要不要去上课呢……跟朋友约好了要去的 / 我大概会睡在课上

[ ] #035  ts=2026-04-15T14:07:41  source=20260415_150022.jsonl  tool_use=YES (2 轮: update_timeline_event,log_timeline_event)
    msg: [2026-04-15 14:07] 找不到好地方了… / 还要等pre预演

[ ] #036  ts=2026-04-15T14:08:01  source=20260415_150022.jsonl  tool_use=NO
    msg: [2026-04-15 14:08] 学校里人好多啊

[ ] #037  ts=2026-04-15T14:30:20  source=20260415_150022.jsonl  tool_use=NO
    msg: [2026-04-15 14:30] 在花园里躺在长椅上

[ ] #038  ts=2026-04-18T16:11:15  source=20260418_162714.jsonl  tool_use=YES (2 轮: save_memory)
    msg: [2026-04-18 16:11] 不知道，你不是记得吗

[ ] #039  ts=2026-04-18T16:13:55  source=20260418_162714.jsonl  tool_use=YES (2 轮: set_reminder)
    msg: [2026-04-18 16:13] 到opera house得50分钟…

[ ] #040  ts=2026-04-18T16:15:37  source=20260418_162714.jsonl  tool_use=YES (2 轮: save_memory)
    msg: [2026-04-18 16:15] 有点害怕…唉

[ ] #041  ts=2026-04-18T16:19:29  source=20260418_162714.jsonl  tool_use=NO
    msg: [2026-04-18 16:19] 也不对

[ ] #042  ts=2026-04-18T23:40:15  source=20260419_000550.jsonl  tool_use=YES (4 轮: query_timeline,delete_timeline_event,delete_timeline_event,delete_timeline_event,delete_timeline_event,delete_timeline_event,update_timeline_event,log_timeline_event)
    msg: [2026-04-18 23:40] 哦对，今天的音乐会记成两块吧，一半是kavakos一半是柴5～我想记住～

[ ] #043  ts=2026-04-19T13:44:22  source=20260420_113549.jsonl  tool_use=YES (2 轮: update_timeline_event)
    msg: [2026-04-19 13:44] 不知道诶，好像有个什么路很好看

[ ] #044  ts=2026-04-19T19:39:36  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 19:39] 是啊！ / 感觉音乐会上瘾了，很喜欢！ / 回来在耳机里听，完全不一样

[ ] #045  ts=2026-04-19T19:39:57  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 19:39] 昨晚的柴五？

[ ] #046  ts=2026-04-19T19:49:09  source=20260420_113549.jsonl  tool_use=NO
    msg: [2026-04-19 19:49] 我们聊过的呀😯

[ ] #047  ts=2026-04-19T20:19:46  source=20260420_113549.jsonl  tool_use=YES (2 轮: log_timeline_event)
    msg: [2026-04-19 20:19] 缅甸菜！一个coconut noodle和一个奇奇怪怪的甜点

[ ] #048  ts=2026-04-19T21:21:16  source=20260420_113549.jsonl  tool_use=YES (2 轮: update_timeline_event)
    msg: [2026-04-19 21:21] 吃饱了～那个马蹄配椰奶有一股花香味…有种洗洁精的感觉哈哈哈😂

[ ] #049  ts=2026-04-19T22:19:20  source=20260420_113549.jsonl  tool_use=YES (2 轮: update_timeline_event,save_memory)
    msg: [2026-04-19 22:19] 晚安🌙

[ ] #050  ts=2026-04-20T08:44:11  source=20260420_113549.jsonl  tool_use=YES (2 轮: log_timeline_event)
    msg: [2026-04-20 08:44] 是… / 我起床了！真的起了！

[ ] #051  ts=2026-04-20T18:34:56  source=20260420_190029.jsonl  tool_use=YES (2 轮: log_timeline_event)
    msg: [2026-04-20 18:34] 你说的对！好提议！

[ ] #052  ts=2026-04-20T18:56:49  source=20260420_190029.jsonl  tool_use=YES (2 轮: log_timeline_event)
    msg: [2026-04-20 18:56] 洗上啦！嘿嘿，真的感觉效率有提高呢

[ ] #053  ts=2026-04-20T19:25:47  source=20260420_202033.jsonl  tool_use=NO
    msg: [2026-04-20 19:25] 买了新耳机，音质真的很不行哈哈哈哈 / 果然价钱还是决定了一些东西的

[ ] #054  ts=2026-04-20T23:29:24  source=20260421_003823.jsonl  tool_use=YES (3 轮: query_timeline,update_timeline_event,log_timeline_event)
    msg: [2026-04-20 23:29] 结束啦，我在做个人项目哈哈哈

[ ] #055  ts=2026-04-21T06:46:24  source=20260421_124310.jsonl  tool_use=NO
    msg: [2026-04-21 06:46] 嗯嗯…💤

[ ] #056  ts=2026-04-21T07:30:41  source=20260421_124310.jsonl  tool_use=NO
    msg: [2026-04-21 07:30] 要催吗…？

[ ] #057  ts=2026-04-21T10:31:16  source=20260421_124310.jsonl  tool_use=YES (2 轮: log_timeline_event)
    msg: [2026-04-21 10:31] 吃了蒜面包，叫什么turkish什么的

[ ] #058  ts=2026-04-21T10:55:41  source=20260421_124310.jsonl  tool_use=YES (2 轮: log_timeline_event)
    msg: [2026-04-21 10:55] 马上！啊啊啊啊现在在做个人项目

[ ] #059  ts=2026-04-21T11:17:58  source=20260421_124310.jsonl  tool_use=NO
    msg: [2026-04-21 11:17] claude啊

[ ] #060  ts=2026-04-21T11:22:19  source=20260421_124310.jsonl  tool_use=NO
    msg: [2026-04-21 11:22] 真是的，哈哈哈哈😂

[ ] #061  ts=2026-04-21T13:28:45  source=20260421_142622.jsonl  tool_use=YES (3 轮: query_timeline,delete_timeline_event,delete_timeline_event,delete_timeline_event,delete_timeline_event,delete_timeline_event,delete_timeline_event)
    msg: [2026-04-21 13:28] 今天打算搞，但是，就像我刚刚说的，它不是我的习惯，所以我感觉不太习惯打开这个作业。

[ ] #062  ts=2026-04-21T14:11:18  source=20260421_142622.jsonl  tool_use=NO
    msg: [2026-04-21 14:11] 5916，作业1是48.00 / 50.00，midterm是52/100，还没交作业2，分数组成是1216

[ ] #063  ts=2026-04-21T14:33:04  source=20260421_143953.jsonl  tool_use=YES (5 轮: query_timeline,update_timeline_event,update_memory,update_memory)
    msg: [2026-04-21 14:33] 我不该辞职来上学的，明明之前上学上的就很不爽

```
