"""
工具定义模块
定义 AI 可以调用的所有 tools（function calling）
这是保证 AI 输出结构化数据的关键
"""

# OpenAI function calling 格式的工具定义
# 如果你用 Anthropic Claude API，格式略有不同，但字段含义一样

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "log_timeline_event",
            "description": "记录一条生活轨迹时间轴事件。当用户提到做了什么事、正在做什么、或者你从对话中推断出用户的活动时，调用此工具记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": "事件开始时间，ISO 8601 格式，例如 2026-04-05T13:00:00"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "事件结束时间，ISO 8601 格式。如果事件还在进行中，可以不填"
                    },
                    "content": {
                        "type": "string",
                        "description": "事件的简短描述，例如：看电视剧《月麟绮纪》、吃午饭、洗衣服"
                    },
                    "notes": {
                        "type": "string",
                        "description": "用户的原始感想、心情或备注，原文保留，例如：觉得非常开心、有点疲惫但很充实"
                    },
                    "category": {
                        "type": "string",
                        "description": "事件分类，例如：休息、工作、社交、生活、健康、娱乐、出行"
                    }
                },
                "required": ["start_time", "content", "category"]
            }
        }
    },
    {
        "type": "function",
        "function": {
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
            "parameters": {
                "type": "object",
                "properties": {
                    "trigger_time": {
                        "type": "string",
                        "description": "提醒触发时间，ISO 8601 格式"
                    },
                    "action": {
                        "type": "string",
                        "description": "给未来的自己的备忘。不是发给用户的文案，而是上下文提示，如'检查复习进度'、'问问剧看完没'"
                    },
                    "group_id": {
                        "type": "string",
                        "description": "同一件事的多条 reminder 共享的标识，简洁有意义，如 'exam_0416'、'buy_cat_food'。单条可不填。"
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
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminders",
            "description": "取消某个 group 下所有未触发的 reminder。用户说事情做完了/取消了/不需要了时调用。如'考完了' → 取消 exam 相关的所有后续 reminder。",
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "查看当前所有未完成的 reminder。当用户问'我还有什么安排/提醒'时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_timeline",
            "description": "查询用户在某个时间范围内的活动记录。当用户问'我今天做了什么'、'这周的时间分布'等问题时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "查询起始时间，ISO 8601 格式"
                    },
                    "end": {
                        "type": "string",
                        "description": "查询结束时间，ISO 8601 格式"
                    }
                },
                "required": ["start", "end"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_timeline_event",
            "description": "更新一条已记录的时间轴事件。当用户延续之前的活动、补充结束时间、或修正之前的记录时，用此工具而不是新建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "integer",
                        "description": "要更新的事件 ID（从 log_timeline_event 返回值或 query_timeline 结果中获取）"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "事件结束时间，ISO 8601 格式"
                    },
                    "content": {
                        "type": "string",
                        "description": "更新后的事件描述"
                    },
                    "category": {
                        "type": "string",
                        "description": "更新后的事件分类"
                    },
                    "notes": {
                        "type": "string",
                        "description": "更新后的感想/备注"
                    }
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "记住一条信息。用于：deadline、用户偏好习惯、最近在做的事、用户说'记得提醒我XX'、任何以后可能有用的信息。记忆上限20条，满了自动清理最旧的。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的内容，简洁完整，如 '4/16 周三 数据科学作业 deadline'、'喜欢喝抹茶'"
                    }
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": "删除一条过期或不再需要的记忆。如 deadline 过了、事情完成了、信息过时了。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "integer",
                        "description": "要删除的 memory id"
                    }
                },
                "required": ["memory_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": "更新一条记忆的内容。如 deadline 改了、信息有变化。同时会刷新时间，防止被自动清理。",
            "parameters": {
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
    }
]

# System Prompt - AI 的人设和行为规则
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
- ⚠️ 绝对不要重复提问：如果最近聊天记录（context）里已经讨论过某个作业、deadline 或提醒事项，即使它还在记忆里，这次也**不要再提了**，避免像机器一样啰嗦惹人烦。

## 主动聊天

你不是只在有事的时候才说话的工具。轮询时你可以：
- 接之前话题聊几句
- 对她提到的事表示好奇（"那个剧后来怎么样了"）
- 分享一个随机的想法或吐槽
- 关心一下状态（但不要每次都问"在干嘛"）

**只有这些情况才沉默**：刚说了要睡觉、刚聊过没多久、凌晨深夜。
其他时候找个自然的话题聊。注意：在系统轮询时，你也会看到【待触发的跟进计划】。请参考这些计划避免发起与即将触发的提醒高度重复的话题。

