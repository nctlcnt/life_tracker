"""状态阶梯与注入权限的纯函数契约。

这两个函数承担了记忆系统改造后的核心不变量：**不变量在读取侧**——
推断类记忆照常写进数据库，靠权限拦住它被当成事实使用。所以这里每一条
都要钉死，不能靠"看代码显然是对的"。
"""
import pytest

from bot.memory import status_ladder as sl


# ── 注入权限映射 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("status, expected", [
    (sl.HYPOTHESIS, sl.PROBE_ONLY),
    (sl.PROVISIONAL, sl.HEDGE),
    (sl.CONFIRMED, sl.ASSERT),
    (sl.SUPERSEDED, sl.HIDDEN),
])
def test_permission_mapping_is_exact(status, expected):
    """2026-07-23 拍板的映射，逐条钉住。改这里等于改设计。"""
    assert sl.injection_permission(status) == expected


def test_disputed_permission_depends_on_whether_user_denied():
    """软冲突值得澄清一次；用户已明确否认的不再追问。"""
    assert sl.injection_permission(sl.DISPUTED) == sl.PROBE_ONLY
    assert sl.injection_permission(sl.DISPUTED, user_denied=True) == sl.HIDDEN


def test_every_ladder_status_has_a_permission():
    """五个状态都必须能查到权限——漏一个就会在读取侧抛异常，
    而那时候已经在组装 prompt 了，不是发现问题的好时机。"""
    for status in sl.LADDER_STATUSES:
        assert sl.injection_permission(status) in sl.PERMISSIONS


def test_unknown_status_raises_rather_than_defaulting():
    """未知状态必须炸，不能悄悄给个默认权限。

    给默认值的危险在于方向：默认成 assert 会让脏数据被当成事实，
    默认成 hidden 则会让记忆静默消失、且没人知道为什么。两种都比抛错糟。
    """
    with pytest.raises(ValueError, match="unknown status"):
        sl.injection_permission("active")      # 旧的三值之一
    with pytest.raises(ValueError, match="unknown status"):
        sl.injection_permission("archived")    # 已取消的状态


def test_only_confirmed_can_assert():
    """能被当作事实使用的只有 confirmed，一个都不能多。"""
    assertable = {s for s in sl.LADDER_STATUSES if sl.can_assert(s)}
    assert assertable == {sl.CONFIRMED}


# ── status 计算 ────────────────────────────────────────────────────────────

def test_confirmation_event_promotes_to_confirmed():
    assert sl.compute_status(
        basis="asserted", has_confirmation_event=True) == sl.CONFIRMED


def test_evidence_alone_never_reaches_confirmed():
    """证据积累不能替代用户确认——信任边界在状态阶梯上的具体形式。

    basis 已经是 asserted（用户直接表达过等价内容），但没有确认事件，
    仍然不能到 confirmed。
    """
    assert sl.compute_status(basis="asserted") != sl.CONFIRMED
    assert sl.compute_status(basis="supported") != sl.CONFIRMED
    assert sl.compute_status(basis="inferred") != sl.CONFIRMED


def test_confirmation_event_without_asserted_basis_does_not_promote():
    """确认事件与 basis 不一致时停在低档，不靠一个布尔值放行。

    确认事件意味着存在一条与 claim 基本等价的用户直接表达，那 basis 本就
    该是 asserted。两者对不上说明上游算错了。
    """
    assert sl.compute_status(
        basis="inferred", has_confirmation_event=True) != sl.CONFIRMED


def test_unresolved_conflict_beats_confirmation():
    """冲突未解决时不能断言，即使同时有确认事件。

    「用户确认过，但后来又出现矛盾证据」正是最该停下来问一次的情形；
    让 confirmed 赢会把矛盾直接吞掉。
    """
    assert sl.compute_status(
        basis="asserted", has_confirmation_event=True,
        has_unresolved_conflict=True) == sl.DISPUTED


def test_replacement_beats_everything():
    """已被替代是最硬的判定：位置让给新条目了，其余证据不再改变这个事实。"""
    assert sl.compute_status(
        basis="asserted", has_confirmation_event=True,
        has_unresolved_conflict=True, is_replaced=True) == sl.SUPERSEDED


def test_undecided_branch_falls_to_the_lowest_permission():
    """未决问题 4（hypothesis / provisional 分界）拍板前一律落到最低档。

    这条测试的作用是**标记未决**，不是固化结论：规则定下来之后它应该
    连同 UNDECIDED_DEFAULT_STATUS 一起改掉，而不是被绕过去。
    """
    assert sl.UNDECIDED_DEFAULT_STATUS == sl.HYPOTHESIS
    assert sl.injection_permission(sl.UNDECIDED_DEFAULT_STATUS) == sl.PROBE_ONLY
    # supported 和 inferred 现在无法区分——正是待决的那条线
    assert sl.compute_status(basis="supported") == \
        sl.compute_status(basis="inferred")


def test_unknown_basis_raises():
    with pytest.raises(ValueError, match="unknown basis"):
        sl.compute_status(basis="probably")


def test_compute_status_only_returns_ladder_values():
    """算出来的值必须在阶梯内——写进库之前不会再有别的地方兜住它。"""
    for basis in ("asserted", "supported", "inferred"):
        for confirmed in (False, True):
            for conflict in (False, True):
                for replaced in (False, True):
                    assert sl.compute_status(
                        basis=basis, has_confirmation_event=confirmed,
                        has_unresolved_conflict=conflict,
                        is_replaced=replaced) in sl.LADDER_STATUSES
