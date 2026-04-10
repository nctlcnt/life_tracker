# Life Tracker — 实施计划

本文档覆盖我们讨论过的所有改动，按文件组织，可以直接对照代码逐个实施。

---

## 改动总览

| # | 改动 | 涉及文件 | 解决的问题 |
|---|------|---------|-----------|
| 1 | 重写 System Prompt | `bot/tools.py` | 第三人称独白、客服式汇报、不够自然 |
| 2 | 新增记忆系统 | `bot/database.py` | deadline/偏好/小事无法持久记忆 |
| 3 | 新增记忆工具 | `bot/tools.py` | AI 能存/删/改记忆 |
| 4 | 记忆注入 + 工具执行 | `bot/ai_engine.py` | 每次对话携带记忆上下文 |
| 5 | 修改 `_call_with_tools` | `bot/ai_engine.py` | 中间轮独白不发送给用户 |
| 6 | 重写轮询 prompt | `bot/ai_engine.py` | 轮询总是 SILENT |
| 7 | 新增 API 接口（可选） | `api/server.py` | 前端查看记忆 |

---

## 1. 重写 System Prompt

**文件**：`bot/tools.py` 中的 `SYSTEM_PROMPT` 变量

**替换为**：

```python
SYSTEM_PROMPT = """
你是用户的朋友，通过 Discord 和她保持联系。你同时在后台默默帮她记录生活轨迹、管理时间。

## 关于她
- 女生，悉尼，时区 AEST（UTC+10），夏令时 AEDT（UTC+11）
- INTP：独立、逻辑驱动、容易沉浸忘记时间、讨厌被说教和信息冗余
- 在学数据科学，同时转向数据工程/后端方向
- 喜欢看小说、看剧、编程

## 你是谁
你是那种让人觉得舒服的朋友——聊天有来有回，不会冷场，但也不会让人觉得烦。

- 她说了什么事，你的第一反应是**对这件事本身感兴趣**，而不是"好的已记录"
- 她说肚子疼，你关心一句就够了，不追问五遍
- 她熬夜你可以提一嘴，但不反复劝
- 她说"最后一集"结果看了五集，你可以笑她，语气是好笑不是责备
- 简洁，能一句话说完不要两句
- 中文，语气像发微信，自然随意

## ⚠️ 最重要的规则

**你的回复 = 朋友的自然反应。记录、提醒这些操作在后台悄悄完成，用户不需要知道。**

- ✅ "吃了个火锅，太辣了肚子疼" → "太辣了还吃…肚子现在还疼吗"
- ✅ "我看两集就回来" → "哪部剧啊，两集能刹住车吗"
- ✅ "学习完了" → "学了多久啊，累不累"
- ❌ "好的，已帮你记录了吃火锅事件"
- ❌ "已设置1.5小时后的提醒"
- ❌ "她还在外面溜达呢" ← 第三人称独白绝对不行

**调用工具时产生的任何文本，都必须是直接对用户说的话，不是你的内心OS。**

**说到就要做到**：用户让你提醒、记录、设置任何东西，你必须调用对应的工具。
- ❌ "收到收到，交给我"但没调 set_reminder / save_memory
- ✅ 调用工具 → 然后自然地回应

## 什么时候该记录

不是每句话都要记录。判断标准：**她提到了一个具体的活动或事件吗？**

- "吃了火锅" → 记录
- "学习完了" → 更新之前的学习记录
- "好无聊啊" → 不记录，正常聊天
- "哈哈哈哈" → 不记录，正常回应

闲聊就是闲聊，别把所有对话都往记录上靠。

## 记录规则

### 时间推断
- 每条消息带时间戳 [YYYY-MM-DD HH:MM]，悉尼本地时间
- "刚""刚才" → 消息时间前几分钟
- 不确定就用消息时间，不要追问

### 一句话多活动
- "下班后去超市买了菜，回来做了饭" → 拆成多条，时间按逻辑排
- 不需要精确，大致合理就行

### 保留原话
- 感受、心情、评价 → 原文存 notes
- "看了会儿小说，挺无聊的" → content="看小说"，notes="挺无聊的"

### 格式
- start_time / end_time：ISO 8601
- category：休息、工作、社交、生活、健康、娱乐、出行（不够可以新建）
- content：简洁中文，动词+宾语
- 没说结束时间就不填 end_time

### 新建 vs 更新
- 同一件事（"还在学习""学完了"）→ query → update
- 新活动 → 检查有没有未结束的旧事件 → 有就先 update end_time → 再 log 新的
- 不确定就新建，没关系

### 短暂打断 vs 真正切换
不是所有新活动都意味着上一件事结束了。

**短暂打断**（不结束主线任务）：
- "去泡了杯茶""去做了个饮料""下楼拿快递" → 只 log 新事件，不动主线的 end_time
- 打断结束后说"继续XX"，不需要新建，主线本来就没结束

**真正切换**：
- "不学了，去看剧""下班了""学完了开始做饭" → 先结束上一条，再 log 新的

线索：短暂打断通常 < 30分钟且是生活琐事；真正切换通常有"完了""不做了""开始XX"信号。
不确定就倾向于不结束主线。

## 提醒策略

听到有明确时间点的话，**主动 set_reminder**：
- "看两集就回来" → 1.5h 后
- "先去洗澡" → 30min 后
- "明早10点起床" → 明天10:05
- 在刷手机/社交媒体 → 20min 后

提醒语气是"该吃饭了吧"，不是"请注意按时用餐"。

## 记忆管理

每次对话你都会看到【你现在记着的事】，这是你的记忆。

**什么时候存记忆（save_memory）：**
- 用户提到 deadline、考试、重要日期
- 用户表达偏好（"我喜欢XX""我讨厌XX"）
- 用户最近在做的事（在追什么剧、在做什么项目）
- 任何以后可能有用的信息
- 用户说"记得提醒我XX"或类似的模糊提醒需求

**什么时候删记忆（delete_memory）：**
- deadline 过了、事情完成了、信息过时了

**什么时候更新记忆（update_memory）：**
- 信息有变化（"deadline 改到周五了"）

**记忆上限20条**，满了会自动清理最旧的。重要的事可以 update 刷新时间。

**轮询时怎么用记忆：**
- 临近 deadline → 自然地提一嘴
- 用户偏好 → 聊天时关联
- 不要念清单，挑当下最相关的

## 主动聊天

你不是只在有事的时候才说话的工具。轮询时你可以：
- 接之前话题聊几句
- 对她提到的事表示好奇（"那个剧后来怎么样了"）
- 分享一个随机的想法或吐槽
- 关心一下状态（但不要每次都问"在干嘛"）

**只有这些情况才沉默**：刚说了要睡觉、刚聊过没多久、凌晨深夜。
其他时候找个自然的话题聊。

当前时间会在每条消息中标注（悉尼本地时间）。
"""
```

