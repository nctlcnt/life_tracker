"""LT-134 characterization tests：冻结 openai SDK 引擎的请求/响应契约。

production fallback preset (glm) 走这条路径（openai SDK + custom base_url）。
与 relay 的已知差异也在这里冻结，合并时必须显式决策取哪边：
- 请求参数用 max_completion_tokens（relay 用 max_tokens）
- assistant tool_calls 回填时从 SDK 对象重建 dict shape
- usage 解析含 prompt_tokens_details.cached_tokens
- OpenAIError → AIProviderError
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from openai import OpenAIError

from config import Preset
from bot import trace
import bot.ai_engine_openai as engine_openai
from bot.ai_provider_error import AIProviderError
from bot.database import Database
from bot.prompts import PromptParts, build_tool_round_hint
from bot.tools import get_tools


def _preset() -> Preset:
    return Preset(name="glm-test", provider="openai", api_key="sk-test",
                  base_url="https://glm.example.com/v1", model="glm-x")


def _prompt() -> PromptParts:
    return PromptParts(mode="chat", template="你是测试助手。", values={})


def _tool_call(call_id="call_1", name="list_reminders", arguments="{}"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(content=None, tool_calls=None, finish_reason="stop",
              prompt_tokens=100, completion_tokens=20, cached_tokens=0):
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls),
        finish_reason=finish_reason,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


class _FakeClient:
    def __init__(self, script, calls):
        async def create(**kwargs):
            calls.append(kwargs)
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=create))


@pytest.fixture
def openai_call(monkeypatch, tmp_path):
    """返回 run(script, **kwargs) → (result, calls)，自动清理 trace 上下文。"""
    db = Database(str(tmp_path / "openai_test.db"))

    def run(script, *, messages=None, send_callback=None,
            tool_callback=None, tool_names=None):
        calls: list[dict] = []
        client = _FakeClient(list(script), calls)
        monkeypatch.setattr(engine_openai, "_get_client",
                            lambda api_key, base_url: client)
        result = asyncio.run(engine_openai._call_with_tools(
            db, _prompt(),
            messages or [{"role": "user", "content": "[2026-07-17 10:00] 在吗"}],
            _preset(),
            send_callback=send_callback, tool_callback=tool_callback,
            tool_names=tool_names,
        ))
        return result, calls

    yield run
    trace._current.set(None)


def test_request_uses_max_completion_tokens(openai_call):
    result, calls = openai_call([_response(content="你好呀")])

    assert result == "你好呀"
    kwargs = calls[0]
    assert kwargs["model"] == "glm-x"
    assert kwargs["max_completion_tokens"] == 4096
    assert "max_tokens" not in kwargs
    assert kwargs["messages"][0] == {"role": "system", "content": "你是测试助手。"}
    assert kwargs["messages"][1] == {"role": "user",
                                     "content": "[2026-07-17 10:00] 在吗"}
    assert kwargs["tools"] == get_tools(None)


def test_empty_tool_names_omits_tools_key(openai_call):
    _, calls = openai_call([_response(content="ok")], tool_names=set())
    assert "tools" not in calls[0]


def test_tool_round_rebuilds_assistant_message_shape(openai_call):
    tool_rounds = []

    async def on_tools(names):
        tool_rounds.append(names)

    result, calls = openai_call(
        [_response(content="先查一下", tool_calls=[_tool_call()],
                   finish_reason="tool_calls"),
         _response(content="没有待办")],
        tool_callback=on_tools,
    )

    assert result == "先查一下\n没有待办"
    assert tool_rounds == [["list_reminders"]]

    msgs = calls[1]["messages"]
    # SDK 对象 → dict 重建，shape 必须是 OpenAI 官方 tool_calls 回传格式
    assert msgs[-3] == {
        "role": "assistant",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "list_reminders", "arguments": "{}"},
        }],
        "content": "先查一下",
    }
    expected_result = {"success": True, "reminders": [], "count": 0}
    assert msgs[-2] == {"role": "tool", "tool_call_id": "call_1",
                        "content": json.dumps(expected_result, ensure_ascii=False)}
    assert msgs[-1] == {"role": "user",
                        "content": build_tool_round_hint(["list_reminders"])}


def test_usage_parsing_includes_cached_tokens(openai_call):
    entry = trace.start(trigger="chat", model="glm-x", provider="openai",
                        prompt_parts=None, messages=[])
    openai_call([_response(content="ok", prompt_tokens=1200,
                           completion_tokens=30, cached_tokens=1024)])

    assert entry["rounds"][0]["usage"] == {
        "prompt_tokens": 1200,
        "completion_tokens": 30,
        "cached_tokens": 1024,
    }


def test_openai_error_raises_provider_error(openai_call):
    with pytest.raises(AIProviderError):
        openai_call([OpenAIError("upstream down")])


def test_round_cap_returns_internal_error_text(openai_call):
    script = [_response(tool_calls=[_tool_call()], finish_reason="tool_calls")
              for _ in range(5)]
    result, calls = openai_call(script)
    assert result == "（内部错误：工具调用次数过多）"
    assert len(calls) == 5
