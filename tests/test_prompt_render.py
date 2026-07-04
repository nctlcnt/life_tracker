"""Golden parity test（LT-129 核心不变量）：

用旧结构化 section 数据走新渲染器（运行时 fallback 合成 + 显式迁移合成两条路径），
输出必须与冻结的旧四层拼装逐字节一致——保证迁移部署当晚 cache 前缀不失效、
模型看到的 prompt 零变化。
"""
import json
from pathlib import Path

import pytest

from bot.prompts import LEGACY_STRUCTURED_KEYS, build_prompt, synthesize_main_template
from tests.legacy_reference import legacy_from_formatted

REPO_ROOT = Path(__file__).resolve().parents[1]


def _default_sections() -> dict[str, str]:
    data = json.loads((REPO_ROOT / "docs" / "default-prompts.json").read_text())
    return dict(data["sections"])


SYNTHETIC_SECTIONS = {
    "identity": "我是日和 (⁠◕⁠ᴗ⁠◕⁠✿⁠)，说话末尾偶尔带颜文字 {>_<}",
    "user_model": "用户是留学生",
    "system_mechanics": "",  # 空散文段：旧拼装会跳过，新合成也必须跳过
    "communication": "语气轻松",
    "protocols": "",
    "tools": 'set_reminder 参数示例：{"action": "喝水", "trigger_time": "2026-01-01"}',
}

FULL_DATA = dict(
    memories=[{"id": 1, "content": "在准备路考"}, {"id": 7, "content": "ADHD 评估进行中"}],
    relevant_history=[{"embedding_context": "user: 上次聊到的睡眠问题\nassistant: 建议早点睡"}],
    today_timeline=[{"id": 3, "start_time": "09:00", "end_time": "10:00",
                     "category": "study", "project_name": "COMP9021",
                     "content": "写作业", "notes": "比较顺利"}],
    weather="晴 22°C",
    calendar="明天 10:00 牙医",
    deadlines=[{"id": 2, "title": "作业 due", "due_time": "2026-12-31T23:59:00"}],
    projects=[{"project_name": "COMP9021"}, {"project_name": "驾照"}],
    pending_reminders=[{"id": 5, "trigger_time": "2026-12-01T09:00:00",
                        "priority": "high", "action": "交作业", "group_id": "g1"}],
)

DATA_VARIANTS = {
    "full": FULL_DATA,
    "no_memories": {**FULL_DATA, "memories": None},
    "no_relevant_history": {**FULL_DATA, "relevant_history": None},
    "no_weather_calendar": {**FULL_DATA, "weather": None, "calendar": None},
    "no_deadlines_reminders": {**FULL_DATA, "deadlines": None, "pending_reminders": None},
    "all_empty": {k: None for k in FULL_DATA},
}


def _assert_parity(sections: dict[str, str], data: dict) -> None:
    p = build_prompt("chat", sections=sections, **data)
    stripped = {k: (sections.get(k) or "").strip()
                for k in (*LEGACY_STRUCTURED_KEYS, "tools")}
    # 复用 p.values（同一份 _format_* 输出），oracle 只对比"拼装"差异，
    # 也避免 format_countdown 两次调用跨时间边界的偶发不一致
    legacy = legacy_from_formatted(stripped, p.values)

    legacy_blocks = [t for t in (legacy.static_text(), legacy.stable_context_text(),
                                 legacy.memories_text(), legacy.dynamic_text()) if t]
    assert [t for _, t in p.render_blocks()] == legacy_blocks
    assert p.to_claude_blocks() == legacy.to_claude_blocks()
    assert p.flatten() == legacy.flatten()
    assert p.concise().flatten() == legacy.concise().flatten()
    assert p.concise().to_claude_blocks() == legacy.concise().to_claude_blocks()


@pytest.mark.parametrize("data_name", DATA_VARIANTS)
@pytest.mark.parametrize("sections_name", ["defaults", "synthetic"])
def test_runtime_fallback_parity(sections_name: str, data_name: str):
    """main_template 为空 → build_prompt 现场合成，结果与旧拼装逐字节一致。"""
    sections = _default_sections() if sections_name == "defaults" else dict(SYNTHETIC_SECTIONS)
    sections.pop("main_template", None)
    _assert_parity(sections, DATA_VARIANTS[data_name])


@pytest.mark.parametrize("data_name", DATA_VARIANTS)
@pytest.mark.parametrize("sections_name", ["defaults", "synthetic"])
def test_migrated_template_parity(sections_name: str, data_name: str):
    """main_template = 迁移合成结果（模拟迁移后的 DB），与旧拼装逐字节一致。"""
    sections = _default_sections() if sections_name == "defaults" else dict(SYNTHETIC_SECTIONS)
    stripped = {k: (sections.get(k) or "").strip() for k in sections}
    sections["main_template"] = synthesize_main_template(stripped)
    _assert_parity(sections, DATA_VARIANTS[data_name])
