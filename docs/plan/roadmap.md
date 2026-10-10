# LifeMemo / life_tracker 路线图

> 最后更新：2026-10-10。记录方向、已经定下来的设计和进度。
> 具体实现细节看各自的 plan 文件（`backend-memo-plan.md` 等）。

## 优先级

1. **标签**：最重要的是不计时的标签，计时标签在它的基础上做
2. **上周 reflection**：上周的总结在这一整周都能看到
3. **计时标签**
4. **昨日 reflection**

插在前面先做的：**消息卡片**。App 里的聊天、之后的 reflection 和 memory 都依赖"哪些消息算进对话历史"这条规则，先定下来，后面就不用回头清理数据。

2026-10-10 调整：App 里的聊天先不做，陪聊继续留在 Discord（全平台可用，App 签名会过期）。
check-in 改成 Discord thread，见下面「check-in thread」。

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

### 卡片名、末尾引用、`/conversation`（分支 `feat/card-context-and-conversation`，没有单独合并）

> 这个分支没有单独开 PR，已经包含在下面 check-in thread 的分支里一起合并。
> 其中「当前卡片」相关的部分（`/conversation`、`is_current`、`current_card_id`）在 thread 方案里删掉了，
> 卡片名、末尾引用、卡片总结和读取 API 保留。

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
- 读取 API（给 App 的卡片界面用）：
  - `GET /api/cards` 列表，`GET /api/cards/{id}` 单张，`GET /api/cards/{id}/messages` 卡片里的消息。
  - 只读，不含新建和切换。Discord 侧的 `/conversation` 已经覆盖那两件事。
  - 只返回被回复过（active）的卡片。没被回复过的 pending 卡片不在里面：它们还没进对话历史，
    过期就整张删掉，要不要在界面上露出来属于下面"待定问题"里还没定的那条。
  - 过期隐去的卡片照样返回，带 `faded` 标记和 `expires_at`，渐隐效果由 App 自己做。
  - 消息返回的是原文，不是给模型的那份表示（那一份带 `[时间]` 前缀，用户消息还会被换成
    `current_content`，里面含 `[回复 …]` 引用块）。界面和模型两种表示不能混用。
  - 不接受 channel 参数：`api/server.py` 其它接口都是隐式单用户的，频道取 `config.CHANNEL_ID`，
    没配置就不限频道。
- 测试：`tests/test_context_window.py` 新增 10 个、`tests/test_cards.py` 新增 6 个，
  新文件 `tests/test_card_summary.py` 9 个、`tests/test_cards_api.py` 10 个；全量 615 项通过。
- 后端到此为止，App 侧的界面单独做。

### check-in thread（分支 `feat/checkin-threads`，待合并）

- check-in 照常发在主频道，bot 随即从这条消息上开一个 thread（名字是 check-in 的第一句话），
  自动归档 1 天。没人回的 check-in 第二天就收起来。
- 在 thread 里说话，卡片转为 active，thread 的自动归档改成 3 天，和卡片过期时间一致。
- 在主频道 reply 一条 check-in，bot 去它的 thread 里回应。
- 同一次 check-in 的后续消息（AI 连发几条、超过 2000 字的分段）都发进 thread。
- 卡片第一次生成总结时，用总结替换 thread 名字。只改一次，因为 Discord 对改名限流很紧（10 分钟 2 次）。
  她自己开的 thread 不改名。
- 主频道直接发消息 = 普通陪聊，不再归属任何卡片。「当前卡片」的概念整个删掉，`/conversation` 指令也删掉了。
- reminder、`system_alert`、curator 通知不开 thread：没回复就不进历史，在主频道 reply 了才写进主对话。
- 她在一个找不到卡片的 thread 里说话（pending 卡片已经过期删掉，或者是她自己从别的消息上开的 thread）：
  新开一张 `thread` 类型的卡片，并从 Discord 取回 thread 的起始消息补进历史。
- **存储**：thread 里的消息在 DB 里仍然记在主频道（`channel_id` = 主频道）下，只靠 `card_id` 区分，
  真实的 thread id 记在 metadata 的 `discord_channel_id`，`conversation_cards` 新增 `thread_id` 列。
  这样 compact、工具批次、长期记忆（curator）和语义检索这些按频道工作的流程不用改，thread 里聊到的事情照样会被处理。
