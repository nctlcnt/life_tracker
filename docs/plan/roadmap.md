# LifeMemo / life_tracker 路线图

> 最后更新：2026-10-19。记录方向、已经定下来的设计和进度。
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
- **2026-10-06 之后生产上已经没有卡片功能**：那天部署的是 memo 标签分支（`5cd7ac2`），
  它从 PR #24 分出来，不包含 PR #25 和 #27。生产库里最后一张卡片创建于 10-06，
  从 10-10 起所有消息的 `card_id` 都是空的。

### 卡片名、末尾引用、`/conversation`（PR #27，已合并到 main `dde10b4`）

> 2026-10-19：这一节里的末尾引用和 `/conversation` 已经删除，卡片名前缀保留。
> 原因见下面"消息卡片"一节：Discord 主线不再按卡片划分，也不再靠 reply 切换卡片。

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

### 结构化 memo 标签（`5cd7ac2`）

- 提交 `5cd7ac2`「Add structured memo tags and tag API」，2026-10-06 部署到生产。
- 这个提交一直没有合并进 main。2026-10-19 在本地分支 `chore/merge-memo-tags` 合并（`464fe6f`，
  未推送），冲突只有 `bot/database.py` 一处，两边都在 `Database` 类末尾追加了方法，两边都保留。

### 卡片改为 thread：第 1 步（本地分支 `feat/card-threads`，`3d940cc`，未推送）

- 删除 user 卡片、`current_card_id`、按 reply 切换卡片、`/conversation`、末尾引用，
  以及 `/api/cards` 返回里的 `is_current` / `current_card_id`。
- 新增 `CardService.enter_card(card_id)`：用户在卡片里说话之前调用，pending 卡片转为 active。
- 这一步单独部署会让 check-in 一直停在 pending，必须和第 2 步一起上线。

### 卡片改为 thread：第 2、3 步（同一分支）

- `conversation_cards.discord_thread_id`；主动消息第一次发送时开 thread，把用户加进去。
- 队列里的 `channel_id` 一律是主频道，实际发到哪个 thread 由 `LifeTrackerBot._card_target` 在发送时根据卡片决定。
- thread 里的消息写进历史时 `channel_id` 写主频道；check-in 批次的输入里多了 `card_source_id`，工具结果据此找到卡片。
- 测试：新文件 `tests/test_card_threads.py`（用替身模拟 Discord 的频道和 thread）。
- 还没有在真实的 Discord 上跑过，staging 验证留到第 4 步之后一起做。

---

## 设计记录

### 消息卡片

> 2026-10-19 改版。旧规则是「每条消息都属于某张卡片，reply 某条消息就切换到它所在的卡片」，
> 已经放弃。

**目的**：很多 check-in 没有回复，以前全都写进对话历史，memory 和 reflection 读到的大多是 AI 的单方面发言。

**规则**

- **Discord 主线是一整条线**：直接在频道里聊天的消息不属于任何卡片（`card_id` 为空），
  照常进入对话历史和记忆。不再有「当前卡片」，也不靠 reply 切换卡片。
- **卡片只放 AI 主动发的消息**：check-in、reminder、DDL 提醒、curator 通知、系统告警，每一次发送都是一张卡片。
- **卡片 = thread**：在 App 的卡片界面做出来之前，先用 Discord thread 显示卡片。之后 App 读取的是同一套卡片数据。
- 回复之前是 pending，消息只存在 `card_pending_messages`，**不进入**对话历史。
- 在 thread 里说话时，卡片转为 active，AI 的开场白和你的回复一起写进对话历史，并带上 `card_id` 记录来源。
- AI 的回答跟着触发它的消息走：触发消息在卡片里就进这张卡片，在主线上就留在主线。
- 过期时间 3 天，有新消息时往后推：
  - active 卡片到期只在界面上隐去，内容保留在历史里。App 里做成渐隐效果。
  - pending 卡片：已经开了 thread 的，到期后保留卡片这一行，因为 thread 在 Discord 里还在，
    几天后在里面回复仍然要能找到卡片；没有 thread 的（旧数据、AI 选择不说话的空卡片）到期直接删除。
