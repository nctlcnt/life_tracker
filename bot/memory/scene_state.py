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
`start()` 覆盖式写入；普通聊天只读取，不重新生成也不改文本。

**三、唯一终止条件是下一个 check-in。** 新 check-in 先清除旧场景；如果它
启用了 `track_scene`，工具轮再写入新场景。不按静默时间或来回数清除。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

_STATE_KEY_PREFIX = "scene:"


@dataclass(frozen=True)
class Scene:
    """一个进行中的场景。"""

    check_in_name: str
    description: str
    started_at: str

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
          now: datetime) -> Scene:
    """开始一个新场景，覆盖旧的。

    覆盖是有意的：现有 check-in 互相就是彼此的终止条件（采访 15:59 触发，
    下一个 21:23，中间还夹着几次轻量 check-in），新的盖掉旧的即可，
    不需要谁去判断上一个结束了没有。
    """
    stamp = now.isoformat(timespec="seconds")
    scene = Scene(
        check_in_name=check_in_name,
        description=" ".join(str(description).split()),
        started_at=stamp,
    )
    db.set_state(_key(channel_id), json.dumps(scene.__dict__, ensure_ascii=False))
    return scene


def load(db, channel_id: str, now: datetime | None = None) -> Scene | None:
    """读取当前场景；``now`` 仅为兼容旧调用签名，不参与过期判断。"""
    raw = db.get_state(_key(channel_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        check_in_name = str(data.get("check_in_name") or "").strip()
        description = str(data.get("description") or "").strip()
        if not check_in_name or not description:
            return None
        # 旧状态中的 turns/max_turns/idle_minutes/last_turn_at 被有意忽略，
        # 因此升级不会丢掉正在进行的场景。
        return Scene(
            check_in_name=check_in_name,
            description=description,
            started_at=str(data.get("started_at") or ""),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def clear(db, channel_id: str) -> None:
    db.set_state(_key(channel_id), "")
