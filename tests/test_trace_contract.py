"""LT-134 characterization tests：冻结 trace 落盘与 DB 摘要契约。

引擎合并后 trace round 序列化收敛为一种格式，但 JSONL entry 顶层 schema、
rounds 字段集合、ai_runs 摘要（save_ai_run 入参）必须保持不变，
TraceViewer 与 dashboard 依赖它们。
"""
import json

import pytest

from bot import trace
from bot.prompts import PromptParts


@pytest.fixture
def trace_dir(monkeypatch, tmp_path):
    target = tmp_path / "ai_traces"
    monkeypatch.setattr(trace, "_TRACE_DIR", target)
    yield target
    trace._current.set(None)


def _run_one_trace(prompt_parts=None, db=None, error=None):
    trace.start(
        trigger="chat", model="claude-x", provider="relay",
        prompt_parts=prompt_parts,
        messages=[
            {"role": "user", "content": "早上的事"},
            {"role": "assistant", "content": "嗯嗯"},
            {"role": "user", "content": "[2026-07-17 10:00] 提醒我喝水"},
        ],
    )
    trace.add_round(
        raw_output="<think>想想</think>好的",
        think="想想", visible_text="好的",
        tool_calls=[{"name": "set_reminder",
                     "input": {"trigger_time": "2026-07-17 11:00", "action": "喝水"},
                     "id": "call_1"}],
        tool_results=[{"name": "set_reminder", "tool_use_id": "call_1",
                       "result": {"success": True, "reminder_id": 7}}],
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        stop_reason="tool_calls",
    )
    trace.finalize(final_text="好的" if error is None else None,
                   error=error, db=db)


def test_entry_written_to_daily_jsonl_with_stable_schema(trace_dir):
    prompt = PromptParts(mode="chat", template="系统提示原文", values={})
    _run_one_trace(prompt_parts=prompt)

    files = list(trace_dir.glob("*.jsonl"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())

    assert set(entry) == {"id", "ts", "trigger", "model", "provider",
                          "prompt_parts", "user_message", "history",
                          "window", "rounds", "final_text", "error"}
    # LT-135 新增字段：未传窗口时为 null，旧记录无此键，读取方按缺省处理
    assert entry["window"] is None
    assert entry["trigger"] == "chat"
    assert entry["provider"] == "relay"
    # 最后一条 message 拆成 user_message，其余进 history
    assert entry["user_message"] == "[2026-07-17 10:00] 提醒我喝水"
    assert [m["content"] for m in entry["history"]] == ["早上的事", "嗯嗯"]
    assert entry["final_text"] == "好的"
    assert entry["error"] is None

    # prompt 序列化 = schema 2（LT-129 单模板），TraceViewer 按此渲染
    assert entry["prompt_parts"]["schema"] == 2
    assert entry["prompt_parts"]["template"] == "系统提示原文"
    assert entry["prompt_parts"]["blocks"] == [
        {"i": 0, "tier": 1, "chars": len("系统提示原文"), "text": "系统提示原文"}]

    round_ = entry["rounds"][0]
    assert set(round_) == {"n", "raw_output", "think", "visible_text",
                           "tool_calls", "tool_results",
                           "post_hints_triggered", "usage", "stop_reason"}
    assert round_["n"] == 1
    assert round_["tool_calls"][0]["name"] == "set_reminder"
    assert round_["stop_reason"] == "tool_calls"


def test_finalize_persists_run_summary_contract(trace_dir):
    class _FakeDb:
        def __init__(self):
            self.saved = None

        def save_ai_run(self, **kwargs):
            self.saved = kwargs

    db = _FakeDb()
    _run_one_trace(db=db)

    saved = db.saved
    assert set(saved) == {"run_id", "trigger", "model", "provider",
                          "started_at", "finished_at", "status", "error",
                          "final_text", "tool_calls"}
    assert saved["status"] == "success"
    assert saved["provider"] == "relay"
    call = saved["tool_calls"][0]
    assert call["round_n"] == 1
    assert call["tool_name"] == "set_reminder"
    assert json.loads(call["arguments_json"]) == {
        "trigger_time": "2026-07-17 11:00", "action": "喝水"}
    assert json.loads(call["result_json"]) == {"success": True, "reminder_id": 7}
    assert call["success"] == 1


def test_error_run_marked_failed(trace_dir):
    class _FakeDb:
        def __init__(self):
            self.saved = None

        def save_ai_run(self, **kwargs):
            self.saved = kwargs

    db = _FakeDb()
    _run_one_trace(db=db, error="AIProviderError: boom")

    assert db.saved["status"] == "failed"
    assert db.saved["error"] == "AIProviderError: boom"

    files = list(trace_dir.glob("*.jsonl"))
    entry = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert entry["error"] == "AIProviderError: boom"


def test_trace_write_failure_never_raises(monkeypatch, tmp_path):
    # _TRACE_DIR 指到一个普通文件下面，mkdir 必然失败
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setattr(trace, "_TRACE_DIR", blocker / "sub")

    trace.start(trigger="chat", model="m", provider="relay",
                prompt_parts=None, messages=[])
    # 契约：trace 写失败绝不能炸主流程
    try:
        trace.finalize(final_text="ok")
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"finalize raised: {exc}")
    finally:
        trace._current.set(None)
