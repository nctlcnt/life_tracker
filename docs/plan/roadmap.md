# LifeMemo / life_tracker 路线图

> 最后更新：2026-09-28。记录方向、已经定下来的设计和进度。
> 具体实现细节看各自的 plan 文件（`backend-memo-plan.md` 等）。

## 优先级

1. **标签**：最重要的是不计时的标签，计时标签在它的基础上做
2. **上周 reflection**：上周的总结在这一整周都能看到
3. **计时标签**
4. **昨日 reflection**

插在前面先做的：**消息卡片**。App 里的聊天、之后的 reflection 和 memory 都依赖"哪些消息算进对话历史"这条规则，先定下来，后面就不用回头清理数据。

---

## 已完成

### 斜杠指令清理 + 静音时段（PR #24，已合并到 main）

- 删除 `/todo`、`/model`、`/fallback`、`/poll`。剩下 `/calendar`、`/weather`、`/tz`。preset 和 check-in 开关都在 admin 页面。
- 静音时段默认 00:00–06:00：
  - 只作用于 `after_ai_call` 类型的轮询（`random_poll`、`ttl_followup` 等）。时段内不调用 AI，所以不会为了一个 `[SILENT]` 消耗 token。
  - reminder / DDL 提醒必须送达，不受影响。早安、睡前这类 `window` check-in 的时段是手动设定的，也不受影响。
  - 以后在 App / admin 修改：写 `app_state.quiet_hours`，格式 `"00:00-06:00"`，`"off"` 表示关闭。

### 消息卡片后端第一版（PR #25，已合并到 main `92a989e`）

- 新表 `conversation_cards`、`card_pending_messages`，`conversation_messages` 加 `card_id` 列。
- 规则集中在 `bot/cards.py`，见下面"消息卡片"一节。
- 过期卡片的清理由 `CardService.purge_expired` 在收发消息的路径上顺带完成，没有单独的定时任务。
- 测试：`tests/test_cards.py`，共 16 个。
- 2026-09-28 在 staging 测试通过，合并之后已经部署到生产。

### 卡片名、末尾引用、`/conversation`（分支 `feat/card-context-and-conversation`，待合并）

- 上下文里每条消息的时间前缀扩展成 `[卡片名 · 时间]`，卡片名是「来源类型 + 同类型内的序号」，
  例如 `check_in3`。序号只在 active 卡片里排，因为 pending 卡片会被过期清理删掉，
  把它们算进去会让同一张卡片的名字来回漂移。没有卡片归属的旧消息前缀保持原样。
- 末尾引用分三种情况：被回复的消息已经折进摘要时，连同卡片名多引用几条原话；
  明文里还看得见、而且 `discord_bot` 已经内联过引用片段时，只补一个卡片名，
  不重复引用同一段话；内联引用缺失时（抓取 Discord 原消息失败也会走到这里），
  补上卡片名和一小段原话。
- `/conversation new` / `list` / `switch`，`switch` 的 autocomplete 读预先生成好的总结。
- 卡片总结在 `bot/card_summary.py`：用户消息写入之后在后台生成，同一张卡片不并发调用，
  生成期间又来消息就在结束后补跑一次。失败不影响聊天。用的是 compact 那份 preset，
  没有新增配置项。
- 触发点目前只有 `discord_bot.on_message`，因为 `card_for_user_message` 现在只有它在调。
  以后 App 走自己的写入路径时，需要在那条路径上补同样的触发。
- `switch` 会把卡片的过期时间往后推 3 天。roadmap 原文只写了「有新消息时往后推」，
  切换算不算「新消息」没有写，这里按「算」处理。
- 测试：`tests/test_context_window.py` 新增 10 个、`tests/test_cards.py` 新增 6 个、
  新文件 `tests/test_card_summary.py` 9 个；全量 605 项通过。
- 后端到此为止。App 侧的卡片界面接下来单独做，它需要的读取 API 还没有写。

---

## 设计记录

### 消息卡片

**目的**：很多 check-in 没有回复，以前全都写进对话历史，memory 和 reflection 读到的大多是 AI 的单方面发言。

**规则**

- AI 主动发的每条消息（check-in、poll、reminder、系统通知）都是一张卡片。回复之前是 pending，**不进入** `conversation_messages`。
- 回复某张卡片时，AI 的开场白和你的回复一起写进对话历史，这张卡片成为当前卡片。
- 直接发消息（不 reply）→ 进入当前卡片。没有当前卡片、或当前卡片已经隐去时，自动开一张新卡片。
- 新的 check-in 到来不会切换当前卡片，只有 reply 它才会切换。
- 过期时间 3 天：
  - pending 卡片到期直接删除。
  - 回复过的卡片到期只在界面上隐去，内容保留在历史里。
  - 有新消息时，过期时间往后推。App 里做成渐隐效果。