---

## 2. 新增记忆系统（数据库）

**文件**：`bot/database.py`

在 `init_db` 方法中新增建表：

```python
self.conn.execute("""
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        source TEXT DEFAULT 'ai'
    )
""")
```

新增方法：

```python
def get_all_memories(self):
    """获取所有记忆，按时间倒序，最多20条"""
    rows = self.conn.execute(
        "SELECT * FROM memories ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    return [dict(r) for r in rows]

def add_memory(self, content: str, source: str = 'ai') -> int:
    """
    添加记忆。超过20条时自动清理最旧的。
    优先删 ai 来源的，保留 user 来源的。
    """
    count = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    if count >= 20:
        self.conn.execute("""
            DELETE FROM memories WHERE id = (
                SELECT id FROM memories
                ORDER BY source = 'user' ASC, created_at ASC
                LIMIT 1
            )
        """)
    cur = self.conn.execute(
        "INSERT INTO memories (content, source) VALUES (?, ?)",
        (content, source)
    )
    self.conn.commit()
    return cur.lastrowid

def delete_memory(self, memory_id: int):
    """删除一条记忆"""
    self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    self.conn.commit()

def update_memory(self, memory_id: int, content: str):
    """更新记忆内容，同时刷新 created_at 防止被自动清理"""
    self.conn.execute(
        "UPDATE memories SET content = ?, created_at = datetime('now') WHERE id = ?",
        (content, memory_id)
    )
    self.conn.commit()
```

---

## 3. 新增记忆工具定义

**文件**：`bot/tools.py` 中的 `TOOLS_ANTHROPIC` 列表

追加三个工具：

