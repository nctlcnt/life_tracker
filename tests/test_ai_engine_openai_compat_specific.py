"""LT-134 统一引擎（ai_engine_openai_compat）特有行为的测试。

共享契约在 test_ai_engine_relay_contract.py 里参数化覆盖；这里只测
统一引擎相对 relay 基线新增的两点：
- base_url 为空 → 默认官方 OpenAI 端点（接管原 openai SDK 引擎的 preset）
- 官方 GPT-5 系拒绝 max_tokens 时换 max_completion_tokens 重试
"""
import asyncio
import json

import pytest

from config import Preset
from bot import trace
import bot.ai_engine_openai_compat as compat
from bot.ai_provider_error import AIProviderError
from bot.database import Database
from bot.prompts import PromptParts

from tests.test_ai_engine_relay_contract import (
    _FakeAsyncClient, _FakeResponse, _completion,
)


def _preset(base_url="", use_v1_suffix=True) -> Preset:
    return Preset(name="official-test", provider="openai", api_key="sk-test",
                  base_url=base_url, model="gpt-5.5", use_v1_suffix=use_v1_suffix)


@pytest.fixture
def compat_call(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "compat_test.db"))

    def run(script, *, preset=None, tool_names=None):
        calls: list[dict] = []
        monkeypatch.setattr(compat.httpx, "AsyncClient",
                            lambda: _FakeAsyncClient(list(script), calls))
        result = asyncio.run(compat._call_with_tools(
            db, PromptParts(mode="chat", template="你是测试助手。", values={}),
            [{"role": "user", "content": "在吗"}],
            preset or _preset(),
            tool_names=tool_names,
        ))
        return result, calls

    yield run
    trace._current.set(None)


def test_empty_base_url_defaults_to_official_openai(compat_call):
    _, calls = compat_call([_completion(content="ok")])
    assert calls[0]["url"] == "https://api.openai.com/v1/chat/completions"


def test_max_tokens_rejection_retries_with_max_completion_tokens(compat_call):
    rejection = _FakeResponse(
        status_code=400, payload=None,
        text=json.dumps({"error": {"message":
            "Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead."}}))

    result, calls = compat_call([rejection, _completion(content="好的")])

    assert result == "好的"
    assert len(calls) == 2
    assert calls[0]["payload"]["max_tokens"] == 4096
    assert "max_completion_tokens" not in calls[0]["payload"]
    assert calls[1]["payload"]["max_completion_tokens"] == 4096
    assert "max_tokens" not in calls[1]["payload"]


def test_param_switch_sticks_for_later_rounds(compat_call):
    rejection = _FakeResponse(
        status_code=400, payload=None,
        text="use 'max_completion_tokens' instead")
    tool_call = {"id": "call_1", "type": "function",
                 "function": {"name": "list_reminders", "arguments": "{}"}}

    result, calls = compat_call([
        rejection,
        _completion(tool_calls=[tool_call], finish_reason="tool_calls"),
        _completion(content="搞定"),
    ])

    assert result == "搞定"
    # 第 2 轮（工具回填后）沿用切换过的参数，不再回退重试
    assert calls[2]["payload"]["max_completion_tokens"] == 4096
    assert "max_tokens" not in calls[2]["payload"]


def test_plain_400_raises_without_retry(compat_call):
    with pytest.raises(AIProviderError):
        compat_call([_FakeResponse(status_code=400, payload=None,
                                   text="invalid api key")])
