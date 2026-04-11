"""
工具定义模块
定义 AI 可以调用的所有 tools（function calling）
这是保证 AI 输出结构化数据的关键

⚠️ 这里只放**工具 schema** 和工具名子集。
所有 prompt 字符串（人设、规则、中间轮提醒、场景模板等）都在 `bot/prompts.py`。
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
                    },
                    "is_parallel": {
                        "type": "boolean",
                        "description": "是否是平行事件（一心二用时的次要活动）。用户同时在做另一件事、但这是注意力较次要的那条线时填 true。例：一边吃饭一边看剧 → '吃饭' 正常记，'看剧' 用 is_parallel=true。默认 false。"
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
            "name": "delete_reminder",
            "description": (
                "按 reminder_id 精准删除单条 pending reminder。"
                "主要用途：当你发现自己刚 set 了一条和已有内容重复的 reminder 时，"
                "用这个工具把重复的那一条去掉。与 cancel_reminders 的区别："
                "cancel_reminders 会清掉整个 group（多条），delete_reminder 只删指定的一条。"
                "只对 status=pending 的条目生效。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "要删除的 reminder id（从【待触发的跟进计划】或 list_reminders 结果中取）"
                    }
                },
                "required": ["reminder_id"]
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
                    },
                    "is_parallel": {
                        "type": "boolean",
                        "description": "修正事件的平行标记。通常不用改，除非之前记错了主/次活动。"
                    }
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_timeline_event",
            "description": "删除一条已记录的时间轴事件。用于清理重复/错误/过时的记录。如果刚建完发现是重复，立即调用此工具删掉新建的那条。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "integer",
                        "description": "要删除的事件 ID（从 query_timeline 或 log_timeline_event 返回值中获取）"
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

# ── 工具子集：轮询和提醒路径不需要全部工具 ──

# 随机轮询：主要是聊天、设提醒、管记忆
POLL_TOOL_NAMES = {
    "set_reminder", "delete_reminder", "query_timeline", "list_reminders",
    "save_memory", "delete_memory", "update_memory",
}

# 提醒触发：回应提醒、管记忆、取消后续提醒（禁止 set_reminder 防死循环）
REMINDER_TOOL_NAMES = {
    "query_timeline", "list_reminders", "cancel_reminders", "delete_reminder",
    "save_memory", "delete_memory", "update_memory",
}

# 统一调度入口：合并轮询和提醒的工具集
# 主动聊天时可以 set_reminder，提醒触发时可以 cancel_reminders，按 prompt 类型动态选择
SCHEDULED_TOOL_NAMES = POLL_TOOL_NAMES | REMINDER_TOOL_NAMES


# === Prompt 字符串已全部迁到 bot/prompts.py ===
# 原先这里有：TOOL_ROUND_REMINDER、TOOL_POST_HINTS、build_tool_round_hint、
# PROMPT_PERSONA/RESPONSE_GUIDELINES/TIME_PERCEPTION/TOOL_GUIDELINES、
# SYSTEM_PROMPT、SYSTEM_PROMPT_CONCISE、PERSONA_MARKER
# —— 都搬到了 bot/prompts.py，以便所有 prompt 在一个地方集中维护、减少重复发送。



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
                },
                "is_parallel": {
                    "type": "boolean",
                    "description": "平行事件标记：用户一心二用时的次要活动。例：一边吃饭一边看剧 → '吃饭' 正常记，'看剧' 用 is_parallel=true。平行事件不会打断主活动的时间线，也不计入注意力切换。默认 false。"
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
                },
                "is_parallel": {
                    "type": "boolean",
                    "description": "修正平行标记，通常不用改"
                }
            },
            "required": ["event_id"]
        }
    },
    {
        "name": "delete_timeline_event",
        "description": "删除一条已记录的时间轴事件。用于清理重复/错误/过时的记录。如果刚 log 完发现是重复，立即调用此工具删掉新建的那条。",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "integer",
                    "description": "要删除的事件 ID"
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
        "name": "delete_reminder",
        "description": (
            "按 reminder_id 精准删除单条 pending reminder。"
            "主要用途：当你发现自己刚 set 了一条和已有内容重复的 reminder 时，"
            "用这个工具把重复的那一条去掉。与 cancel_reminders 的区别："
            "cancel_reminders 会清掉整个 group（多条），delete_reminder 只删指定的一条。"
            "只对 status=pending 的条目生效。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {
                    "type": "integer",
                    "description": "要删除的 reminder id（从【待触发的跟进计划】或 list_reminders 结果中取）"
                }
            },
            "required": ["reminder_id"]
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