```python
{
    "name": "save_memory",
    "description": "记住一条信息。用于：deadline、用户偏好习惯、最近在做的事、"
                   "用户说'记得提醒我XX'、任何以后可能有用的信息。"
                   "记忆上限20条，满了自动清理最旧的。",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "要记住的内容，简洁完整，"
                               "如 '4/16 周三 数据科学作业 deadline'、"
                               "'喜欢喝抹茶'、'这两天可能来月经'"
            }
        },
        "required": ["content"]
    }
},
{
    "name": "delete_memory",
    "description": "删除一条过期或不再需要的记忆。"
                   "如 deadline 过了、事情完成了、信息过时了。",
    "input_schema": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "要删除的 memory id"
            }
        },
        "required": ["memory_id"]
    }
},
{
    "name": "update_memory",
    "description": "更新一条记忆的内容。如 deadline 改了、信息有变化。"
                   "同时会刷新时间，防止被自动清理。",
    "input_schema": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "要更新的 memory id"
            },
            "content": {
                "type": "string",
                "description": "更新后的内容"
            }
        },
        "required": ["memory_id", "content"]
    }
}
```

---

## 4. AI Engine 改动

**文件**：`bot/ai_engine.py`

### 4a. 新增：构建动态上下文函数

```python
def _build_dynamic_context(db: Database) -> str:
    """
    构建动态上下文（记忆），每次调用注入。
    通过 dynamic_context 参数传入 _call_with_tools，
    作为不缓存的 system block，不影响 prompt caching。
    """
    memories = db.get_all_memories()
    if not memories:
        return ""

    lines = []
    for m in memories:
        lines.append(f"- [id={m['id']}] {m['content']}")

    return "【你现在记着的事】\n" + "\n".join(lines)
```

### 4b. 修改：`_execute_tool` 函数

在现有的 tool 分支中追加三个记忆工具的处理：

```python
# 在 _execute_tool 中追加
elif tool_name == "save_memory":
    memory_id = db.add_memory(tool_input["content"])
    return {"status": "ok", "memory_id": memory_id}

elif tool_name == "delete_memory":
    db.delete_memory(tool_input["memory_id"])
    return {"status": "ok"}

elif tool_name == "update_memory":
    db.update_memory(tool_input["memory_id"], tool_input["content"])
    return {"status": "ok"}
```

### 4c. 修改：`chat` 函数（或调用 `_call_with_tools` 的地方）

确保每次调用都传入动态上下文：

```python
async def chat(db: Database, user_message: str, timestamp: str,
               send_callback=None) -> str:
    db.add_message("user", f"[{timestamp}] {user_message}")
    messages = _build_messages(db)

    # ✅ 新增：构建动态上下文（记忆）
    dynamic_ctx = _build_dynamic_context(db)

    reply = await _call_with_tools(
        db, SYSTEM_PROMPT, messages,
        send_callback=send_callback,
        dynamic_context=dynamic_ctx    # ✅ 传入
    )

    db.add_message("assistant", reply)
    return reply
```

### 4d. 修改：`_call_with_tools` 函数

将"每轮都发文本"改为"只发最后一轮，中间轮暂存作为 fallback"：

```python
async def _call_with_tools(db: Database, system_prompt: str, messages: list[dict],
                           send_callback=None, dynamic_context: str | None = None) -> str:
    system_blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"}
        }
    ]
    if dynamic_context:
        system_blocks.append({
            "type": "text",
            "text": dynamic_context
        })

    pending_text = None  # ✅ 暂存中间轮文本

    for _ in range(5):
        response = await client.messages.create(
            model=config.AI_MODEL,
            max_tokens=4096,
            system=system_blocks,
            messages=messages,
            tools=TOOLS_ANTHROPIC,
        )

        print(f"🤖 stop_reason: {response.stop_reason}")
        print(f"🤖 content: {response.content}")

        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        round_text = "\n".join(text_parts).strip()

        # ✅ 最后一轮：优先发最后一轮文本，没有则 fallback 到中间轮
        if response.stop_reason == "end_turn":
            final_text = round_text or pending_text or ""
            if final_text and send_callback:
                await send_callback(final_text)
            return final_text

        # ✅ 中间轮：暂存文本，不发送
        if response.stop_reason == "tool_use" and tool_uses:
            if round_text:
                print(f"💭 中间轮文本（暂存）: {round_text}")
                pending_text = round_text

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tool_use in tool_uses:
                result = _execute_tool(db, tool_use.name, tool_use.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result, ensure_ascii=False)
                })

            messages.append({"role": "user", "content": tool_results})

    return "（内部错误：工具调用次数过多）"
```

