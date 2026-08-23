"""tool_result 之后注入的定向后置提示（build_tool_round_hint）。

这段文本每命中一次工具就追加一次。system 从第二轮起改发 concise()，
DB 的 tools section 不再重复投递，所以工具轮之后这段提示是中间轮唯一
还在传达工具使用规则的通道，内容和命中条件都要钉住。

注意 2026-04-13 的 34c65a0 曾以"AI 无法判断当前是否为最后一轮，中间轮
缺少 tool_guidelines 会导致行为不一致"为由取消过中间轮精简；随后的
de26cde 只为 Gemini 恢复，且保留了"最后一轮用全量 prompt"的例外。
当前实现没有这个例外，改为依赖本文件测的这些定向提示来补足。
"""
import pytest

from bot.prompts import TOOL_POST_HINTS, TOOL_ROUND_REMINDER, build_tool_round_hint
from bot.tools import POLL_TOOL_NAMES, REMINDER_TOOL_NAMES, TOOLS


def test_no_hint_returns_bare_reminder():
    assert build_tool_round_hint(["log_timeline_event"]) == TOOL_ROUND_REMINDER
    assert build_tool_round_hint([]) == TOOL_ROUND_REMINDER


def test_add_deadline_hint_points_at_memory_dedup():
    """建完 deadline 才能查 memory 里有没有重复条目——这是典型的
    只能在 tool_result 之后执行的规则。"""
    hint = build_tool_round_hint(["add_deadline"])

    assert hint.startswith(TOOL_ROUND_REMINDER)
    assert "delete_memory" in hint
    assert "【你现在记着的事】" in hint


def test_timeline_tools_have_no_post_hint():
    """timeline 类工具的"延续 vs 新建"是下笔前的判断，靠常驻的
    【今天完整 Timeline】在首轮完成；事后提醒既救不回已写的记录，
    又要在最高频工具上每次加价。"""
    for name in ("log_timeline_event", "update_timeline_event",
                 "delete_timeline_event"):
        assert name not in TOOL_POST_HINTS


def test_multiple_tools_accumulate_and_deduplicate():
    hint = build_tool_round_hint(
        ["add_deadline", "list_reminders", "add_deadline"])

    assert hint.count(TOOL_POST_HINTS["add_deadline"]) == 1
    assert TOOL_POST_HINTS["list_reminders"] in hint


@pytest.mark.parametrize("name", sorted(TOOL_POST_HINTS))
def test_hint_keys_are_real_tool_names(name):
    """防幽灵引用：post-hint 按实际调用的工具名匹配，键写错（或工具被删）
    就是一段永远不会触发的死文本。"""
    assert name in {t["function"]["name"] for t in TOOLS}


@pytest.mark.parametrize("profile", [POLL_TOOL_NAMES, REMINDER_TOOL_NAMES])
def test_add_deadline_hint_only_fires_where_delete_memory_exists(profile):
    """hint 让模型接着调 delete_memory。任何能触发这条 hint 的工具子集
    都必须同时提供 delete_memory，否则等于引导它调一个不在手上的工具。"""
    if "add_deadline" in profile:
        assert "delete_memory" in profile
