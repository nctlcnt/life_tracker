"""Strict propose/apply pipeline for curator-owned long-term memories."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


CURATOR_NAME = "memory-curator-v1"
CURATOR_ACTIONS = {"create", "update", "supersede", "archive"}
CURATOR_EVIDENCE_ROLES = {"supports", "contradicts", "supersedes"}
CURATOR_MEMORY_TYPES = {
    "preference",
    "identity",
    "interaction_style",
    "current_state",
    "open_loop",
    "project_intent",
    "temporary_context",
    "plan",
    "general",
}


def _require_object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _require_exact_keys(value: dict, *, required: set[str],
                        optional: set[str], field: str) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing:
        raise ValueError(f"{field} missing fields: {missing}")
    if unknown:
        raise ValueError(f"{field} has unknown fields: {unknown}")


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text or null")
    return value.strip() or None


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return int(value)


@dataclass(frozen=True)
class CuratorSource:
    message_id: int
    quote: str | None
    evidence_role: str


@dataclass(frozen=True)
class CuratorOperation:
    action: str
    reason: str
    sources: tuple[CuratorSource, ...]
    memory_id: int | None = None
    summary: str | None = None
    memory_type: str | None = None
    quote: str | None = None
    has_quote: bool = False


@dataclass(frozen=True)
class CuratorBatch:
    operations: tuple[CuratorOperation, ...]

    def to_dict(self) -> dict:
        operations = []
        for operation in self.operations:
            item = {
                "action": operation.action,
                "reason": operation.reason,
                "sources": [
                    {
                        "message_id": source.message_id,
                        "quote": source.quote,
                        "evidence_role": source.evidence_role,
                    }
                    for source in operation.sources
                ],
            }
            if operation.memory_id is not None:
                item["memory_id"] = operation.memory_id
            if operation.summary is not None:
                item["summary"] = operation.summary
            if operation.memory_type is not None:
                item["memory_type"] = operation.memory_type
            if operation.has_quote:
                item["quote"] = operation.quote
            operations.append(item)
        return {"operations": operations}


def _parse_source(raw: Any, index: str) -> CuratorSource:
    source = _require_object(raw, index)
    _require_exact_keys(
        source,
        required={"message_id", "evidence_role"},
        optional={"quote"},
        field=index,
    )
    role = _require_text(source["evidence_role"], f"{index}.evidence_role")
    if role not in CURATOR_EVIDENCE_ROLES:
        raise ValueError(f"{index}.evidence_role is invalid: {role}")
    return CuratorSource(
        message_id=_require_positive_int(source["message_id"], f"{index}.message_id"),
        quote=_optional_text(source.get("quote"), f"{index}.quote"),
        evidence_role=role,
    )


def _parse_operation(raw: Any, index: int) -> CuratorOperation:
    field = f"operations[{index}]"
    operation = _require_object(raw, field)
    action = _require_text(operation.get("action"), f"{field}.action")
    if action not in CURATOR_ACTIONS:
        raise ValueError(f"{field}.action is invalid: {action}")

    common = {"action", "reason", "sources"}
    required_by_action = {
        "create": {"summary", "memory_type"},
        "update": {"memory_id"},
        "supersede": {"memory_id", "summary", "memory_type"},
        "archive": {"memory_id"},
    }
    optional_by_action = {
        "create": {"quote"},
        "update": {"summary", "memory_type", "quote"},
        "supersede": {"quote"},
        "archive": set(),
    }
    _require_exact_keys(
        operation,
        required=common | required_by_action[action],
        optional=optional_by_action[action],
        field=field,
    )

    raw_sources = operation["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError(f"{field}.sources must be a non-empty list")
    sources = tuple(
        _parse_source(source, f"{field}.sources[{source_index}]")
        for source_index, source in enumerate(raw_sources)
    )
    keys = {(source.message_id, source.evidence_role) for source in sources}
    if len(keys) != len(sources):
        raise ValueError(f"{field}.sources contains duplicate evidence")

    summary = None
    if "summary" in operation:
        summary = _require_text(operation["summary"], f"{field}.summary")
    memory_type = None
    if "memory_type" in operation:
        memory_type = _require_text(operation["memory_type"], f"{field}.memory_type")
        if memory_type not in CURATOR_MEMORY_TYPES:
            raise ValueError(f"{field}.memory_type is invalid: {memory_type}")
    if action == "update" and not ({"summary", "memory_type", "quote"} & set(operation)):
        raise ValueError(f"{field} update has no content changes")

    return CuratorOperation(
        action=action,
        reason=_require_text(operation["reason"], f"{field}.reason"),
        sources=sources,
        memory_id=(
            _require_positive_int(operation["memory_id"], f"{field}.memory_id")
            if "memory_id" in operation else None
        ),
        summary=summary,
        memory_type=memory_type,
        quote=(
            _optional_text(operation.get("quote"), f"{field}.quote")
            if "quote" in operation else None
        ),
        has_quote="quote" in operation,
    )


def parse_curator_batch(raw: str | dict) -> CuratorBatch:
    """Parse model output without accepting markdown fences or extra fields."""
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"curator output is not valid JSON: {exc.msg}") from exc
    else:
        payload = raw
    payload = _require_object(payload, "batch")
    _require_exact_keys(
        payload, required={"operations"}, optional=set(), field="batch")
    operations = payload["operations"]
    if not isinstance(operations, list):
        raise ValueError("batch.operations must be a list")
    return CuratorBatch(tuple(
        _parse_operation(operation, index)
        for index, operation in enumerate(operations)
    ))


def build_curator_prompt(*, messages: list[dict], memories: list[dict],
                         now: str | None = None) -> str:
    """Build a self-contained prompt whose evidence handles are DB message ids."""
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    visible = [
        {
            "message_id": int(message["id"]),
            "role": message["role"],
            "created_at": message["created_at"],
            "content": message["content"],
        }
        for message in messages
    ]
    current_memories = [
        {
            "memory_id": int(memory["id"]),
            "summary": memory["summary"],
            "memory_type": memory["memory_type"],
            "status": memory["status"],
        }
        for memory in memories
    ]
    return "\n\n".join([
        "你是私人助理的长期记忆 curator。只提取未来对话确实有复用价值、且有明确原文证据的信息。",
        "不要把普通闲聊、模型推断、时间线事件、todo、deadline 或一次性细节写成长期记忆。"
        "不确定时输出空 operations。",
        f"当前 UTC 时间是 {now}。必须结合每条消息的 created_at 判断时效。"
        "旧消息可以证明稳定偏好或身份，但不能单独证明 current_state、temporary_context、"
        "open_loop 或 project_intent 现在仍成立；已经过去的安排、课程作业和状态不要写成当前记忆。",
        "只输出一个 JSON 对象，不要 markdown，不要解释。顶层只能有 operations。"
        "action 只能是 create/update/supersede/archive。"
        "每条 operation 必须有 reason 和非空 sources；sources 的 message_id 只能使用本批消息 id，"
        "quote 若提供必须是对应 content 的原文子串。"
        "create/supersede 必须有 summary、memory_type；update 必须有 memory_id 和至少一个内容变更；"
        "archive 只表示当前记忆已经不再有用。",
        "memory_type 只能是：" + ", ".join(sorted(CURATOR_MEMORY_TYPES)) + "。",
        "输出示例："
        '{"operations":[{"action":"create","summary":"用户偏好简短回复",'
        '"memory_type":"preference","reason":"用户明确表达了稳定偏好",'
        '"sources":[{"message_id":123,"quote":"回复短一点",'
        '"evidence_role":"supports"}]}]}',
        "【现有长期记忆】\n" + json.dumps(
            current_memories, ensure_ascii=False, separators=(",", ":")),
        "【本批可见消息】\n" + json.dumps(
            visible, ensure_ascii=False, separators=(",", ":")),
    ])


def load_curator_interval(db, *, channel_id: str, after_message_id: int,
                          limit: int) -> list[dict]:
    """Load one continuous, immutable-by-id curator batch."""
    return db.get_conversation_messages_after(
        str(channel_id), int(after_message_id), limit=max(int(limit), 0))
