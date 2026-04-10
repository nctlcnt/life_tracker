
## Implementation Plan：Reminder 改造

### 核心思路

把 `set_reminder` 从"一次性闹钟"改成"AI 预约的主动介入"。AI 在收到消息时自己规划后续跟进的时间表，scheduler 到点触发，AI 拿到上下文自行决定说什么。

---

### Step 1：改造 reminders 表

在 `database.py` 的建表逻辑里，把 reminders 表改成：

```sql
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_time TEXT NOT NULL,
    action TEXT NOT NULL,
    group_id TEXT,                          -- 新增：同一件事的多条 reminder 共享
    priority TEXT DEFAULT 'normal',         -- 新增：low / normal / high
    status TEXT DEFAULT 'pending',          -- pending / triggered / cancelled
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

**新增字段**：
- `group_id`：同一件事生成的多条 reminder 用相同的 group_id（如 `ddl_ds_0416`），方便批量取消
- `priority`：让 AI 标记重要程度，`high` 的即使用户最近刚聊过也要提
- `status`：加 `cancelled` 状态，支持批量取消

**迁移**：写一个 `ALTER TABLE` 给已有数据加上新字段，默认值填充即可。

---

### Step 2：新增 database.py 方法

```python
def cancel_reminders_by_group(self, group_id: str) -> int:
    """取消某个 group 下所有 pending 的 reminder，返回取消条数"""

def get_pending_reminders_by_group(self, group_id: str) -> list:
    """查询某个 group 下还剩多少 pending 的 reminder"""

def list_active_reminders(self) -> list:
    """列出所有 pending 的 reminder（给 AI 看/给用户查）"""