- 没回复的空档不需要保留在对话里，"发过什么"在 `outbound_deliveries` 里有记录。

**Discord thread 的做法**

- 卡片第一次发消息时创建 thread，不带起始消息，主线里只会出现 Discord 的「开启了 thread」系统提示。
- thread 名：来源的显示名加上时间，例如 `Morning check-in · 10/19 08:43`。check-in 用 `check_ins.label`。
- 自动归档时间设为 3 天（Discord 的 4320 分钟档），和卡片过期时间一致。归档的 thread 里发消息会自动恢复。
- 创建后把用户加进 thread，否则收不到通知。
- `conversation_cards` 增加 `discord_thread_id` 列。
- thread 里的消息写进历史时，`channel_id` 仍然写主频道的 id。所以上下文窗口、compact、工具轨的游标都不需要知道 thread 的存在。
- `outbound_deliveries.channel_id` 也保持主频道。实际发到哪个 Discord 频道，在发送时根据卡片决定：
  主动消息 → 它自己的卡片的 thread；回复类消息和表情回应 → 触发消息所在卡片的 thread；不属于卡片 → 主频道。
- check-in 工具批次的结果（例如工具执行失败的提示）发到这次 check-in 的 thread 里。卡片还是 pending 时，它和开场白一样先存在卡片里。
- 不是由卡片开出来的 thread（例如手动建的）机器人不处理，和以前一样。发送队列按对话所属的频道排队，
  如果把这种 thread 当作主线，回复就只能发回主频道，在 thread 里说话、回复却出现在主频道会很奇怪。
- 没有建 thread 的权限时，消息仍然发在主频道，这张卡片就没有 thread。消息必须送达。

**发给 AI 的上下文**

- 整体按消息进入历史的顺序（id）线性排列，主线和卡片里的消息混在一起，卡片里的消息带卡片名，例如 `[check_in3 · 2026-09-27 09:00]`。
- 在 thread 里聊天时，额外告诉 AI 现在是在哪张卡片里说话。
- 场景状态（`scene_state`）按卡片分开：采访类 check-in 写入的场景只在它自己的 thread 里生效，不影响主线聊天，也不会被其他 check-in 清掉。

**compact**

- 默认每天一次，只压缩昨天之前的消息。"一天"按凌晨 4 点切分，和 memo、reflection 一致。
- compact prompt 里写明：DDL、天气、提醒类对话只保留你回复中的事实，过期信息可以丢掉。

**卡片总结**

- 每张卡片用小模型生成一句话总结，存在 `summary` 字段，给卡片列表（`/api/cards`）用。thread 里有新的用户消息后在后台更新。

**以后接 App**

- App 的卡片界面读取同一套卡片数据。到那时再加一条投递到 App 的通道和推送；Discord thread 是否保留，到时再定。

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

- **标签**：已有 memo 正文里的 `#xxx` 怎么迁移到标签表。（父标签计数、删除父标签的语义、斜杠指令怎么处理标签，都已经定下来，见上面"标签"一节。）
- **上周 reflection**：一周的起止时间（暂定上周一 4:00 到这周一 4:00）。

---

## 接下来

1. 卡片改为 thread（分支 `feat/card-threads`，第 2–4 步必须和第 1 步一起上线）：
   - 第 2、3 步（已完成，见上面"已完成"）：卡片对应 Discord thread；回复、工具结果、表情回应发回对应的 thread。
   - 第 4 步：thread 里聊天时标明当前卡片；场景状态按卡片分开。
   - 上线前在 staging 确认机器人有创建公开 thread、在 thread 里发消息的权限。
2. 更新外层 LifeMemo 的 submodule 指针（在本仓库之外，还没有确认是否已经做过）。
3. compact 改为每天凌晨 4 点、compact prompt 补上过期信息的取舍规则。（暂缓，先不动 compact。）
4. App 的聊天卡片界面。对应的读取 API 已经做完，见上面那一节。
5. 标签（不计时）。后端的标签 API 见 `5cd7ac2`。
6. 上周 reflection。
7. 计时标签。
8. 昨日 reflection。