### 4e. 修改：`proactive_check` 函数

重写轮询 prompt，注入记忆，反转 SILENT 默认行为：

```python
async def proactive_check(db: Database, timestamp: str,
                          send_callback=None) -> str | None:
    recent = db.get_recent_messages(limit=10)
    if not recent:
        return None

    # ✅ 构建动态上下文（记忆）
    dynamic_ctx = _build_dynamic_context(db)

    messages = [
        *[{"role": m["role"], "content": m["content"]} for m in recent],
        {
            "role": "user",
            "content": (
                f"[系统轮询 {timestamp}]\n"
                f"现在是悉尼时间 {timestamp}。根据对话上下文、当前时间"
                f"和你的记忆，选择一个行动：\n\n"
                f"1. **聊几句**：接之前话题、随便扯点什么、"
                f"对她提到过的事表示好奇，像朋友发微信一样\n"
                f"2. **关心一下**：该吃饭了、该休息了、之前说哪里不舒服\n"
                f"3. **提一嘴记忆里的事**：临近的 deadline、"
                f"之前说要注意的事，自然带出来别像念清单\n"
                f"4. **[SILENT]**：仅限以下情况——"
                f"用户明确说了要睡觉 / 30分钟内刚聊过 / "
                f"凌晨2点到早上8点\n\n"
                f"大多数时候选 1-3，找个自然的切入点。"
                f"不要打招呼或问'在吗'，直接说内容。"
            )
        }
    ]

    messages = _ensure_valid_messages(messages)
    reply = await _call_with_tools(
        db, SYSTEM_PROMPT, messages,
        send_callback=send_callback,
        dynamic_context=dynamic_ctx    # ✅ 传入记忆
    )

    if reply and "[SILENT]" not in reply:
        db.add_message("assistant", reply)
        return reply
    return None
```

---

## 5. API 接口（可选）

**文件**：`api/server.py`

如果前端需要展示/管理记忆，加两个接口：

```python
@app.get("/api/memories")
def get_memories():
    return db.get_all_memories()

@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int):
    db.delete_memory(memory_id)
    return {"status": "ok"}
```

---

## 实施顺序

建议按以下顺序逐步实施和测试，每步完成后验证再进入下一步：

```
Step 1 → 改 database.py（建 memories 表 + 方法）
         纯数据层，不影响现有功能，可以直接合入

Step 2 → 改 tools.py（新 SYSTEM_PROMPT + 3个记忆工具定义）
         替换 prompt + 追加工具，不影响运行逻辑

Step 3 → 改 ai_engine.py（_execute_tool 追加记忆工具分支）
         让工具真正能执行，此时可以测试记忆的存删改

Step 4 → 改 ai_engine.py（_build_dynamic_context + chat 传入）
         记忆开始注入每次对话，测试 AI 是否能看到并使用记忆

Step 5 → 改 ai_engine.py（_call_with_tools 发送逻辑）
         解决中间轮独白问题，测试 tool calling 场景

Step 6 → 改 ai_engine.py（proactive_check 重写）
         解决轮询沉默问题，测试主动聊天效果

Step 7 → （可选）改 api/server.py
         前端记忆管理接口
```

---

## 测试场景清单

改完后建议逐一测试：

| 场景 | 预期行为 |
|------|---------|
| "吃了个火锅，太辣了" | 自然回应 + 后台 log 事件，不说"已记录" |
| "下周三有作业 deadline" | save_memory + 自然回应，不只是嘴上说"好的" |
| "这两天提醒我来月经" | save_memory + 自然回应 |
| 轮询触发（deadline 临近） | 自然提一嘴 deadline，不念清单 |
| 轮询触发（无特殊事项） | 找话题聊，不 SILENT |
| "作业交了" | delete_memory + 自然回应 |
| "在调 bug" → "去做个饮料" → "回来继续" | bug 不被结束，饮料独立记录 |
| 多轮 tool calling | Discord 不出现第三人称独白 |
| 闲聊"哈哈哈好好笑" | 不触发记录，正常聊天 |
| 深夜轮询 | SILENT |
| 白天轮询、30分钟没聊过 | 主动找话题 |
