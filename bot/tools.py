"""
工具定义模块
定义 AI 可以调用的所有 tools（function calling）
这是保证 AI 输出结构化数据的关键

⚠️ 这里只放**工具 schema**（工具是什么、参数格式）和工具名子集。
所有使用策略（什么时候调、跨工具决策逻辑）集中在 DB 的 `tools` prompt section。
"""

# Canonical tool schema. OpenAI / Relay 直接使用；Claude / Gemini 从这里转换。

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "log_timeline_event",
            "description": "记录一条已发生的生活轨迹时间轴事件。",
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
                        "description": "事件标题，高度概括这段时间在做什么。高度概括的标题（动词+宾语），例如：看剧、吃午饭、写代码"
                    },
                    "category": {
                        "type": "string",
                        "enum": ["Focus", "Routine", "Chill"],
                        "description": ("三分法分类：\n"
                "- Focus：需要脑力投入的活动，必须填 project_name，且只能使用现有项目列表里的项目\n"
                "- Routine：日常维护（吃饭、洗澡、家务等）\n"
                "- Chill：娱乐放松\n\n"),
                    },
                    "project_name": {
                        "type": "string",
                        "description": "项目名称，category=Focus 时必填。只能填写【现有项目列表】里已经存在的项目，不要自行创造新项目名，改记为非 Focus，或在回复里让她先到 Project Overview 手动添加项目。"
                    },
                    "notes": {
                        "type": "string",
                        "description": "事件的具体细节+感受、感想、心情或备注。包括具体内容（如剧名、菜名）和用户原话感受。"
                    },
                    "session_id": {
                        "type": "integer",
                        "description": "如果这是在恢复或继续之前的某个被打断的活动，填入之前那条活动记录的 event_id。全新活动可不填。"
                    },
                },
                "required": ["start_time", "content", "category"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "预约一次未来的主动联系。到时间后你会被唤醒，拿到 action 作为上下文，自行决定对用户说什么。",
            "parameters": {
                "type": "object",
                "properties": {
                    "trigger_time": {
                        "type": "string",
                        "description": "提醒触发时间，ISO 8601 格式"
                    },
                    "action": {
                        "type": "string",
                        "description": "给未来的自己的上下文备忘，如'检查复习进度'、'问问剧看完没'"
                    },
                    "group_id": {
                        "type": "string",
                        "description": "同一件事的多条 reminder 共享的标识，如 'exam_0416'。单条可不填。"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "low=随意跟进 normal=正常 high=重要deadline"
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
            "description": "取消某个 group 下所有未触发的 reminder。",
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
            "description": "按 reminder_id 精准删除单条 pending reminder。只对 status=pending 生效。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "要删除的 reminder id（从 list_reminders 结果中取）"
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
            "description": "查看当前所有 pending 的 reminder。",
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
            "name": "query_calendar",
            "description": "查询已启用的 Google Calendars 中计划中的日程。用于查未来/当天安排；只读，不会创建或修改日历。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "查询起始时间，本地 ISO 8601 格式或 YYYY-MM-DD。日期按本地当天 00:00 解析。"
                    },
                    "end": {
                        "type": "string",
                        "description": "查询结束时间，本地 ISO 8601 格式或 YYYY-MM-DD。作为 exclusive 上界。"
                    },
                    "query": {
                        "type": "string",
                        "description": "可选关键词过滤，例如课程名、地点或标题片段。"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回条数，默认 50，上限 100。"
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
            "description": "更新一条已有的时间轴事件。notes 会追加到已有内容后面，不会覆盖。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "integer",
                        "description": "要更新的事件 ID（从今天 timeline 上下文或 log_timeline_event 返回值中获取）"
                    },
                    "end_time": {
                        "type": "string",
                        "description": "事件结束时间，ISO 8601 格式"
                    },
                    "content": {
                        "type": "string",
                        "description": "更新后的事件标题"
                    },
                    "category": {
                        "type": "string",
                        "enum": ["Focus", "Routine", "Chill"],
                        "description": "更新后的事件分类：Focus / Routine / Chill"
                    },
                    "project_name": {
                        "type": "string",
                        "description": "更新项目名称（category=Focus 时适用）。只能填写【现有项目列表】里已经存在的项目，不要自行创造新项目名。"
                    },
                    "notes": {
                        "type": "string",
                        "description": "要追加的新信息/感想/备注（会追加到已有 notes 后面，不会覆盖）"
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
            "description": "删除一条时间轴事件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "integer",
                        "description": "要删除的事件 ID（从今天 timeline 上下文或 log_timeline_event 返回值中获取）"
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
            "description": (
                "记住一条长期不变的事实（偏好、身份信息），不是日常进展的备忘录——"
                "日常聊到的具体事情（比如某个作业做到哪一步了）系统会自动记录，不需要主动存。"
                "只在信息属于'很久以后也大概率还成立'时才调用，例如 '喜欢喝抹茶'、'在读数据科学'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的内容，简洁完整，如 '喜欢喝抹茶'、'在读数据科学专业'"
                    },
                    "memory_type": {
                        "type": "string",
                        "description": "可选，自由文本分类，如 preference / identity / interaction_style"
                    },
                    "valid_until": {
                        "type": "string",
                        "description": "可选，ISO 8601 时间。这条记忆到期后不再进入 prompt。不传 = 永久有效"
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
            "description": "删除一条记忆。",
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
            "description": "更新一条记忆的内容/分类/有效期。",
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
                    },
                    "memory_type": {
                        "type": "string",
                        "description": "可选，更新分类"
                    },
                    "valid_until": {
                        "type": "string",
                        "description": "可选，更新有效期（ISO 8601），传空字符串表示改回永久有效"
                    }
                },
                "required": ["memory_id", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": (
                "语义检索过往对话历史（当前上下文窗口之外的旧对话）。"
                "当你想回忆用户之前提过的具体事情——尤其是主动联系时想跟进某个话题、"
                "但眼前上下文里没有细节——先用它查证，不要凭印象编。"
                "query 用描述性自然语言写你想找的内容（如 'ECON5111 seminar 时间'、'上次做蛋糕'）。"
                "返回按相关度排序的历史片段；查不到就是没聊过或记录太久远。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "想找的内容，一句描述性自然语言"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_deadline",
            "description": "记录一个 deadline。系统自动计算倒计时并在动态上下文中展示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "deadline 标题，简洁明确，如 '数据科学期中考'、'Spark 作业'"
                    },
                    "due_time": {
                        "type": "string",
                        "description": "截止时间，ISO 8601 格式。相对时间要转成绝对时间"
                    }
                },
                "required": ["title", "due_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "complete_deadline",
            "description": "标记一个 deadline 为已完成。",
            "parameters": {
                "type": "object",
                "properties": {
                    "deadline_id": {
                        "type": "integer",
                        "description": "deadline id（从【待完成的 Deadline】中取）"
                    }
                },
                "required": ["deadline_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_deadline",
            "description": "删除一个 deadline。",
            "parameters": {
                "type": "object",
                "properties": {
                    "deadline_id": {
                        "type": "integer",
                        "description": "deadline id"
                    }
                },
                "required": ["deadline_id"]
            }
        }
    }
]

# ── 工具子集：轮询和提醒路径不需要全部工具 ──

# 写入类工具：新建或更新轨迹/记录才触发 ✅ reaction（delete/query 不触发）
SET_TOOL_NAMES = {
    "log_timeline_event",
    "update_timeline_event",
    "set_reminder",
    "save_memory",
    "add_deadline",
}

# 随机轮询：主要是聊天、设提醒、管记忆
# search_history 解决 poll 没有用户消息做检索锚点的问题——AI 自己写 query 回忆
POLL_TOOL_NAMES = {
    "set_reminder", "delete_reminder", "list_reminders",
    "save_memory", "delete_memory", "update_memory",
    "add_deadline", "complete_deadline", "delete_deadline",
    "search_history",
}

# 提醒触发：回应提醒、管记忆、取消后续提醒（禁止 set_reminder 防死循环）
REMINDER_TOOL_NAMES = {
    "list_reminders", "cancel_reminders", "delete_reminder",
    "save_memory", "delete_memory", "update_memory",
    "search_history",
}

# 统一调度入口：合并轮询和提醒的工具集
# 主动聊天时可以 set_reminder，提醒触发时可以 cancel_reminders，按 prompt 类型动态选择
SCHEDULED_TOOL_NAMES = POLL_TOOL_NAMES | REMINDER_TOOL_NAMES



def get_tools(tool_names=None):
    """Return OpenAI-style tools, optionally filtered by function name."""
    if tool_names is None:
        return TOOLS
    return [t for t in TOOLS if t["function"]["name"] in tool_names]


