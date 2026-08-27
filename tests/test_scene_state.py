"""场景状态的持久化与终止契约。

设计文档第 5.3 节规定：场景描述生成一次，普通聊天只读取；唯一终止
条件是下一个 check-in。这里把这类不会报错、只会让语气悄悄跑偏的行为钉死。
"""
from datetime import datetime, timedelta
import json

import pytest

from bot.database import Database
from bot.memory import scene_state


CHANNEL = "channel-1"
T0 = datetime(2026, 8, 24, 20, 0, 0)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "scene.db"))


def _start(db, now=T0, **kwargs):
    fields = {
        "check_in_name": "interview_evening",
        "description": "今天的晚间采访，刚问了晚饭吃什么，她说了麻辣烫。",
    }
    fields.update(kwargs)
    return scene_state.start(db, CHANNEL, now=now, **fields)


def test_start_then_load_roundtrips(db):
    started = _start(db)
    loaded = scene_state.load(db, CHANNEL, T0)

    assert loaded == started
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


def test_prompt_block_marks_itself_as_system_generated(db):
    """必须标明这是系统标记，不是用户说过的话。"""
    block = _start(db).as_prompt_block()

    assert "系统标记" in block
    assert "不是她说的话" in block
    assert "麻辣烫" in block


def test_start_overwrites_the_previous_scene(db):
    """新 check-in 建立的场景直接覆盖旧场景。"""
    _start(db)

    replaced = _start(
        db,
        now=T0 + timedelta(hours=3),
        check_in_name="random_poll",
        description="随手聊两句。",
    )

    assert replaced.check_in_name == "random_poll"
    assert scene_state.load(db, CHANNEL, T0 + timedelta(hours=3)) == replaced


def test_load_is_read_only(db):
    """普通聊天读取场景，不能顺手刷新、计数或改写状态。"""
    _start(db)
    raw = db.get_state(f"scene:{CHANNEL}")

    assert scene_state.load(db, CHANNEL, T0 + timedelta(days=30)) is not None
    assert db.get_state(f"scene:{CHANNEL}") == raw


def test_scene_does_not_expire_by_elapsed_time(db):
    started = _start(db)

    assert scene_state.load(db, CHANNEL, T0 + timedelta(days=30)) == started


def test_legacy_budget_fields_are_ignored(db):
    """升级后保留已有场景，即使旧的轮数/静默预算已经耗尽。"""
    db.set_state(f"scene:{CHANNEL}", json.dumps({
        "check_in_name": "interview_evening",
        "description": "旧格式的场景",
        "turns": 5,
        "max_turns": 5,
        "idle_minutes": 45,
        "started_at": T0.isoformat(timespec="seconds"),
        "last_turn_at": T0.isoformat(timespec="seconds"),
    }, ensure_ascii=False))

    scene = scene_state.load(db, CHANNEL, T0 + timedelta(days=30))

    assert scene is not None
    assert scene.description == "旧格式的场景"
    assert scene.started_at == T0.isoformat(timespec="seconds")


def test_clear_ends_the_scene(db):
    _start(db)

    scene_state.clear(db, CHANNEL)

    assert scene_state.load(db, CHANNEL, T0) is None


@pytest.mark.parametrize("junk", ["", "not json", '{"unexpected": 1}', "[]"])
def test_corrupt_state_reads_as_no_scene(db, junk):
    """坏数据不能在 prompt 组装路径抛异常。"""
    db.set_state(f"scene:{CHANNEL}", junk)
    assert scene_state.load(db, CHANNEL, T0) is None
