"""LT-134 characterization tests：冻结 OpenAI-compatible 请求契约。

relay（raw httpx）是行为基线，production active preset (kiro) 走的就是这条
路径；统一引擎 ai_engine_openai_compat 必须满足完全相同的契约，因此本文件
对两个模块参数化跑同一套断言（relay 删除后收敛为只跑统一引擎）。冻结的内容：
- URL 拼接（use_v1_suffix）与 Bearer 鉴权头
- 请求 payload：model / max_tokens / system 消息在首位 / tools 子集过滤
- 多轮 tool calling 的消息回填 shape（assistant tool_calls → role:tool → round hint）
- <think> 块剥离、重复文本去重、send_callback / tool_callback 触发
- 错误规范化：非 200 / 网络错误 / 非 JSON → AIProviderError
- 5 轮上限兜底文案
- trace round 记录契约
"""
import asyncio
import json

import httpx
import pytest

from config import Preset
from bot import trace
import bot.ai_engine_openai_compat as openai_compat
import bot.ai_engine_relay as relay
from bot.ai_provider_error import AIProviderError
from bot.database import Database
from bot.prompts import PromptParts, build_tool_round_hint
from bot.tools import get_tools


def _preset(base_url="https://relay.example.com", use_v1_suffix=True) -> Preset:
    return Preset(name="kiro-test", provider="relay", api_key="sk-test",
                  base_url=base_url, model="claude-x", use_v1_suffix=use_v1_suffix)


def _prompt() -> PromptParts:
    return PromptParts(mode="chat", template="你是测试助手。", values={})


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _completion(content=None, tool_calls=None, finish_reason="stop", usage=None):
    message = {}
    if content is not None:
        message["content"] = content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return _FakeResponse(payload={
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    })


class _FakeAsyncClient:
    """替身 httpx.AsyncClient：按脚本吐响应，记录每次请求。"""

    def __init__(self, script, calls):
        self._script = script
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        self._calls.append({"url": url, "payload": json, "headers": headers})
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(params=["relay", "openai_compat"])
def relay_call(request, monkeypatch, tmp_path):
    """返回 run(script, **kwargs) → (result, calls)，自动清理 trace 上下文。

    两个引擎模块共用同一套契约断言。"""
    engine = {"relay": relay, "openai_compat": openai_compat}[request.param]
    db = Database(str(tmp_path / "relay_test.db"))

    def run(script, *, preset=None, prompt=None, messages=None,
            send_callback=None, tool_callback=None, tool_names=None):
        calls: list[dict] = []
        monkeypatch.setattr(engine.httpx, "AsyncClient",
                            lambda: _FakeAsyncClient(list(script), calls))
        result = asyncio.run(engine._call_with_tools(
            db, prompt if prompt is not None else _prompt(),
            messages or [{"role": "user", "content": "[2026-07-17 10:00] 在吗"}],
            preset or _preset(),
            send_callback=send_callback, tool_callback=tool_callback,
            tool_names=tool_names,
        ))
        return result, calls

    yield run
    trace._current.set(None)


def test_request_shape_bearer_auth_and_v1_suffix(relay_call):
    result, calls = relay_call([_completion(content="你好呀")])

    assert result == "你好呀"
    assert len(calls) == 1
    req = calls[0]
    assert req["url"] == "https://relay.example.com/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer sk-test"
    payload = req["payload"]
    assert payload["model"] == "claude-x"
    assert payload["max_tokens"] == 4096
    assert payload["messages"][0] == {"role": "system", "content": "你是测试助手。"}
    assert payload["messages"][1] == {"role": "user", "content": "[2026-07-17 10:00] 在吗"}
    # tool_names=None → 全量工具，OpenAI function schema 原样传
    assert payload["tools"] == get_tools(None)


def test_v1_suffix_not_duplicated_and_optional(relay_call):
    _, calls = relay_call([_completion(content="ok")],
                          preset=_preset(base_url="https://r.example.com/v1"))
    assert calls[0]["url"] == "https://r.example.com/v1/chat/completions"

    _, calls = relay_call([_completion(content="ok")],
                          preset=_preset(base_url="https://r.example.com/api",
                                         use_v1_suffix=False))
    assert calls[0]["url"] == "https://r.example.com/api/chat/completions"


def test_empty_tool_names_omits_tools_key(relay_call):
    _, calls = relay_call([_completion(content="ok")], tool_names=set())
    assert "tools" not in calls[0]["payload"]


def test_final_round_invokes_send_callback_once(relay_call):
    sent = []

    async def send(text):
        sent.append(text)

    result, _ = relay_call([_completion(content="回复内容")], send_callback=send)
    assert result == "回复内容"
    assert sent == ["回复内容"]


