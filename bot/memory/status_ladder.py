"""记忆状态阶梯与注入权限。

设计依据：`docs/modules/memory/plan/memory-v4-design.md` 第 5 节。

这个模块只做两件事，都是纯函数，不碰数据库：

1. **算 status**：根据一条记忆的全部证据算出它的使用权等级；
2. **查注入权限**：由 status 唯一映射到「聊天能不能用、怎么用」。

为什么把它们放在一起又分成两个函数：它们是两个不同的问题。status 回答
「这条有多可信」，注入权限回答「据此允许做什么」。把权限直接算进 status
会让两者绑死，以后想调整权限口径就得改状态语义。

**本模块描述的是目标阶梯，数据库尚未切换。** 当前 `personal_memories.status`
的 CHECK 仍是 `active` / `superseded` / `archived` 三值（见
`personal_repository.MEMORY_STATUSES`）。切换是一次建表重写，单独进行。
在切换完成之前，这里的函数已经可以被调用和测试，只是算出来的值还写不进库。
"""
from __future__ import annotations

# ── 状态阶梯（五值）────────────────────────────────────────────────────────
HYPOTHESIS = "hypothesis"       # 证据只够合理猜想
PROVISIONAL = "provisional"     # 有明显支持，但没有用户确认事件
CONFIRMED = "confirmed"         # 存在用户确认事件
DISPUTED = "disputed"           # 有未解决的硬冲突，且尚无替代 claim
SUPERSEDED = "superseded"       # 已被新条目替代

LADDER_STATUSES = frozenset(
    {HYPOTHESIS, PROVISIONAL, CONFIRMED, DISPUTED, SUPERSEDED})

# ── 注入权限（四档）────────────────────────────────────────────────────────
ASSERT = "assert"           # 作为已确认信息直接使用
HEDGE = "hedge"             # 必须带限定语，且与 assert 分块
PROBE_ONLY = "probe_only"   # 不进事实段落，只能用来生成确认问题
HIDDEN = "hidden"           # 不进 prompt

PERMISSIONS = frozenset({ASSERT, HEDGE, PROBE_ONLY, HIDDEN})

# status → 权限。`disputed` 不在这里，因为它是唯一一个需要第二个输入的
# （见 injection_permission 的说明）。
_PERMISSION_BY_STATUS = {
    HYPOTHESIS: PROBE_ONLY,
    PROVISIONAL: HEDGE,
    CONFIRMED: ASSERT,
    SUPERSEDED: HIDDEN,
}

# ── 未决问题 4 的占位默认值 ─────────────────────────────────────────────────
# `hypothesis` 与 `provisional` 的分界规则尚未拍板（设计文档第 12 节未决问题 4，
# 候选方案见 memory_database_schema_plan.md 第 5.2 节）。
#
# 拍板之前一律落到 hypothesis，理由是它的权限最低（probe_only），
# 猜错的方向是「本该带限定语引用的记忆只被拿去提问」，代价是少说一句话；
# 反过来猜错则是「只有推断支撑的说法被当成用户提过的事实」，代价大得多。
#
# 这个常量刻意起了显眼的名字，方便拍板后 grep 出全部落点。
# **不要**把它替换成某个看起来像结论的阈值——那等于替人拍板。
UNDECIDED_DEFAULT_STATUS = HYPOTHESIS


def injection_permission(status: str, *, user_denied: bool = False) -> str:
    """由 status 查出注入权限。

    `user_denied` 只对 `disputed` 有意义：模型推断出的软冲突值得向用户澄清
    一次，权限 `probe_only`；用户已经明确否认过的，权限 `hidden`，不再追问，
    直到出现新证据才重新处理。

    注意「什么算用户已明确否认」还需要一个可机械判定的标准（设计文档第 12 节
    末尾的待确认细则）。在那之前调用方必须自己给出这个布尔值，本函数不猜。
    """
    if status not in LADDER_STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    if status == DISPUTED:
        return HIDDEN if user_denied else PROBE_ONLY
    return _PERMISSION_BY_STATUS[status]


def can_assert(status: str, *, user_denied: bool = False) -> bool:
    """这条记忆能不能被当作事实直接使用。

    单独给一个函数，是因为这是读取侧最常问的一个问题，
    也是最不能写错的一个——写成 `status != "hidden"` 之类的近似判断，
    就会把 hedge 和 probe_only 也放进事实段落。
    """
    return injection_permission(status, user_denied=user_denied) == ASSERT


def compute_status(
    *,
    basis: str,
    has_confirmation_event: bool = False,
    has_unresolved_conflict: bool = False,
    is_replaced: bool = False,
) -> str:
    """按一条记忆的**全部**证据算出 status。

    调用方必须传入重新汇总后的证据事实，不能拿旧 status 做增量修改——
    增量修改会让状态依赖历史路径，同样的证据在不同顺序下算出不同结果。

    参数：
      basis                  当前全部证据与 claim 的关系：
                             `asserted` / `supported` / `inferred`
      has_confirmation_event 是否存在用户确认事件。它有两条识别路径，
                             见设计文档第 5.1 节；判定在调用方，不在这里。
      has_unresolved_conflict 是否有未解决的硬冲突
      is_replaced            是否已被 supersede 操作替代

    判定顺序是有意为之，越靠前的越「硬」：
    """
    if basis not in {"asserted", "supported", "inferred"}:
        raise ValueError(f"unknown basis: {basis!r}")

    # 1. 已被替代最硬：位置已经让给新条目，其余证据都不再改变这个事实。
    if is_replaced:
        return SUPERSEDED

    # 2. 冲突未解决时不能断言，即使同时存在确认事件也不行。
    #    「用户确认过，但后来又出现了矛盾证据」正是最该停下来问一次的情形，
    #    让 confirmed 赢会把矛盾直接吞掉。
    if has_unresolved_conflict:
        return DISPUTED

    # 3. 只有用户确认事件能给出 confirmed。证据积累不能替代确认——
    #    这是信任边界在状态阶梯上的具体形式。
    #    同时要求 basis 是 asserted：确认事件意味着存在一条与 claim 基本
    #    等价的用户直接表达，那么 basis 本就该是 asserted。两者不一致时
    #    说明上游算错了，宁可停在低档也不要凭一个布尔值放行。
    if has_confirmation_event and basis == "asserted":
        return CONFIRMED

    # 4. 其余情形落在 hypothesis 和 provisional 之间，分界规则未定。
    return UNDECIDED_DEFAULT_STATUS
