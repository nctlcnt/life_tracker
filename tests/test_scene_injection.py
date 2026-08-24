"""场景状态怎么进入 prompt，以及来回数在哪里推进。

这两件事错了都不会报错：注入位置错只是悄悄打掉 cache，推进时机错只是让场景
比预算多活几轮。所以要钉住。
"""
from datetime import datetime, timedelta

import pytest

from bot.database import Database
from bot.memory import scene_state
from bot.prompts import PromptParts


CHANNEL = "channel-1"
T0 = datetime(2026, 8, 24, 20, 0, 0)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "inject.db"))


def _prompt():
    return PromptParts(
        mode="chat",
        template="人设正文。\n\n{memories}\n\n{deadlines}",
        values={"memories": "【记忆】- 喜欢编程", "deadlines": "【Deadline】- 无"})


# ── with_suffix ────────────────────────────────────────────────────────────

def test_suffix_lands_at_the_very_end(db):
    """必须追加到末尾。

    末尾属于最高 cache tier（动态数据段），所以场景变化不会让前面的稳定前缀
    失效。放在开头或中间会把整条前缀的 cache 打掉——这是个不会报错、
    只会让账单变贵的错误。
    """
    text = _prompt().with_suffix("【当前场景】晚间采访").flatten()

    assert text.rstrip().endswith("【当前场景】晚间采访")
    assert text.index("人设正文") < text.index("【当前场景】")


def test_suffix_does_not_mutate_the_original(db):
    """PromptParts 会被多处复用，追加必须返回副本。"""
    original = _prompt()
    original.with_suffix("【当前场景】晚间采访")

    assert "当前场景" not in original.flatten()


def test_empty_suffix_is_a_noop(db):
    original = _prompt()
    assert original.with_suffix("") is original
    assert original.with_suffix("   \n ") is original


def test_suffix_survives_concise(db):
    """中间轮改发 concise() 时场景不能消失——工具轮里同样需要知道在做什么。"""
    p = _prompt().with_suffix("【当前场景】晚间采访")
    assert "【当前场景】晚间采访" in p.concise().flatten()


# ── 来回数在哪里推进 ────────────────────────────────────────────────────────

def test_turn_is_counted_even_when_the_reply_fails(db):
    """来回数必须在调用**之前**推进。

    放在成功分支里会让失败的轮次不计数，场景实际存活时间超出预算。
    这里直接测 scene_state 的语义：touch 一次就是一个来回，与结果无关。
    """
    scene_state.start(db, CHANNEL, check_in_name="interview_evening",
                      description="晚间采访", now=T0, max_turns=2)

    first = scene_state.touch(db, CHANNEL, T0 + timedelta(minutes=1))
    assert first.turns == 1

    # 第二次用尽预算
    assert scene_state.touch(db, CHANNEL, T0 + timedelta(minutes=2)) is None


def test_no_scene_means_no_suffix(db):
    """没有场景时 prompt 不应该多出任何东西——空标题会让模型以为
    "有个场景但没告诉我"，凭空制造不确定感。"""
    assert scene_state.load(db, CHANNEL, T0) is None

    scene = scene_state.load(db, CHANNEL, T0)
    text = _prompt().flatten() if scene is None else \
        _prompt().with_suffix(scene.as_prompt_block()).flatten()

    assert "当前场景" not in text


# ── 转发链完整性 ────────────────────────────────────────────────────────────

def test_every_layer_of_scheduled_action_accepts_the_same_kwargs():
    """三层 scheduled_action 的关键字参数必须一致。

    调用链是 ai_engine（路由 + fallback）→ ai_engine_openai_compat（引擎适配）
    → ai_engine_base（实现）。给它加参数要改三处，漏掉中间那层不会有任何
    静态错误——直到真的触发一次 check-in 才炸 TypeError。

    2026-08-24 加 track_scene 时就漏了适配层，部署之后才在生产上暴露。
    """
    import inspect

    from bot import ai_engine, ai_engine_base, ai_engine_openai_compat

    def kwargs_of(fn):
        return {
            name for name, p in inspect.signature(fn).parameters.items()
            if p.kind is inspect.Parameter.KEYWORD_ONLY
            or (p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and p.default is not inspect.Parameter.empty)
        }

    router = kwargs_of(ai_engine.scheduled_action)
    adapter = kwargs_of(ai_engine_openai_compat.scheduled_action)
    base = kwargs_of(ai_engine_base.scheduled_action)

    # 路由层的每个可选参数都必须能一路传到底
    assert router <= adapter, f"适配层缺少: {sorted(router - adapter)}"
    assert router <= base, f"实现层缺少: {sorted(router - base)}"
