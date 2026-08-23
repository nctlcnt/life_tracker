"""按注入权限分档读取记忆——本次改造把不变量落在读取侧的执行点。

设计的核心是：推断类内容照常写进数据库，靠**读取时的权限分档**拦住它被当成
事实使用。所以这组测试守的不是"能不能查出来"，而是"查出来的东西有没有被
放进正确的档位"。放错档位不会报错，只会让 bot 用错误的确定性说话。
"""
import pytest

from bot.database import Database
from bot.memory import status_ladder as sl
from bot.memory.personal_repository import PersonalMemoryRepository
from bot.prompts import LABEL_MEMORIES, format_memory_tiers


@pytest.fixture
def repository(tmp_path):
    return PersonalMemoryRepository(Database(str(tmp_path / "tiers.db")))


def _seed(repository, claim, status, memory_type="general"):
    return repository.create_onboarding_seed(
        claim=claim, reason="测试", memory_type=memory_type, status=status)


# ── 查询按权限，不按 status ────────────────────────────────────────────────

def test_only_confirmed_lands_in_the_assert_tier(repository):
    """能被当事实用的只有 confirmed。这条一旦松掉，写入侧放行的推断
    就会直接以事实身份进 prompt，整套设计的不变量就落空了。"""
    _seed(repository, "喜欢编程", sl.CONFIRMED)
    _seed(repository, "可能在准备转方向", sl.PROVISIONAL)
    _seed(repository, "也许喜欢爵士", sl.HYPOTHESIS)

    asserted = repository.list_by_permission(sl.ASSERT)

    assert [m["summary"] for m in asserted] == ["喜欢编程"]


def test_provisional_lands_in_the_hedge_tier(repository):
    _seed(repository, "可能在准备转方向", sl.PROVISIONAL)
    _seed(repository, "喜欢编程", sl.CONFIRMED)

    hedged = repository.list_by_permission(sl.HEDGE)

    assert [m["summary"] for m in hedged] == ["可能在准备转方向"]


def test_hypothesis_is_probe_only_and_never_reaches_either_visible_tier(repository):
    """`hypothesis` 只能用来生成确认问题。它既不能当事实，也不能带限定语引用——
    后者同样是在向用户复述一个没有依据的说法。"""
    _seed(repository, "也许喜欢爵士", sl.HYPOTHESIS)

    assert repository.list_by_permission(sl.ASSERT) == []
    assert repository.list_by_permission(sl.HEDGE) == []
    assert len(repository.list_by_permission(sl.PROBE_ONLY)) == 1


def test_superseded_is_hidden_from_every_visible_tier(repository):
    old = _seed(repository, "住在墨尔本", sl.CONFIRMED)
    new = _seed(repository, "住在悉尼", sl.CONFIRMED)
    repository.set_status(old, sl.SUPERSEDED, superseded_by=new)

    assert [m["summary"] for m in repository.list_by_permission(sl.ASSERT)] == \
        ["住在悉尼"]


def test_disputed_is_conservatively_kept_out_of_visible_tiers(repository):
    """`disputed` 的权限细则（软冲突 probe_only / 已否认 hidden）尚未定，
    「什么算用户已明确否认」还缺一个可机械判定的标准。在它定下来之前，
    一律按未否认处理，也就是不进任何可见档——保守方向。"""
    _seed(repository, "有争议的说法", sl.DISPUTED)

    assert repository.list_by_permission(sl.ASSERT) == []
    assert repository.list_by_permission(sl.HEDGE) == []


def test_unknown_permission_raises(repository):
    with pytest.raises(ValueError, match="unknown permission"):
        repository.list_by_permission("assert_maybe")


# ── 渲染：两档必须分块 ─────────────────────────────────────────────────────

def test_two_tiers_render_as_separate_blocks(repository):
    """两档必须是两个块。拼成一段文本的话，模型就没有依据区分
    「已确认的事实」和「有支持但没确认的印象」，读取侧的把关就白做了。"""
    text = format_memory_tiers(
        asserted=[{"id": 1, "summary": "喜欢编程"}],
        hedged=[{"id": 2, "summary": "可能在准备转方向"}],
        display_name="Cece")

    assert LABEL_MEMORIES in text
    assert "【还不确定的印象】" in text
    # 两个标题之间必须有空行分隔，不能挨着
    assert "\n\n【还不确定的印象】" in text
    # hedge 档要显式要求带限定语
    assert "限定语" in text


def test_hedge_block_disappears_when_empty(repository):
    """没有 hedge 档记忆时不应该留一个空标题——空标题会让模型以为
    "有一些不确定的印象但没告诉我"，凭空制造不确定感。"""
    text = format_memory_tiers(
        asserted=[{"id": 1, "summary": "喜欢编程"}], hedged=[],
        display_name="Cece")

    assert "【还不确定的印象】" not in text


def test_everything_empty_renders_nothing(repository):
    assert format_memory_tiers(asserted=[], hedged=[], display_name="Cece") == ""


# ── 称呼只在标题里 ─────────────────────────────────────────────────────────

def test_display_name_appears_only_in_headers_not_in_items(repository):
    """称呼出现在块标题里，条目本身不带主语。

    这是「改称呼 = 改一个配置值」的前提：一旦称呼被写进 claim 文本，
    换一个叫法就变成全表重写，而且要教会 curator 输出占位符、
    再加 validator 拦截漏网的字面量。
    """
    text = format_memory_tiers(
        asserted=[{"id": 1, "summary": "喜欢编程"},
                  {"id": 2, "summary": "住在悉尼"}],
        hedged=[], display_name="Cece")

    item_lines = [ln for ln in text.splitlines() if ln.startswith("- [id=")]
    assert item_lines
    for line in item_lines:
        assert "Cece" not in line


def test_changing_the_display_name_touches_only_the_header(repository):
    items = [{"id": 1, "summary": "喜欢编程"}]
    a = format_memory_tiers(asserted=items, hedged=[], display_name="Cece")
    b = format_memory_tiers(asserted=items, hedged=[], display_name="阿猫")

    # 条目部分逐字节相同，只有标题变了
    assert [ln for ln in a.splitlines() if ln.startswith("- [id=")] == \
        [ln for ln in b.splitlines() if ln.startswith("- [id=")]
    assert "Cece" in a and "阿猫" in b
