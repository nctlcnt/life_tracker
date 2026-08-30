"""Golden parity test（LT-129 核心不变量）：

用旧结构化 section 数据走新渲染器（运行时 fallback 合成 + 显式迁移合成两条路径），
输出必须与冻结的旧四层拼装逐字节一致——保证迁移部署当晚 cache 前缀不失效、
模型看到的 prompt 零变化。

LT-156 把 today_timeline/pending_reminders/deadlines/projects 四个占位符的
【】标题从 _format_* 的返回值里挪进了模板字面文本，_relabel() 按新的挪法把
标题重新贴回 legacy oracle 用的 values，并显式编码 tier 抬升处新增的那一个
空行——之后仍要求逐字节相等，只是"一致"的基准换成了 LT-156 之后的新拼装。
"""
import json
from pathlib import Path

import pytest

from bot.prompts import (
    LABEL_DEADLINES,
    LABEL_PENDING_REMINDERS,
    LABEL_PROJECTS,
    LABEL_TODAY_TIMELINE,
    LEGACY_STRUCTURED_KEYS,
    build_prompt,
    synthesize_main_template,
)
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


def _relabel(values: dict[str, str]) -> dict[str, str]:
    """把 LT-156 挪进 synthesize_main_template() 字面文本的四个标题，
    按 _format_* 现在的（不含标题）输出重新拼回 legacy oracle 需要的形态。

    synthesize_main_template() 的占位符顺序 tier 严格递增
    （tools=1 → projects=2 → memories/relevant_history=3 → today_timeline=4），
    每次 tier 抬升时，紧贴在下一个占位符前的标题字面文本会被 render_blocks()
    并进上一段、strip 掉结尾换行，值本身另起一段——flatten() 拼接时两段之间
    用 "\n\n" 相连，标题和内容之间就从 1 个换行变成 2 个。这不是 bug，是把
    标题挪进可编辑模板文本后、cache-tier 分段机制的必然结果（LT-156）。
    projects/today_timeline 前面正好各有一次 tier 抬升，要用双换行；
    pending_reminders/deadlines 前面 tier 不变，单换行不受影响。
    """
    out = dict(values)
    if out.get("projects"):
        out["projects"] = f"{LABEL_PROJECTS}\n\n{out['projects']}"
    if out.get("today_timeline"):
        out["today_timeline"] = f"{LABEL_TODAY_TIMELINE}\n\n{out['today_timeline']}"
    if out.get("pending_reminders"):
        out["pending_reminders"] = f"{LABEL_PENDING_REMINDERS}\n{out['pending_reminders']}"
    if out.get("deadlines"):
        out["deadlines"] = f"{LABEL_DEADLINES}\n{out['deadlines']}"
    return out


def _assert_parity(sections: dict[str, str], data: dict) -> None:
    p = build_prompt("chat", sections=sections, **data)
    stripped = {k: (sections.get(k) or "").strip()
                for k in (*LEGACY_STRUCTURED_KEYS, "tools")}
    # 复用 p.values（同一份 _format_* 输出，现在不含 LT-156 挪走的四个标题），
    # 重新贴上标题（并按 tier 抬升位置补上 render_blocks() 会产生的空行）后
    # 喂给 oracle：只对比"拼装"差异，也避免 format_countdown 两次调用跨时间
    # 边界的偶发不一致
    legacy = legacy_from_formatted(stripped, _relabel(p.values))

    # 不再逐段比较 render_blocks()：projects/today_timeline 紧跟在 tier 抬升
    # 之前的标题字面文本，现在被并进上一段、结尾换行被 strip 掉，值本身
    # 另起一段——4 个固定 legacy 桶（static/stable_context/memories/dynamic）
    # 假设标题和值永远同段，这个假设不再成立。flatten() 把所有段落用 "\n\n"
    # 重新拼接，段边界落在哪一段不影响模型最终看到的文本，才是真正的行为契约。
    assert p.flatten() == legacy.flatten()
    assert p.concise().flatten() == legacy.concise().flatten()


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


@pytest.mark.parametrize(
    "field, label",
    [
        ("pending_reminders", LABEL_PENDING_REMINDERS),
        ("deadlines", LABEL_DEADLINES),
        ("today_timeline", LABEL_TODAY_TIMELINE),
        ("projects", LABEL_PROJECTS),
    ],
)
def test_empty_context_lists_still_say_so(field, label):
    """空列表必须留下痕迹，不能让整段从 prompt 里消失。

    工具策略教模型「看【待触发的 Reminder】列表，那才是真相」。段落一旦消失，
    模型分不清「确实没有」和「没告诉我」，就会按策略去 list_reminders 复查，
    白白多一次调用。

    标题现在是模板字面文本，不是 _format_* 的返回值；today_timeline/projects
    前面各有一次 tier 抬升，标题和内容之间会多一个空行（见 _relabel()），
    所以这里取标题后第一个非空行，不假设固定是第几行。
    """
    sections = _default_sections()
    sections.pop("main_template", None)
    rendered = build_prompt(
        "chat", sections=sections, **{**FULL_DATA, field: None}
    ).flatten()

    assert label in rendered
    remainder_lines = rendered.split(label, 1)[1].splitlines()
    segment = next(line for line in remainder_lines if line.strip())
    assert "没有" in segment or "还没有" in segment or segment.strip() == "- 无"
