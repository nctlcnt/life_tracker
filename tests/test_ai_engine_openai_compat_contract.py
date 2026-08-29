"""统一引擎（ai_engine_openai_compat）的 OpenAI-compatible 请求契约。

LT-134 引擎合并期间以合并前的 relay 引擎（当时的 production active 路径）
为行为基线参数化对照，合并完成后收敛为只跑统一引擎。冻结的内容：
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


@pytest.fixture
def relay_call(monkeypatch, tmp_path):
    """返回 run(script, **kwargs) → (result, calls)，自动清理 trace 上下文。"""
    engine = openai_compat
    db = Database(str(tmp_path / "relay_test.db"))

    def run(script, *, preset=None, prompt=None, messages=None,
            send_callback=None, tool_callback=None, tool_names=None,
            tool_executor=None, return_final_only=False, max_rounds=5):
        calls: list[dict] = []
        monkeypatch.setattr(engine.httpx, "AsyncClient",
                            lambda: _FakeAsyncClient(list(script), calls))
        result = asyncio.run(engine._call_with_tools(
            db, prompt if prompt is not None else _prompt(),
            messages or [{"role": "user", "content": "[2026-07-17 10:00] 在吗"}],
            preset or _preset(),
            send_callback=send_callback, tool_callback=tool_callback,
            tool_names=tool_names,
            tool_executor=tool_executor,
            return_final_only=return_final_only,
            max_rounds=max_rounds,
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
    # 默认聊天工具集不包含只能由 check-in 显式启用的 set_scene。
    assert payload["tools"] == get_tools(None)
    assert "set_scene" not in {
        tool["function"]["name"] for tool in payload["tools"]
    }


def test_set_scene_is_available_when_explicitly_requested(relay_call):
    _, calls = relay_call(
        [_completion(content="ok")], tool_names={"set_scene"}
    )

    assert [
        tool["function"]["name"] for tool in calls[0]["payload"]["tools"]
    ] == ["set_scene"]


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


def test_background_worker_uses_injected_executor_and_returns_only_final_json(
    relay_call,
):
    tool_call = {
        "id": "call_worker_1",
        "type": "function",
        "function": {
            "name": "set_reminder",
            "arguments": json.dumps(
                {"trigger_time": "2026-08-29T08:00:00", "action": "交作业"},
                ensure_ascii=False,
            ),
        },
    }
    executed = []

    async def executor(name, arguments, call_index):
        executed.append((name, arguments, call_index))
        return {"success": True, "reminder_id": 7}

    final_json = json.dumps(
        {
            "outcome": "completed",
            "execution_results": [
                {
                    "operation": "设置提醒",
                    "status": "succeeded",
                    "details": {},
                }
            ],
            "important_information": [],
            "supersedes_previous": False,
        },
        ensure_ascii=False,
    )
    result, calls = relay_call(
        [
            _completion(
                content="internal progress",
                tool_calls=[tool_call],
                finish_reason="tool_calls",
            ),
            _completion(content=final_json),
        ],
        tool_names={"set_reminder"},
        tool_executor=executor,
        return_final_only=True,
    )

    assert result == final_json
    assert executed == [
        (
            "set_reminder",
            {"trigger_time": "2026-08-29T08:00:00", "action": "交作业"},
            0,
        )
    ]
    assert calls[1]["payload"]["messages"][-2] == {
        "role": "tool",
        "tool_call_id": "call_worker_1",
        "content": json.dumps(
            {"success": True, "reminder_id": 7}, ensure_ascii=False
        ),
    }


def test_background_worker_round_cap_is_a_hard_failure(relay_call):
    tool_call = {
        "id": "call_loop",
        "type": "function",
        "function": {"name": "list_reminders", "arguments": "{}"},
    }

    async def executor(_name, _arguments, _call_index):
        return {"success": True, "reminders": []}

    with pytest.raises(AIProviderError, match="超过最大调用轮数"):
        relay_call(
            [_completion(tool_calls=[tool_call], finish_reason="tool_calls")],
            tool_names={"list_reminders"},
            tool_executor=executor,
            return_final_only=True,
            max_rounds=1,
        )


_TOOLS_SECTION = "【工具使用策略】log 前自查同时段是否已有相同事件。"


def _prompt_with_tools() -> PromptParts:
    """带 {tools} 占位符的模板——默认 _prompt() 没有占位符，concise() 是空操作。"""
    return PromptParts(mode="chat", template="你是测试助手。\n\n{tools}",
                       values={"tools": _TOOLS_SECTION})


def _tool_round_script():
    tool_call = {"id": "call_1", "type": "function",
                 "function": {"name": "list_reminders", "arguments": "{}"}}
    return tool_call, [
        _completion(content="我先看一眼", tool_calls=[tool_call],
                    finish_reason="tool_calls"),
        _completion(content="现在没有待办提醒"),
    ]


def test_tools_section_dropped_from_system_after_first_round(relay_call):
    """中间轮改发 concise()：tools 使用策略只投递一次，其余 system 正文保持不变。"""
    _, script = _tool_round_script()
    _, calls = relay_call(script, prompt=_prompt_with_tools())

    assert len(calls) == 2
    first_system = calls[0]["payload"]["messages"][0]
    second_system = calls[1]["payload"]["messages"][0]

    assert first_system["role"] == "system"
    assert _TOOLS_SECTION in first_system["content"]

    # 第二轮：抽掉 tools 段，人格/规则正文照发
    assert second_system["role"] == "system"
    assert _TOOLS_SECTION not in second_system["content"]
    assert second_system["content"] == "你是测试助手。"


def test_tool_schema_still_sent_every_round(relay_call):
    """省掉的是 DB 的 tools 使用策略散文，不是工具 schema——后者每轮都得发，
    否则模型没法在中间轮继续调工具。"""
    _, script = _tool_round_script()
    _, calls = relay_call(script, prompt=_prompt_with_tools())

    assert calls[0]["payload"]["tools"] == get_tools(None)
    assert calls[1]["payload"]["tools"] == get_tools(None)


def test_concise_system_keeps_tool_round_message_shape(relay_call):
    """换 system 不能动会话消息的回填顺序。"""
    tool_call, script = _tool_round_script()
    result, calls = relay_call(script, prompt=_prompt_with_tools())

    assert result == "我先看一眼\n现在没有待办提醒"
    msgs = calls[1]["payload"]["messages"]
    assert msgs[-3] == {"role": "assistant", "tool_calls": [tool_call],
                        "content": "我先看一眼"}
    assert msgs[-2]["role"] == "tool"
    assert msgs[-1] == {"role": "user",
                        "content": build_tool_round_hint(["list_reminders"])}


def test_plain_string_prompt_unchanged_across_rounds(relay_call):
    """simple_completion 传的是已拍平的 str，没有占位符可抽，两轮同一份。"""
    _, script = _tool_round_script()
    _, calls = relay_call(script, prompt="已拍平的 system 文本")

    assert calls[0]["payload"]["messages"][0] == {
        "role": "system", "content": "已拍平的 system 文本"}
    assert calls[1]["payload"]["messages"][0] == {
        "role": "system", "content": "已拍平的 system 文本"}


def test_empty_system_omits_system_block_in_all_rounds(relay_call):
    """模板整体为空时，两轮都不能发空 system 块（anthropic 系代理会 400）。"""
    _, script = _tool_round_script()
    _, calls = relay_call(script,
                          prompt=PromptParts(mode="chat", template="{tools}",
                                             values={"tools": ""}))

    for call in calls:
        assert call["payload"]["messages"][0]["role"] == "user"


def test_system_block_dropped_when_template_is_only_tools(relay_call):
    """边界：模板正文全靠 {tools} 撑着时，第二轮整个 system 块消失（首轮仍有）。
    线上 main_template 有大段人格正文，concise 后仍非空，走不到这里；
    这条只是把模板被改空后的行为钉住，避免退化成发空 system 块。"""
    _, script = _tool_round_script()
    _, calls = relay_call(script,
                          prompt=PromptParts(mode="chat", template="{tools}",
                                             values={"tools": _TOOLS_SECTION}))

    assert calls[0]["payload"]["messages"][0] == {
        "role": "system", "content": _TOOLS_SECTION}
    assert calls[1]["payload"]["messages"][0]["role"] == "user"


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


def test_empty_choices_raises_provider_error(relay_call):
    with pytest.raises(AIProviderError, match="缺少 choices"):
        relay_call([_FakeResponse(payload={"choices": []})])