```

现有的 `get_due_reminders` 和 `mark_reminder_done` 保留，`mark_reminder_done` 改成更新 `status = 'triggered'`。

---

### Step 3：改造工具定义（tools.py）

**改 `set_reminder`**：

```python
{
    "name": "set_reminder",
    "description": 
        "预约一次未来的主动联系。到时间后你会被唤醒，"
        "拿到 action 作为上下文，自行决定对用户说什么。\n\n"
        "这不是闹钟通知，是你给自己安排的 follow-up 计划。\n\n"
        "使用场景：\n"
        "- 用户提到 deadline → 根据紧急程度安排多条，越临近越密集\n"
        "  例：后天考试 → 今晚1条 + 明天2条 + 后天早上1条\n"
        "- 用户说看两集就回来 → 1.5h 后设1条\n"
        "- 用户说要做某事（买猫粮/交作业）→ 当天晚上或明天设1条跟进\n"
        "- 同一件事的多条 reminder 用相同的 group_id\n\n"
        "⚠️ 收到 [提醒触发] 前缀消息时，那条 reminder 已经触发了，"
        "直接回应用户，绝对不要再 set_reminder 设相同内容！",
    "input_schema": {
        "type": "object",
        "properties": {
            "trigger_time": {
                "type": "string",
                "description": "触发时间，ISO 8601"
            },
            "action": {
                "type": "string",
                "description": "给未来的自己的备忘。不是发给用户的文案，"
                               "而是上下文提示，如'检查复习进度'、'问问剧看完没'"
            },
            "group_id": {
                "type": "string",
                "description": "同一件事的多条 reminder 共享的标识，"
                               "简洁有意义，如 'exam_0416'、'buy_cat_food'。"
                               "单条 reminder 可以不填。"
            },
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high"],
                "description": "low=随意跟进 normal=正常 high=重要deadline，即使刚聊过也要提"
            }
        },
        "required": ["trigger_time", "action"]
    }
}
```

**新增 `cancel_reminders`**：

```python
{
    "name": "cancel_reminders",
    "description": "取消某个 group 下所有未触发的 reminder。"
                   "用户说事情做完了/取消了/不需要了时调用。"
                   "如'考完了' → 取消 exam 相关的所有后续 reminder。",
    "input_schema": {
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "要取消的 reminder 组标识"
            }
        },
        "required": ["group_id"]
    }
}
```

**新增 `list_reminders`**：

```python
{
    "name": "list_reminders",
    "description": "查看当前所有未完成的 reminder。"
                   "当用户问'我还有什么安排/提醒'时调用。",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

---

### Step 4：ai_engine.py — 注册新工具的执行逻辑

在 `_execute_tool` 方法里：

- `set_reminder`：调用 db 写入，返回 `"已安排"`（AI 不会给用户看到这个返回值）
- `cancel_reminders`：调用 `db.cancel_reminders_by_group(group_id)`，返回取消条数
- `list_reminders`：调用 `db.list_active_reminders()`，返回格式化的列表

---

### Step 5：改造 scheduler.py — 触发逻辑

现在的 `_reminder_check_loop` 大概是：扫到期 → 调 `ai_engine.reminder_action` → 发消息。

改造点：

**触发时注入更丰富的上下文**：

```python
async def _trigger_reminder(self, reminder):
    # 查同 group 的信息
    group_info = ""
    if reminder['group_id']:
        remaining = db.get_pending_reminders_by_group(reminder['group_id'])
        total = db.count_reminders_in_group(reminder['group_id'])
        group_info = f"（这是关于此事的第{total - len(remaining) + 1}条提醒，共{total}条）"
    
    context = (
        f"[提醒触发] {reminder['action']}\n"
        f"优先级: {reminder['priority']}\n"
        f"{group_info}\n"
        f"上次用户发消息: {db.get_last_user_message_time()}"
    )
    
    response = await ai_engine.reminder_action(context)
    
    if response and response != "[SILENT]":
        await send_to_discord(response)
    
    db.mark_reminder_done(reminder['id'])
```

**AI 可以选择沉默**：如果 priority 是 low/normal 且用户 5 分钟前刚聊过，AI 可能判断不需要打扰，返回 `[SILENT]`。但 `high` 优先级的，prompt 里会告诉 AI 即使刚聊过也要提。

---

### Step 6：System Prompt 修改

把现在 prompt 里的提醒策略段落替换掉。改动集中在两处：

**1）提醒策略段落**（替换原来的简单版本）：

```
## 提醒策略

你的 set_reminder 不是给用户的闹钟，是你给自己安排的"之后要跟进这件事"。
到时间后 scheduler 会唤醒你，你拿到 action 上下文，自己决定说什么。

### 什么时候设 reminder
- 用户说看两集就回来 → 1.5h 后设一条
- 先去洗澡 → 30min 后一条
- 在刷手机/社交媒体 → 20min 后一条
- 用户说要做某事（买猫粮/交报告）→ 今晚或明天设一条跟进

### deadline 类：安排多条，越临近越密
例："后天周三考试"（现在周一下午）
→ 今晚一条：聊聊准备情况
→ 明天上午一条：提一嘴
→ 明天晚上一条：关心复习进度
→ 后天早上一条：考试当天鼓励
同一件事用相同 group_id，如 "exam_0416"。

### 优先级
- high：重要 deadline、考试、面试 → 即使刚聊过也要提
- normal：一般跟进
- low：随意聊聊的话题、无关紧要的事

### ⚠️ 禁止
- 收到 [提醒触发] 后绝对不要再 set_reminder 同样的事（会死循环）
- 用户说"做完了/考完了/不需要了" → 立即 cancel_reminders 该 group
```

**2）轮询段落**：加一句让 AI 在轮询时也能看到 pending reminders 的总览，这样 AI 在随机轮询时也知道"有什么事在排队"，避免和即将触发的 reminder 内容重复。

---

### Step 7：ai_engine.py — `_build_messages` 注入 watches 信息

在构建发给 Claude 的消息时，除了现有的 memories，额外注入 pending reminders 概览：

```python
def _build_reminder_context(self):
    reminders = db.list_active_reminders()
    if not reminders:
        return ""
    
    lines = ["【待触发的跟进计划】"]
    for r in reminders:
        time_str = r['trigger_time']
        lines.append(f"- [{r['priority']}] {time_str} | {r['action']} (group: {r.get('group_id', '无')})")
    return "\n".join(lines)
```

这段和 memories 一起拼进 prompt 的上下文部分。让 AI 在回复用户消息时也能看到"我之后安排了什么"，避免重复设置。

---

### 改动范围总结

| 文件 | 改动 |
|---|---|
| `database.py` | reminders 表加字段 + 3 个新方法 |
| `tools.py` | 改 set_reminder 定义 + 新增 cancel_reminders / list_reminders |
| `ai_engine.py` | `_execute_tool` 注册新工具 + `_build_messages` 注入 reminder 上下文 |
| `scheduler.py` | 触发逻辑加上下文注入 + 沉默判断 |
| `SYSTEM_PROMPT` | 替换提醒策略段落 |

### 不动的部分

- events 表、memories 表 — 不改
- log_timeline_event / update_timeline_event / query_timeline — 不改
- save_memory / delete_memory / update_memory — 不改
- Discord Bot 收发逻辑 — 不改
- FastAPI — 不改
- 随机轮询循环本身 — 保留，和 reminder 触发共存

### 建议实施顺序

1. 先改 `database.py`（表结构 + 新方法），跑一下 migration 确认没问题
2. 改 `tools.py`（工具定义），这步纯声明不影响运行
3. 改 `ai_engine.py`（注册执行逻辑 + 注入上下文）
4. 改 `scheduler.py`（触发逻辑）
5. 最后改 System Prompt，然后测试 AI 是否开始主动安排多条 reminder