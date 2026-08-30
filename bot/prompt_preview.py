"""Read-only manifests for the prompts that each runtime track sees.

This module intentionally reuses the production prompt builders.  The only
difference is that deadline expiry maintenance is disabled, so opening the
Admin preview cannot change application data.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import config
from bot.ai_engine_base import CHAT_WITHOUT_BUSINESS_TOOLS, _build_prompt
from bot.async_pipeline.tool_worker import CONVERSATION_TOOL_NAMES
from bot.async_pipeline.worker_prompts import (
    build_result_expression_system,
    build_tool_worker_system,
)
from bot.database import Database
from bot.memory import MemoryService
from bot.memory.markdown_repository import estimate_tokens
from bot.prompts import (
    LEGACY_STRUCTURED_KEYS,
    PROMPT_SECTION_LABELS,
    render_check_in_prompt,
)
from bot.tools import POLL_TOOL_NAMES, REMINDER_TOOL_NAMES, get_tools


PROMPT_TRACK_MANIFEST: tuple[dict[str, Any], ...] = (
    {
        "key": "chat",
        "label": "聊天轨",
        "description": "普通用户消息的实时回复；apply 模式下不持有业务工具。",
    },
    {
        "key": "check_in",
        "label": "Check-in 表达轨",
        "description": "主动 check-in 的用户可见表达；模板作为最后一条 user 消息注入。",
    },
    {
        "key": "execution",
        "label": "执行轨",
        "description": "普通对话批次的后台工具判断与结构化执行结果。",
    },
    {
        "key": "result_expression",
        "label": "结果表达轨",
        "description": "把执行轨的私有结构化结果转换成日和对用户说的话。",
    },
    {
        "key": "timeline_renderer",
        "label": "Timeline 润色轨",
        "description": "计划中的异步碎片体润色角色；尚未接入运行时。",
    },
)

_RUNTIME_SECTION_CONSUMERS: dict[str, list[str]] = {
    "main_template": ["聊天轨", "Check-in 表达轨", "结果表达轨"],
    "tools": ["执行轨", "legacy 非 apply 模式"],
    "reminder": ["Reminder 到点表达"],
    "weather_report": ["/weather 命令"],
}
_SEED_ONLY_SECTIONS: dict[str, list[str]] = {
    "proactive_gemini": ["新数据库默认 Check-in 初始化"],
    "proactive_claude": ["新数据库默认 Check-in 初始化"],
    "morning": ["新数据库默认 Check-in 初始化"],
    "bedtime": ["新数据库默认 Check-in 初始化"],
}
_UNUSED_SECTIONS = frozenset({
    "dispatch_escalation_trigger",
    "dispatch_small_decide_output",
    "dispatch_paraphrase_task",
    "dispatch_big_worker_output",
})


def _source(
    kind: str,
    label: str,
    reference: str,
    *,
    included: bool = True,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "reference": reference,
        "included": included,
    }


def _stats(
    system_prompt: str,
    messages: list[dict[str, str]],
    tool_schemas: list[dict[str, Any]],
) -> dict[str, Any]:
    message_text = "\n".join(
        f"{item.get('role', '')}: {item.get('content', '')}" for item in messages
    )
    tools_text = (
        json.dumps(tool_schemas, ensure_ascii=False, sort_keys=True)
        if tool_schemas
        else ""
    )
    payload = json.dumps(
        {
            "system_prompt": system_prompt,
            "messages": messages,
            "tool_schemas": tool_schemas,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "system_chars": len(system_prompt),
        "message_chars": len(message_text),
        "tool_schema_chars": len(tools_text) if tool_schemas else 0,
        "total_chars": len(system_prompt) + len(message_text) + (
            len(tools_text) if tool_schemas else 0
        ),
        "estimated_tokens": estimate_tokens(
            "\n".join(part for part in (system_prompt, message_text, tools_text) if part)
        ),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12],
    }


def _track(
    manifest: dict[str, Any],
    *,
    status: str,
    system_prompt: str = "",
    messages: list[dict[str, str]] | None = None,
    tool_schemas: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
    caveats: list[str] | None = None,
) -> dict[str, Any]:
    messages = messages or []
    tool_schemas = tool_schemas or []
    return {
        **manifest,
        "status": status,
        "system_prompt": system_prompt,
        "messages": messages,
        "tool_schemas": tool_schemas,
        "sources": sources or [],
        "caveats": caveats or [],
        "stats": _stats(system_prompt, messages, tool_schemas),
    }


def _select_check_in(
    check_ins: list[dict[str, Any]], check_in_id: int | None
) -> dict[str, Any] | None:
    if check_in_id is not None:
        return next(
            (item for item in check_ins if int(item["id"]) == int(check_in_id)),
            None,
        )
    return next(
        (item for item in check_ins if item.get("enabled")),
        check_ins[0] if check_ins else None,
    )


def _section_inventory(db: Database) -> dict[str, Any]:
    rows = {item["key"]: item for item in db.list_prompt_sections()}
    main_template_active = bool(
        str((rows.get("main_template") or {}).get("value") or "").strip()
    )
    sections = []
    for key, label in PROMPT_SECTION_LABELS.items():
        row = rows.get(key) or {}
        if key in _RUNTIME_SECTION_CONSUMERS:
            status = "runtime"
            consumers = _RUNTIME_SECTION_CONSUMERS[key]
        elif key in LEGACY_STRUCTURED_KEYS:
            status = "fallback" if main_template_active else "runtime"
            consumers = [
                "main_template 为空时的兼容合成"
                if main_template_active
                else "当前 main_template fallback 合成"
            ]
        elif key in _SEED_ONLY_SECTIONS:
            status = "seed_only"
            consumers = _SEED_ONLY_SECTIONS[key]
        elif key in _UNUSED_SECTIONS:
            status = "unused"
            consumers = []
        else:
            status = "unknown"
            consumers = []
        value = str(row.get("value") or "")
        sections.append({
            "key": key,
            "label": row.get("label") or label,
            "status": status,
            "consumers": consumers,
            "chars": len(value),
            "updated_at": row.get("updated_at"),
        })
    counts = {
        status: sum(item["status"] == status for item in sections)
        for status in ("runtime", "fallback", "seed_only", "unused", "unknown")
    }
    return {"sections": sections, "counts": counts}


def build_prompt_preview(
    db: Database,
    *,
    memory_service: MemoryService | None = None,
    check_in_id: int | None = None,
    now: datetime | None = None,
    tool_worker_enabled: bool | None = None,
    tool_worker_apply: bool | None = None,
) -> dict[str, Any]:
    """Build every prompt preview from one read-only database snapshot."""
    current = now or datetime.now()
    memory_service = memory_service or getattr(db, "_memory_service", None) or MemoryService(db)
    worker_enabled = (
        config.ASYNC_TOOL_WORKER_ENABLED
        if tool_worker_enabled is None
        else bool(tool_worker_enabled)
    )
    apply_mode = (
        config.ASYNC_TOOL_APPLY
        if tool_worker_apply is None
        else bool(tool_worker_apply)
    )
    manifest = {item["key"]: item for item in PROMPT_TRACK_MANIFEST}
    common_caveat = (
        "这是当前数据库的只读快照；预览不会调用天气、Calendar 或语义检索，"
        "这些请求相关内容只会在真实调用时出现。"
    )

    chat_parts = _build_prompt(
        db,
        "chat",
        context_config={"include_tools": not apply_mode},
        memory_service=memory_service,
        maintain_deadline_status=False,
        now=current,
    )
    if apply_mode:
        chat_parts = chat_parts.with_suffix(CHAT_WITHOUT_BUSINESS_TOOLS)
    chat_tools = [] if apply_mode else get_tools()
    chat = _track(
        manifest["chat"],
        status="active",
        system_prompt=chat_parts.flatten(),
        tool_schemas=chat_tools,
        sources=[
            _source("database", "整体 Prompt 模板", "prompt_sections.main_template"),
            _source(
                "database",
                "工具策略",
                "prompt_sections.tools",
                included=not apply_mode,
            ),
            _source("dynamic", "记忆与生活上下文", "runtime context"),
            _source(
                "code",
                "聊天轨无工具契约",
                "bot.ai_engine_base.CHAT_WITHOUT_BUSINESS_TOOLS",
                included=apply_mode,
            ),
        ],
        caveats=[common_caveat, "真实调用还会带上当时的对话窗口。"],
    )

    check_ins = db.list_check_ins()
    selected_check_in = _select_check_in(check_ins, check_in_id)
    if check_in_id is not None and selected_check_in is None:
        raise KeyError(f"unknown check-in: {check_in_id}")

    if selected_check_in is None:
        check_in = _track(
            manifest["check_in"],
            status="unavailable",
            caveats=["当前没有可预览的 check-in。"],
        )
    else:
        context_config = dict(selected_check_in.get("context_config") or {})
        no_tools = apply_mode or selected_check_in.get("tool_profile") == "none"
        if no_tools:
            context_config["include_tools"] = False
        check_in_parts = _build_prompt(
            db,
            "poll",
            context_config=context_config,
            memory_service=memory_service,
            maintain_deadline_status=False,
            now=current,
        )
        if no_tools:
            check_in_parts = check_in_parts.with_suffix(CHAT_WITHOUT_BUSINESS_TOOLS)
        rendered_instruction = render_check_in_prompt(
            selected_check_in,
            current.strftime("%Y-%m-%d %H:%M"),
        )
        messages = [{
            "role": "user",
            "label": f"check_ins.{selected_check_in['name']}.prompt_template",
            "content": (
                f"[check_in:{selected_check_in['name']}]\n{rendered_instruction}"
            ),
        }]
        profile = selected_check_in.get("tool_profile") or "poll"
        if no_tools:
            check_in_tools = []
        elif profile == "reminder_safe":
            check_in_tools = get_tools(REMINDER_TOOL_NAMES)
        else:
            check_in_tools = get_tools(POLL_TOOL_NAMES)
        check_in = _track(
            manifest["check_in"],
            status="active" if selected_check_in.get("enabled") else "inactive",
            system_prompt=check_in_parts.flatten(),
            messages=messages,
            tool_schemas=check_in_tools,
            sources=[
                _source("database", "整体 Prompt 模板", "prompt_sections.main_template"),
                _source(
                    "database",
                    f"Check-in：{selected_check_in.get('label') or selected_check_in['name']}",
                    f"check_ins.{selected_check_in['name']}.prompt_template",
                ),
                _source("dynamic", "Check-in 上下文开关", "check_ins.context_config_json"),
                _source(
                    "code",
                    "聊天轨无工具契约",
                    "bot.ai_engine_base.CHAT_WITHOUT_BUSINESS_TOOLS",
                    included=no_tools,
                ),
            ],
            caveats=[
                common_caveat,
                "真实调用会在这条 user 消息之前附带对话窗口。",
                *(
                    ["apply 模式下，Check-in 需要的工具工作会另行交给执行轨。"]
                    if apply_mode
                    else []
                ),
            ],
        )

    execution_tools = get_tools(CONVERSATION_TOOL_NAMES)
    execution = _track(
        manifest["execution"],
        status="active" if worker_enabled else "inactive",
        system_prompt=build_tool_worker_system(
            db,
            maintain_deadline_status=False,
            now=current,
        ),
        tool_schemas=execution_tools,
        sources=[
            _source(
                "code",
                "执行轨协议与 JSON 契约",
                "bot.async_pipeline.worker_prompts.TOOL_WORKER_CORE",
            ),
            _source("database", "工具业务策略", "prompt_sections.tools"),
            _source("dynamic", "执行上下文", "projects/timeline/reminders/deadlines"),
            _source("code", "可用工具 schema", "bot.tools.TOOLS"),
        ],
        caveats=[
            "这里只预览普通对话批次；check-in 批次会按 tool_profile 缩小工具集。",
            "CONTEXT_ONLY、AUTHORIZED_NEW_INPUT 和既有调用账本取决于具体批次，未在这里伪造。",
        ],
    )

    result_expression = _track(
        manifest["result_expression"],
        status="active" if worker_enabled and apply_mode else "inactive",
        system_prompt=build_result_expression_system(
            db,
            memory_service=memory_service,
            maintain_deadline_status=False,
            now=current,
        ),
        sources=[
            _source("database", "整体 Prompt 模板", "prompt_sections.main_template"),
            _source(
                "code",
                "聊天轨无工具契约",
                "bot.ai_engine_base.CHAT_WITHOUT_BUSINESS_TOOLS",
            ),
            _source(
                "code",
                "执行结果表达契约",
                "bot.async_pipeline.worker_prompts.RESULT_EXPRESSION_CORE",
            ),
            _source("dynamic", "最近对话与执行结果", "runtime result envelope"),
        ],
        caveats=[
            common_caveat,
            "真实调用还会带最近对话，以及当前批次的 EXECUTION_TRACK_RESULT。",
        ],
    )

    timeline_renderer = _track(
        manifest["timeline_renderer"],
        status="planned",
        sources=[
            _source(
                "plan",
                "瞬间碎片记录者 prompt 草稿",
                "docs/plan/parallel-chat-tool-split.md#112",
                included=False,
            ),
        ],
        caveats=["已进入设计计划，但目前没有运行时调用或可生效 prompt。"],
    )

    return {
        "generated_at": current.isoformat(timespec="seconds"),
        "read_only": True,
        "runtime": {
            "tool_worker_enabled": worker_enabled,
            "tool_worker_apply": apply_mode,
        },
        "selected_check_in_id": (
            int(selected_check_in["id"]) if selected_check_in is not None else None
        ),
        "check_ins": [
            {
                "id": int(item["id"]),
                "name": item["name"],
                "label": item.get("label") or item["name"],
                "enabled": bool(item.get("enabled")),
            }
            for item in check_ins
        ],
        "section_inventory": _section_inventory(db),
        "tracks": [
            chat,
            check_in,
            execution,
            result_expression,
            timeline_renderer,
        ],
    }
