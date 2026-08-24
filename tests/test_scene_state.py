"""场景状态的生命周期契约。

这组测试守的是设计文档第 5.3 节那三条约束。它们都是"错了不会报错、只会
让语气慢慢跑偏"的那类性质，所以必须钉死。
"""
from datetime import datetime, timedelta

import pytest

from bot.database import Database
from bot.memory import scene_state


CHANNEL = "channel-1"
T0 = datetime(2026, 8, 24, 20, 0, 0)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "scene.db"))


def _start(db, now=T0, **kwargs):
    fields = {"check_in_name": "interview_evening",
              "description": "今天的晚间采访，刚问了晚饭吃什么，她说了麻辣烫。"}
    fields.update(kwargs)
    return scene_state.start(db, CHANNEL, now=now, **fields)


# ── 基本读写 ────────────────────────────────────────────────────────────────

def test_start_then_load_roundtrips(db):
    started = _start(db)
    loaded = scene_state.load(db, CHANNEL, T0)

    assert loaded == started
    assert loaded.turns == 0
    assert "麻辣烫" in loaded.description


def test_no_scene_returns_none(db):
    assert scene_state.load(db, CHANNEL, T0) is None


def test_scenes_are_per_channel(db):
    _start(db)
    assert scene_state.load(db, "other-channel", T0) is None


def test_description_whitespace_is_normalized(db):
    """描述会被拼进 prompt，多余的换行会打乱那一段的结构。"""
    scene = _start(db, description="晚间采访，\n  刚问了   晚饭。\n")
    assert scene.description == "晚间采访， 刚问了 晚饭。"


# ── 约束一：不带语气（由渲染格式保证可辨识）────────────────────────────────

def test_prompt_block_marks_itself_as_system_generated(db):
    """必须标明这是系统标记，不是用户说过的话。

    与记忆系统那条"模型输出不是用户事实"是同一条纪律。这里风险低，
    但混进对话历史被当成用户原话就麻烦了。
    """
    block = _start(db).as_prompt_block()

    assert "系统标记" in block
    assert "不是她说的话" in block
    assert "麻辣烫" in block


# ── 约束二：生成一次，不重新生成 ────────────────────────────────────────────

def test_touch_advances_turns_without_touching_the_description(db):
    """推进来回数不能改描述。

    模型输出反复喂回自己的 prompt 会累积漂移：第三轮的描述是对第二轮描述的
    转述，两三轮之后跟原意无关。宁可稍微过时。
    """
    original = _start(db)

    after = scene_state.touch(db, CHANNEL, T0 + timedelta(minutes=1))

    assert after.turns == 1
    assert after.description == original.description
    assert after.started_at == original.started_at


def test_start_overwrites_the_previous_scene(db):
    """新场景直接覆盖旧的——现有 check-in 互相就是彼此的终止条件，
    不需要谁去判断上一个结束了没有。"""
    _start(db)
    scene_state.touch(db, CHANNEL, T0 + timedelta(minutes=1))

    replaced = _start(db, now=T0 + timedelta(hours=3),
                      check_in_name="random_poll", description="随手聊两句。")

    assert replaced.check_in_name == "random_poll"
    assert replaced.turns == 0          # 计数跟着新场景重置
    assert scene_state.load(db, CHANNEL, T0 + timedelta(hours=3)) == replaced


# ── 约束三：靠有效期，不靠判断 ──────────────────────────────────────────────

def test_scene_expires_after_the_turn_budget(db):
    """来回数用尽即结束。上限沿用采访模板自己的口径（最多五个来回）。"""
    _start(db, max_turns=2)
    now = T0

    for expected in (1, 2):
        now += timedelta(minutes=1)
        scene = scene_state.touch(db, CHANNEL, now)
        if expected < 2:
            assert scene.turns == expected

    # 第二个来回把预算用尽，场景应当已经消失
    assert scene_state.load(db, CHANNEL, now) is None


def test_scene_expires_after_idle_timeout(db):
    _start(db, idle_minutes=30)

    assert scene_state.load(db, CHANNEL, T0 + timedelta(minutes=29)) is not None
    assert scene_state.load(db, CHANNEL, T0 + timedelta(minutes=31)) is None


def test_idle_timeout_is_measured_from_the_last_turn_not_the_start(db):
    """持续在聊就不该超时——静默计时器每个来回都要重置。"""
    _start(db, idle_minutes=30)

    # 每 20 分钟一个来回，累计已经超过 30 分钟，但从没静默那么久
    now = T0
    for _ in range(3):
        now += timedelta(minutes=20)
        assert scene_state.touch(db, CHANNEL, now) is not None

    assert scene_state.load(db, CHANNEL, now + timedelta(minutes=10)) is not None
    assert scene_state.load(db, CHANNEL, now + timedelta(minutes=31)) is None


def test_expiry_reason_is_reported_for_diagnosis(db):
    scene = _start(db, max_turns=1, idle_minutes=30)

    assert scene.is_expired(T0) == (False, None)
    assert scene.is_expired(T0 + timedelta(minutes=31))[1] == "idle_timeout"


def test_load_does_not_write(db):
    """读路径必须无副作用——它会被读取侧到处调用，不能让它偷偷改状态。"""
    _start(db, idle_minutes=30)
    stale = T0 + timedelta(minutes=90)

    assert scene_state.load(db, CHANNEL, stale) is None
    # 原始记录还在（没被读操作删掉）；清理由 touch 或下次 start 负责
    assert db.get_state(f"scene:{CHANNEL}")


def test_touch_on_an_expired_scene_clears_it(db):
    _start(db, idle_minutes=30)

    assert scene_state.touch(db, CHANNEL, T0 + timedelta(minutes=90)) is None
    assert not db.get_state(f"scene:{CHANNEL}")


# ── 坏数据 ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("junk", ["", "not json", '{"unexpected": 1}', "[]"])
def test_corrupt_state_reads_as_no_scene(db, junk):
    """坏数据当作没有场景，不能抛异常——这条路径在组装 prompt 时被调用，
    炸在那里会连带打断整次回复。"""
    db.set_state(f"scene:{CHANNEL}", junk)
    assert scene_state.load(db, CHANNEL, T0) is None


def test_bad_timestamp_counts_as_expired(db):
    """时间戳坏了就当过期：宁可丢一个场景，也不要让坏数据永久挂着。"""
    _start(db)
    import json
    raw = json.loads(db.get_state(f"scene:{CHANNEL}"))
    raw["last_turn_at"] = "看不懂的时间"
    db.set_state(f"scene:{CHANNEL}", json.dumps(raw, ensure_ascii=False))

    assert scene_state.load(db, CHANNEL, T0) is None
