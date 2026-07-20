"""Curator propose/apply 共享管线（CLI 脚本与 scheduler 共用）。

流程：读区间 → 建 prompt → 模型补全 → 严格 parse + 证据校验；
首次失败走一轮定向修复（build_curator_repair_prompt），再失败上抛。
apply 只在 auto_apply 显式打开时执行，且复用与人工 apply 完全相同的
repository 事务路径。

注意：对 AI 引擎的 import 必须留在函数内（ai_engine_base 反向依赖
bot.memory 包，模块级 import 会成环）。
"""
from __future__ import annotations

import config
from bot.logger import get_logger
from bot.memory.curator import (
    CURATOR_NAME,
    build_curator_prompt,
    build_curator_repair_prompt,
    load_curator_interval,
    parse_curator_batch,
)

logger = get_logger(__name__)

DEFAULT_MAX_OUTPUT_TOKENS = 16384
DEFAULT_REQUEST_TIMEOUT = 600.0


def resolve_curator_preset():
    """curator 专用 preset：配置有效用配置，否则回落 active。"""
    name = getattr(config, "CURATOR_PRESET", "")
    if name and name in config.PRESETS:
        return config.PRESETS[name]
    return config.get_active()


def _assert_run_persisted(db, run_id: str) -> None:
    conn = db._get_conn()
    row = conn.execute(
        "SELECT trigger, status FROM ai_runs WHERE id = ?", (run_id,)
    ).fetchone()
    conn.close()
    if row is None or row["trigger"] != "curator" or row["status"] != "success":
        raise RuntimeError("successful curator trace was not persisted to ai_runs")


async def propose_batch(db, repository, *, channel_id: str, preset,
                        curator_name: str = CURATOR_NAME,
                        limit: int = 50,
                        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
                        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
                        repair: bool = True,
                        auto_apply: bool = False) -> dict:
    """跑一个 curator 批次。返回 proposal dict（与 CLI dry-run 输出同构）。

    auto_apply=True 时对通过校验的 batch 立即走 repository 事务 apply
    （含空 operations——空批 apply 只推进 cursor）。
    """
    from bot.ai_engine_openai_compat import simple_completion

    channel_id = str(channel_id)
    cursor = repository.get_cursor(curator_name, channel_id)
    after_id = int(cursor["last_message_id"])
    messages = load_curator_interval(
        db, channel_id=channel_id, after_message_id=after_id, limit=limit)
    if not messages:
        return {
            "mode": "dry-run",
            "status": "no_messages",
            "cursor": after_id,
            "operations": [],
        }
    upto_id = int(messages[-1]["id"])
    visible_ids = {int(message["id"]) for message in messages}
    memories = repository.list(status="active")
    system_text, task_prompt = build_curator_prompt(
        messages=messages, memories=memories)

    raw, run_id = await simple_completion(
        task_prompt, preset, trigger="curator", db=db, return_run_id=True,
        system_text=system_text,
        max_output_tokens=max_output_tokens, request_timeout=request_timeout)
    _assert_run_persisted(db, run_id)

    def _parse_and_validate(output: str):
        batch = parse_curator_batch(output)
        repository.validate_curator_batch(
            batch,
            curator_name=curator_name,
            channel_id=channel_id,
            after_message_id=after_id,
            upto_message_id=upto_id,
            visible_message_ids=visible_ids,
        )
        return batch

    repaired = False
    try:
        batch = _parse_and_validate(raw)
    except ValueError as first_error:
        if not repair:
            raise
        logger.warning(f"⚠️ curator 输出未通过校验，定向修正一次: {first_error}")
        repair_prompt = build_curator_repair_prompt(
            task_prompt, raw, validation_error=str(first_error))
        raw, run_id = await simple_completion(
            repair_prompt, preset, trigger="curator", db=db, return_run_id=True,
            system_text=system_text,
            max_output_tokens=max_output_tokens, request_timeout=request_timeout)
        _assert_run_persisted(db, run_id)
        batch = _parse_and_validate(raw)
        repaired = True

    result = {
        "mode": "dry-run",
        "status": "validated",
        "run_id": run_id,
        "curator": curator_name,
        "preset": preset.name,
        "model": preset.model,
        "channel_id": channel_id,
        "after_message_id": after_id,
        "upto_message_id": upto_id,
        "visible_message_count": len(messages),
        "repaired": repaired,
        **batch.to_dict(),
    }
    if auto_apply:
        apply_result = repository.apply_curator_batch(
            batch,
            curator_name=curator_name,
            channel_id=channel_id,
            after_message_id=after_id,
            upto_message_id=upto_id,
            visible_message_ids=visible_ids,
            run_id=run_id,
            curator_model=preset.model,
        )
        result["mode"] = "auto-apply"
        result["apply_result"] = apply_result
    return result