- 没回复的空档不需要保留在对话里，"发过什么"在 `outbound_deliveries` 里有记录。

**发给 AI 的上下文**

- 整体按消息进入历史的顺序（id）线性排列，每条带卡片名和时间，例如 `[poll3 · 9/27 09:00]`。
- 末尾引用：说明这条消息在回复哪张卡片的哪一句。被引用的消息保留在原位置，末尾只引用一小段。
- 如果被回复的卡片早于 compact 分界线，末尾多引用几轮。
- 例子：checkin1、2、4 已回复，checkin3 没回复，在 poll3 里说话 → AI 看到 checkin1、2、poll3、4 按时间排列，末尾是"回复 poll3 的某一句"和新消息。checkin3 不出现。

**compact**

- 默认每天一次，只压缩昨天之前的消息。"一天"按凌晨 4 点切分，和 memo、reflection 一致。
- compact prompt 里写明：DDL、天气、提醒类对话只保留你回复中的事实，过期信息可以丢掉。

**Discord 作为代用通道**

- 先保留 Discord 入口，等 App 聊天和多端支持成熟后逐步淘汰。
- 在 Discord 里规则相同：reply 某条消息 = 开启 / 切换卡片，之后的消息都进入这张卡片。
- `/conversation` 指令：`new`（开新卡片）、`list`（看活跃卡片）、`switch`（切换卡片）。
- 每张卡片用小模型生成一句话总结。因为 Discord autocomplete 要求 3 秒内返回，总结要在卡片有新对话后在后台更新、存进 `summary` 字段，不能在打开列表时现场生成。

### 标签

- **不从正文解析**，改成结构化数据。输入框下面有一条标签栏，可以选择已有标签或新建。
- 新建时可以设置颜色、是否计时、父标签。
- 最多两层，例如 `娱乐/影视`。
- 标签**只作用于手动记录的 memo**。斜杠指令生成的 memo 暂时不处理标签。
- 之后把标签列表传给工具轨的 AI，让它像现在处理 project、routine 那样给 memo 打标签。
- 显示每个标签下有多少篇 memo，计数包含子标签。
- 删除父标签时，子标签跟着一起删除。
- 计时标签：记录起始日期，在 memo 上显示为 `tagname | N天`，天数由 `today - started_at` 算出，不存储。
- 数据结构（草案）：
  - `tags`：`id`、`client_id`、`name`、`parent_id`、`color`、`is_timed`、`started_at`、`updated_at`、`deleted_at`
  - `memo_tags`：`memo_id`、`tag_id`
  - 标签也会离线创建，所以和 memo 一样用 `client_id` 加增量同步。

### 上周 reflection

- 数据来源：**memo + timeline events**。暂时不用聊天记录，等消息卡片上线、聊天数据干净之后再考虑。
- 上周的总结在这一整周都固定显示在显眼的位置。内容：记了什么、做了什么、表达了什么，以及这周想改变什么。
- 形式是一段简短的文字（类似 muute 的 weekly insight）。
- 可以编辑。
- 存储：`reflections` 表，每日和每周共用，用 `period` 字段区分 day / week。
- 生成：scheduler 在周一凌晨生成，并推送通知。

### 其他

- App 里 AI 写的 timeline 和自己写的记录要在界面上区分开。
- 推送功能保留。
- 天气作为分析输入、每周词云，还没定。

---

## 待定问题

- **哪些来源算"回复"**：现在只有 `chat`、`chat_error`、`chat_tool_feedback`、`tool_batch` 进入当前卡片，其余（包括 reminder、`system_alert`、curator 通知）都会变成 pending 卡片。reminder 送达这件事要不要留在历史里？
- **标签**：已有 memo 正文里的 `#xxx` 怎么迁移到标签表。（父标签计数、删除父标签的语义、斜杠指令怎么处理标签，都已经定下来，见上面"标签"一节。）
- **上周 reflection**：一周的起止时间（暂定上周一 4:00 到这周一 4:00）。

---

## 接下来

1. 更新外层 LifeMemo 的 submodule 指针（在本仓库之外，还没有确认是否已经做过）。
2. compact 改为每天凌晨 4 点、compact prompt 补上过期信息的取舍规则。（暂缓，先不动 compact。）
3. App 的聊天卡片界面和对应的 API。
4. 标签（不计时）。
5. 上周 reflection。
6. 计时标签。
7. 昨日 reflection。