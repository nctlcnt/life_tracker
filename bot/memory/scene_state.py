"""当前对话场景状态。

## 解决什么

check-in 的模板（例如采访那份 2985 字）是作为**最后一条 user 消息**追加的，
不是 system 的一部分。所以它开口那一轮语气对（离生成位置最近），用户一回复，
模板被推远，system 里的工具策略重新变成最强信号，语气就掉回去。

这个模块存一句几十字的场景描述，让后续每一轮都还知道"我们正在做什么"，
而不必每轮重发整份模板。

## 三条设计约束

**一、场景描述不带语气。** 只描述在做什么（"今天的晚间采访，刚问了晚饭"），
不描述怎么说。语气由人设承担——两处都写就会各写一份、互相漂移。

**二、生成一次，永不重新生成。** 模型输出反复喂回自己的 prompt 会累积漂移：
第三轮的描述是对第二轮描述的转述。宁可稍微过时，不要漂。因此
`start()` 覆盖式写入，而 `touch()` 只推进计数，不改文本。

**三、终止靠有效期，不靠判断"场景结束了"。** 后者不可靠。三条机械条件：
新场景开始（覆盖）、静默超时、来回数用尽。用户中途换话题不做检测——
前三条至少有一条会先到，而且场景挂着也无害，它带的是场景不是任务清单。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

_STATE_KEY_PREFIX = "scene:"

# 默认有效期。都是参数，不是判断——定错了改数字，不用重新设计。
# 来回数沿用采访模板自己的口径（"一到三个来回就结束，最多不要超过五个"）：
# 那句话现在只在开口那一瞬间存在，这里让代码来记这个数。
DEFAULT_MAX_TURNS = 5
DEFAULT_IDLE_MINUTES = 45


@dataclass(frozen=True)
class Scene:
    """一个进行中的场景。"""

    check_in_name: str
    description: str
    turns: int
    max_turns: int
    idle_minutes: int
    started_at: str
    last_turn_at: str

    def is_expired(self, now: datetime) -> tuple[bool, str | None]:
        """返回（是否过期，原因）。原因用于日志和测试断言，不进 prompt。"""
        if self.turns >= self.max_turns:
            return True, "turns_exhausted"
        try:
            last = datetime.fromisoformat(self.last_turn_at)
        except (TypeError, ValueError):
            # 时间戳坏了就当过期：宁可丢一个场景，也不要让坏数据永久挂着。
            return True, "bad_timestamp"
        if now - last > timedelta(minutes=self.idle_minutes):
            return True, "idle_timeout"
        return False, None

    def as_prompt_block(self) -> str:
        """渲染进 prompt。

        必须标明这是系统给的场景标记，不是用户说过的话——与记忆系统那条
        "模型输出不是用户事实"是同一条纪律。这里风险低（只是个场景标签），
        但混进对话历史被当成用户原话就麻烦了。
        """
        return ("【当前场景】（系统标记，不是她说的话）\n"
                f"{self.description}")


def _key(channel_id: str) -> str:
    return f"{_STATE_KEY_PREFIX}{channel_id}"


def start(db, channel_id: str, *, check_in_name: str, description: str,
          now: datetime, max_turns: int = DEFAULT_MAX_TURNS,
          idle_minutes: int = DEFAULT_IDLE_MINUTES) -> Scene:
    """开始一个新场景，覆盖旧的。

    覆盖是有意的：现有 check-in 互相就是彼此的终止条件（采访 15:59 触发，
    下一个 21:23，中间还夹着几次轻量 check-in），新的盖掉旧的即可，
    不需要谁去判断上一个结束了没有。
    """
    stamp = now.isoformat(timespec="seconds")
    scene = Scene(
        check_in_name=check_in_name,
        description=" ".join(str(description).split()),
        turns=0, max_turns=int(max_turns), idle_minutes=int(idle_minutes),
        started_at=stamp, last_turn_at=stamp,
    )
    db.set_state(_key(channel_id), json.dumps(scene.__dict__, ensure_ascii=False))
    return scene


def load(db, channel_id: str, now: datetime) -> Scene | None:
    """取当前有效场景；已过期或不存在都返回 None。

    过期的场景**不在这里删**——读路径不写库。清理由 `touch()` 或下一次
    `start()` 覆盖完成。这样读取侧可以在任何地方安全调用，不必担心它有副作用。
    """
    raw = db.get_state(_key(channel_id))
    if not raw:
        return None
    try:
        scene = Scene(**json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    expired, _ = scene.is_expired(now)
    return None if expired else scene


def touch(db, channel_id: str, now: datetime) -> Scene | None:
    """记一个来回。返回推进后仍然有效的场景，用尽则清除并返回 None。

    **只推进计数，不改描述** —— 见模块开头的约束二。
    """
    scene = load(db, channel_id, now)
    if scene is None:
        clear(db, channel_id)
        return None
    advanced = Scene(
        check_in_name=scene.check_in_name,
        description=scene.description,
        turns=scene.turns + 1,
        max_turns=scene.max_turns,
        idle_minutes=scene.idle_minutes,
        started_at=scene.started_at,
        last_turn_at=now.isoformat(timespec="seconds"),
    )
    expired, _ = advanced.is_expired(now)
    if expired:
        clear(db, channel_id)
        return None
    db.set_state(_key(channel_id),
                 json.dumps(advanced.__dict__, ensure_ascii=False))
    return advanced


def clear(db, channel_id: str) -> None:
    db.set_state(_key(channel_id), "")