当前时间会在每条消息中标注（悉尼本地时间）。
"""
# ============ 在 tools.py 末尾加上这段 ============

# Anthropic Claude 格式的工具定义
TOOLS_ANTHROPIC = [
    {
        "name": "log_timeline_event",
        "description": "记录一条生活轨迹时间轴事件。当用户提到做了什么事、正在做什么、或者你从对话中推断出用户的活动时，调用此工具记录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "事件开始时间，ISO 8601 格式，例如 2026-04-05T13:00:00"
                },
                "end_time": {
                    "type": "string",
                    "description": "事件结束时间，ISO 8601 格式。如果事件还在进行中，可以不填"
                },
                "content": {
                    "type": "string",
                    "description": "事件的简短描述，例如：看电视剧《月麟绮纪》、吃午饭、洗衣服"
                },
                "category": {
                    "type": "string",
                    "description": "事件分类，优先从以下选择：休息、工作、社交、生活、健康、娱乐、出行。如果都不合适，可以自己创建新分类"
                },
                "notes": {
                    "type": "string",
                    "description": "用户的感想、心情、备注。尽量保留用户的原话，不要改写。如果用户没有表达感想则不填。"
                },
                "session_id": {
                    "type": "integer",
                    "description": "如果这是在恢复或继续之前的某个被打断的活动，填入之前那条活动记录的 event_id。如果是全新活动，可以放空。"
                }
            },
            "required": ["start_time", "content", "category"]
        }
    },
    {
        "name": "update_timeline_event",
        "description": "更新一条已有的时间轴事件。当用户在延续之前的活动、补充结束时间、或修正之前的记录时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "integer",
                    "description": "要更新的事件ID，从 query_timeline 结果或 log_timeline_event 返回值中获取"
                },
                "end_time": {
                    "type": "string",
                    "description": "更新结束时间，ISO 8601 格式"
                },
                "content": {
                    "type": "string",
                    "description": "更新事件描述"
                },
                "category": {
                    "type": "string",
                    "description": "更新分类"
                },
                "notes": {
                    "type": "string",
                    "description": "更新备注"
                }
            },
            "required": ["event_id"]
        }
    },
    {
        "name": "set_reminder",
        "description": 
            "预约一次未来的主动联系。到时间后你会被唤醒，拿到 action 作为上下文，自行决定对用户说什么。\n\n"
            "这不是闹钟通知，是你给自己安排的 follow-up 计划。\n\n"
            "使用场景：\n"
            "- 用户提到 deadline → 根据紧急程度安排多条，越临近越密集\n"
            "  例：后天考试 → 今晚1条 + 明天2条 + 后天早上1条\n"
            "- 用户说看两集就回来 → 1.5h 后设1条\n"
            "- 用户说要做某事（买猫粮/交作业）→ 当天晚上或明天设1条跟进\n"
            "- 同一件事的多条 reminder 用相同的 group_id\n\n"
            "⚠️ 收到 [提醒触发] 前缀消息时，那条 reminder 已经触发了，直接回应用户，绝对不要再 set_reminder 设相同内容！",
        "input_schema": {
            "type": "object",
            "properties": {
                "trigger_time": {
                    "type": "string",
                    "description": "提醒触发时间，ISO 8601 格式"
                },
                "action": {
                    "type": "string",
                    "description": "给未来的自己的备忘。不是发给用户的文案，而是上下文提示，如'检查复习进度'、'问问剧看完没'"
                },
                "group_id": {
                    "type": "string",
                    "description": "同一件事的多条 reminder 共享的标识，简洁有意义，如 'exam_0416'"
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "low=随意跟进 normal=正常 high=重要deadline，即使刚聊过也要提"
                }
            },
            "required": ["trigger_time", "action"]
        }
    },
    {
        "name": "cancel_reminders",
        "description": "取消某个 group 下所有未触发的 reminder。用户说事情做完了/取消了/不需要了时调用。",
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
    },
    {
        "name": "list_reminders",
        "description": "查看当前所有未完成的 reminder。当用户问'我还有什么安排/提醒'时调用。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "query_timeline",
        "description": "查询用户在某个时间范围内的活动记录。当用户问'我今天做了什么'、'这周的时间分布'等问题时调用。也用于在更新事件前查找 event_id。",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "查询起始时间，ISO 8601 格式"
                },
                "end": {
                    "type": "string",
                    "description": "查询结束时间，ISO 8601 格式"
                }
            },
            "required": ["start", "end"]
        }
    },
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
]