def test_tool_round_message_shapes_and_callbacks(relay_call):
    tool_call = {"id": "call_1", "type": "function",
                 "function": {"name": "list_reminders", "arguments": "{}"}}
    tool_rounds = []

    async def on_tools(names):
        tool_rounds.append(names)

    result, calls = relay_call(
        [_completion(content="我先看一眼", tool_calls=[tool_call],
                     finish_reason="tool_calls"),
         _completion(content="现在没有待办提醒")],
        tool_callback=on_tools,
    )

    assert result == "我先看一眼\n现在没有待办提醒"
    assert tool_rounds == [["list_reminders"]]
    assert len(calls) == 2

    msgs = calls[1]["payload"]["messages"]
    # 回填顺序：assistant(tool_calls 原样透传) → role:tool → round hint user 消息
    assert msgs[-3] == {"role": "assistant", "tool_calls": [tool_call],
                        "content": "我先看一眼"}
    expected_result = {"success": True, "reminders": [], "count": 0}
    assert msgs[-2] == {"role": "tool", "tool_call_id": "call_1",
                        "content": json.dumps(expected_result, ensure_ascii=False)}
    assert msgs[-1] == {"role": "user",
                        "content": build_tool_round_hint(["list_reminders"])}


def test_think_blocks_stripped_from_display_but_kept_in_trace(relay_call):
    entry = trace.start(trigger="chat", model="claude-x", provider="relay",
                        prompt_parts=None, messages=[])
    sent = []

    async def send(text):
        sent.append(text)

    result, _ = relay_call(
        [_completion(content="<think>内部推理</think>好的收到",
                     usage={"prompt_tokens": 7, "completion_tokens": 3})],
        send_callback=send,
    )

    assert result == "好的收到"
    assert sent == ["好的收到"]
    round_ = entry["rounds"][0]
    assert round_["raw_output"] == "<think>内部推理</think>好的收到"
    assert round_["think"] == "内部推理"
    assert round_["visible_text"] == "好的收到"
    assert round_["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert round_["stop_reason"] == "stop"


def test_trace_records_tool_round_contract(relay_call):
    entry = trace.start(trigger="chat", model="claude-x", provider="relay",
                        prompt_parts=None, messages=[])
    tool_call = {"id": "call_9", "type": "function",
                 "function": {"name": "list_reminders", "arguments": "{}"}}
    relay_call([_completion(tool_calls=[tool_call], finish_reason="tool_calls"),
                _completion(content="完事")])

    assert len(entry["rounds"]) == 2
    tool_round = entry["rounds"][0]
    assert set(tool_round) == {"n", "raw_output", "think", "visible_text",
                               "tool_calls", "tool_results",
                               "post_hints_triggered", "usage", "stop_reason"}
    assert tool_round["tool_calls"] == [
        {"name": "list_reminders", "input": {}, "id": "call_9"}]
    assert tool_round["tool_results"] == [
        {"name": "list_reminders", "tool_use_id": "call_9",
         "result": {"success": True, "reminders": [], "count": 0}}]
    assert tool_round["post_hints_triggered"] == ["list_reminders"]
    final_round = entry["rounds"][1]
    assert final_round["tool_calls"] == []
    assert final_round["visible_text"] == "完事"


def test_repeated_display_text_sent_only_once(relay_call):
    tool_call = {"id": "call_1", "type": "function",
                 "function": {"name": "list_reminders", "arguments": "{}"}}
    sent = []

    async def send(text):
        sent.append(text)

    result, _ = relay_call(
        [_completion(content="好的", tool_calls=[tool_call],
                     finish_reason="tool_calls"),
         _completion(content="好的")],
        send_callback=send,
    )
    assert sent == ["好的"]
    assert result == "好的"


def test_round_cap_returns_internal_error_text(relay_call):
    tool_call = {"id": "call_1", "type": "function",
                 "function": {"name": "list_reminders", "arguments": "{}"}}
    script = [_completion(tool_calls=[tool_call], finish_reason="tool_calls")
              for _ in range(5)]
    result, calls = relay_call(script)
    assert result == "（内部错误：工具调用次数过多）"
    assert len(calls) == 5


def test_http_status_error_raises_provider_error(relay_call):
    with pytest.raises(AIProviderError):
        relay_call([_FakeResponse(status_code=500, payload=None,
                                  text="upstream exploded")])


def test_network_error_raises_provider_error(relay_call):
    with pytest.raises(AIProviderError):
        relay_call([httpx.ConnectError("connection refused")])


def test_non_json_body_raises_provider_error(relay_call):
    with pytest.raises(AIProviderError):
        relay_call([_FakeResponse(status_code=200, payload=None,
                                  text="<html>gateway timeout</html>")])


def test_empty_choices_returns_internal_error_text(relay_call):
    result, _ = relay_call([_FakeResponse(payload={"choices": []})])
    assert result == "（内部错误：中转站未返回内容）"
