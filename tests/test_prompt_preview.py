"""Read-only effective-prompt preview contracts."""

import asyncio
from datetime import datetime

from api import server
from bot.async_pipeline.worker_prompts import (
    build_chat_only_system,
    build_result_expression_system,
    build_tool_worker_system,
)
from bot.database import Database
from bot.memory import MemoryService
from bot.prompt_preview import build_prompt_preview


NOW = datetime(2026, 8, 30, 12, 30, 0)


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "prompt-preview.db"))
    db.set_prompt_section(
        "main_template",
        "PERSONA MARKER\n\n{tools}\n\n{projects}\n\n{memories}\n\n"
        "{today_timeline}\n\n{pending_reminders}\n\n{deadlines}",
    )
    db.set_prompt_section("tools", "BUSINESS TOOL POLICY MARKER")
    return db


def _by_key(preview: dict) -> dict:
    return {item["key"]: item for item in preview["tracks"]}


def test_preview_uses_the_production_builders_without_changing_track_content(tmp_path):
    db = _db(tmp_path)
    memory = MemoryService(db)

    preview = build_prompt_preview(
        db,
        memory_service=memory,
        now=NOW,
        tool_worker_enabled=True,
        tool_worker_apply=True,
    )
    tracks = _by_key(preview)

    assert tracks["chat"]["system_prompt"] == build_chat_only_system(
        db, memory_service=memory, now=NOW
    )
    assert tracks["execution"]["system_prompt"] == build_tool_worker_system(
        db, now=NOW
    )
    assert tracks["result_expression"][
        "system_prompt"
    ] == build_result_expression_system(db, memory_service=memory, now=NOW)

    assert "PERSONA MARKER" in tracks["chat"]["system_prompt"]
    assert "BUSINESS TOOL POLICY MARKER" not in tracks["chat"]["system_prompt"]
    assert tracks["chat"]["tool_schemas"] == []

    assert "PERSONA MARKER" not in tracks["execution"]["system_prompt"]
    assert "BUSINESS TOOL POLICY MARKER" in tracks["execution"]["system_prompt"]
    assert tracks["execution"]["tool_schemas"]

    assert "PERSONA MARKER" in tracks["result_expression"]["system_prompt"]
    assert tracks["timeline_renderer"]["status"] == "planned"
    assert tracks["timeline_renderer"]["system_prompt"] == ""


def test_preview_renders_the_selected_check_in_as_the_last_user_message(tmp_path):
    db = _db(tmp_path)
    memory = MemoryService(db)
    check_in_id = db.create_check_in(
        name="preview_probe",
        label="Preview probe",
        enabled=True,
        schedule_type="window",
        time_start="12:00",
        time_end="13:00",
        prompt_template="{label} at {timestamp}: {instructions}",
        instructions="look closely",
        tool_profile="poll",
    )

    preview = build_prompt_preview(
        db,
        memory_service=memory,
        check_in_id=check_in_id,
        now=NOW,
        tool_worker_enabled=True,
        tool_worker_apply=True,
    )
    track = _by_key(preview)["check_in"]

    assert preview["selected_check_in_id"] == check_in_id
    assert track["status"] == "active"
    assert track["tool_schemas"] == []
    assert track["messages"] == [{
        "role": "user",
        "label": "check_ins.preview_probe.prompt_template",
        "content": (
            "[check_in:preview_probe]\n"
            "Preview probe at 2026-08-30 12:30: look closely"
        ),
    }]


def test_preview_does_not_expire_deadlines_or_write_application_state(tmp_path):
    db = _db(tmp_path)
    memory = MemoryService(db)
    deadline_id = db.add_deadline("old deadline", "2020-01-01T00:00:00")

    build_prompt_preview(
        db,
        memory_service=memory,
        now=NOW,
        tool_worker_enabled=True,
        tool_worker_apply=True,
    )

    conn = db._get_conn()
    row = conn.execute(
        "SELECT status FROM deadlines WHERE id = ?", (deadline_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "active"


def test_admin_preview_endpoint_returns_the_manifest(tmp_path):
    db = _db(tmp_path)
    memory = MemoryService(db)
    original_db = server.db
    original_memory = server.memory
    server.set_database(db, memory)
    try:
        response = asyncio.run(server.admin_preview_prompts())
    finally:
        server.db = original_db
        server.memory = original_memory

    assert response["read_only"] is True
    assert [item["key"] for item in response["tracks"]] == [
        "chat",
        "check_in",
        "execution",
        "result_expression",
        "timeline_renderer",
    ]
    assert response["selected_check_in_id"] is not None
    inventory = {
        item["key"]: item for item in response["section_inventory"]["sections"]
    }
    assert inventory["main_template"]["status"] == "runtime"
    assert inventory["identity"]["status"] == "fallback"
    assert inventory["morning"]["status"] == "seed_only"
    assert inventory["dispatch_paraphrase_task"]["status"] == "unused"


def test_admin_prompt_editor_only_exposes_runtime_sections(tmp_path):
    db = _db(tmp_path)
    memory = MemoryService(db)
    original_db = server.db
    original_memory = server.memory
    server.set_database(db, memory)
    try:
        response = asyncio.run(server.admin_list_prompts())
    finally:
        server.db = original_db
        server.memory = original_memory

    visible = {
        item["key"] for item in response["sections"] if not item["hidden"]
    }
    assert visible == {
        "main_template",
        "tool_worker_template",
        "tools",
        "reminder",
        "weather_report",
    }

    inventory_only = {
        item["key"] for item in response["sections"] if item["hidden"]
    }
    assert "morning" in inventory_only
    assert "dispatch_paraphrase_task" in inventory_only


def test_tool_worker_template_default_is_savable_through_admin():
    """LT-156：tool_worker_template 复用 main_template 的占位符校验规则
    （api.server._validate_prompt_template）。默认内容里带一整段 JSON 输出
    契约，回归防的是花括号被误判成不认识的占位符，导致这个 section 一保存
    就 400，Admin 里再也编辑不了。"""
    from api.server import _validate_prompt_template
    from bot.async_pipeline.worker_prompts import DEFAULT_TOOL_WORKER_TEMPLATE

    _validate_prompt_template("tool_worker_template", DEFAULT_TOOL_WORKER_TEMPLATE)