- **上下文**：
  - thread 里：频道的 compact 摘要 + 这张卡片自己的全部消息。主频道的聊天原文不进来。
  - 主频道：和以前一样，按 id 线性排列，thread 里的对话也在里面，前面带卡片名（例如 `[check_in3 · …]`），所以陪聊能看到 check-in 里聊过的内容。
- 读取 API：卡片多了 `thread_id` 字段，去掉了 `is_current` 和列表里的 `current_card_id`。
- 部署前要确认 bot 在频道里有 **Create Public Threads** 和 **Send Messages in Threads** 权限。
  没有权限时 check-in 留在主频道（只记日志），在主频道 reply 它照样能聊，只是不会进 thread。

---

## 设计记录

### 消息卡片

**目的**：很多 check-in 没有回复，以前全都写进对话历史，memory 和 reflection 读到的大多是 AI 的单方面发言。

**规则**

- AI 主动发的每条消息（check-in、poll、reminder、系统通知）都是一张卡片。回复之前是 pending，**不进入** `conversation_messages`。
- check-in 发出后开一个 Discord thread，卡片的对话都在 thread 里。thread 里第一次有人说话，AI 的开场白和这句话一起写进对话历史。
- 在主频道 reply 某张卡片的消息，同样会让它进入历史；卡片有 thread 时，bot 在 thread 里回应。
- 主频道直接发消息（不 reply）→ 普通陪聊，不归属任何卡片。
- 过期时间 3 天：
  - pending 卡片到期直接删除。
  - 回复过的卡片到期只在界面上隐去，内容保留在历史里。
  - 有新消息时，过期时间往后推。App 里做成渐隐效果。
- 没回复的空档不需要保留在对话里，"发过什么"在 `outbound_deliveries` 里有记录。

**发给 AI 的上下文**

- 主频道：整体按消息进入历史的顺序（id）线性排列，每条带卡片名和时间，例如 `[check_in3 · 2026-10-10 09:00]`。
  thread 里的对话也在其中，所以陪聊是「全知」的。
- thread：频道的 compact 摘要 + 这张卡片自己的消息，只拿已经整理过的上下文，不带主频道原文。
- 末尾引用：主频道 reply 一条没有 thread 的卡片消息（比如提醒）时，说明在回复哪张卡片的哪一句。

**以后的「全知陪聊」**

- 现在主频道上下文直接包含 thread 原文。如果以后 thread 多到挤占上下文，可以换成分层：
  常驻一段「最近的卡片」（每张一句总结）、需要细节时用工具读卡片原文、更久的靠记忆。

**compact**

- 默认每天一次，只压缩昨天之前的消息。"一天"按凌晨 4 点切分，和 memo、reflection 一致。
- compact prompt 里写明：DDL、天气、提醒类对话只保留你回复中的事实，过期信息可以丢掉。

**Discord 和 App 的分工**

- Discord：陪聊、check-in thread、快速记录。全平台可用，不受 App 签名过期影响。
- App：认真写的 memo（地点、图片、标签）、回顾、reflection。两边共用同一个后端和数据。
- App 里的聊天界面暂时不做。想做的时候，后端还缺一条 App 的写入路径（发消息、AI 回复怎么回到 App）。

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

- **reminder 送达要不要留在历史里**：现在 reminder、`system_alert`、curator 通知都是 pending 卡片，没回复就不进历史。
- **标签**：已有 memo 正文里的 `#xxx` 怎么迁移到标签表。（父标签计数、删除父标签的语义、斜杠指令怎么处理标签，都已经定下来，见上面"标签"一节。）
- **上周 reflection**：一周的起止时间（暂定上周一 4:00 到这周一 4:00）。

---

## 接下来

1. check-in thread 在 staging 上试用，确认 bot 的 thread 权限，然后合并。
2. 更新外层 LifeMemo 的 submodule 指针（2026-10-10 确认还没做，本地还停在 `2a44bbf`）。
3. compact 改为每天凌晨 4 点、compact prompt 补上过期信息的取舍规则。（暂缓，先不动 compact。）
4. 标签（不计时）。
5. 上周 reflection。
6. 计时标签。
7. 昨日 reflection。