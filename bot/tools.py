"""
工具定义模块
定义 AI 可以调用的所有 tools（function calling）
这是保证 AI 输出结构化数据的关键

⚠️ 这里只放**工具 schema**（工具是什么、参数格式）和工具名子集。
所有使用策略（什么时候调、跨工具决策逻辑）集中在 `bot/prompts.py::TOOLS_SECTION`。
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
                        "description": "兼容旧数据字段；新事件按时刻点记录，通常不要填写"
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
                        "description": "兼容旧数据字段；新事件通常不要填写。"
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
            "name": "query_timeline",
            "description": "查询指定时间范围内的活动记录。",
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
            "description": "更新一条已有的时间轴事件。notes 会追加到已有内容后面，不会覆盖。",
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
            "name": "attach_recent_image_to_event",
            "description": "把最近收到但尚未挂入 event 的图片附件保存并挂到指定 timeline event。适用于用户说“上一张图必须入库/把刚才图片加到这条 event”。",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "integer",
                        "description": "要挂图片的 timeline event ID"
                    },
                    "image_hash": {
                        "type": "string",
                        "description": "可选。图片 SHA-256 hash；如果不填，默认使用最近一张未入库图片"
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
            "description": "记住一条信息。上限 20 条，满了自动清理最旧的。",
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
            "description": "更新一条记忆的内容。同时刷新时间，防止被自动清理。",
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

# 新建类工具：只有这些操作才触发 ✅ reaction（update/delete/query 均不触发）
SET_TOOL_NAMES = {
    "log_timeline_event",
    "attach_recent_image_to_event",
    "set_reminder",
    "save_memory",
    "add_deadline",
}

# 随机轮询：主要是聊天、设提醒、管记忆
POLL_TOOL_NAMES = {
    "set_reminder", "delete_reminder", "list_reminders",
    "save_memory", "delete_memory", "update_memory",
    "add_deadline", "complete_deadline", "delete_deadline",
}

# 提醒触发：回应提醒、管记忆、取消后续提醒（禁止 set_reminder 防死循环）
REMINDER_TOOL_NAMES = {
    "list_reminders", "cancel_reminders", "delete_reminder",
    "save_memory", "delete_memory", "update_memory",
}

# 统一调度入口：合并轮询和提醒的工具集
# 主动聊天时可以 set_reminder，提醒触发时可以 cancel_reminders，按 prompt 类型动态选择
SCHEDULED_TOOL_NAMES = POLL_TOOL_NAMES | REMINDER_TOOL_NAMES



def get_tools(tool_names=None):
    """Return OpenAI-style tools, optionally filtered by function name."""
    if tool_names is None:
        return TOOLS
    return [t for t in TOOLS if t["function"]["name"] in tool_names]


def to_anthropic_tools(tools):
    """Convert canonical OpenAI-style tool schema to Anthropic tool schema."""
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get("parameters", {
                "type": "object",
                "properties": {},
                "required": [],
            }),
        }
        for t in tools
    ]
