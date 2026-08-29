"""LT-178 persona-free tool batch execution and result expression."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Awaitable, Callable

from bot.ai_engine_base import (
    SQLITE_TRANSACTIONAL_TOOL_NAMES,
    _ensure_valid_messages,
    _execute_tool,
    _execute_tool_async,
    sanitize_toolless_chat_output,
)
from bot.database import Database
from bot.logger import get_logger
from bot.memory import MemoryService
from bot.tools import POLL_TOOL_NAMES, REMINDER_TOOL_NAMES, TOOLS

from .outbound import NullGenerationGate
from .tool_batches import ToolBatchRepository
from .worker_prompts import (
    ToolWorkerOutput,
    build_result_expression_system,
    build_tool_worker_system,
    parse_tool_worker_output,
    result_expression_request,
)


logger = get_logger(__name__)

ModelRunner = Callable[..., Awaitable[tuple[str, str] | str]]
ExpressionRunner = Callable[..., Awaitable[tuple[str, str] | str]]
FinishedCallback = Callable[[dict[str, Any]], Any]

MEMORY_WRITE_TOOLS = {"save_memory", "update_memory", "delete_memory"}
ALL_TOOL_NAMES = {item["function"]["name"] for item in TOOLS}
CONVERSATION_TOOL_NAMES = ALL_TOOL_NAMES - MEMORY_WRITE_TOOLS - {"set_scene"}
ROUTINE_WRITE_TOOLS = {
    "log_timeline_event",
    "update_timeline_event",
    "delete_timeline_event",
    "add_deadline",
    "complete_deadline",
    "delete_deadline",
}
INTERNAL_TOOLS = {"set_scene"}
# 只读工具重跑一次没有副作用，而且能拿到最新数据，所以不参与跨尝试去重。
# 其余工具一律按有副作用处理：将来新增工具默认受保护，漏掉一个的后果是
# 静默重复写入，比多去重一次严重得多。
READ_ONLY_TOOLS = {"list_reminders", "query_calendar", "search_history"}
SIDE_EFFECT_TOOLS = ALL_TOOL_NAMES - READ_ONLY_TOOLS
CREATED_ID_FIELDS = {
    "log_timeline_event": "event_id",
    "set_reminder": "reminder_id",
    "add_deadline": "deadline_id",
}
SAY_KEY = "say"


def _arguments_fingerprint(arguments: Any) -> str:
    """同一组参数的稳定指纹，用于识别跨尝试的重复调用。"""
    return json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)


class BatchLeaseLost(RuntimeError):
    pass


class ToolReplayMismatch(RuntimeError):
    """账本上同一个序号被两组不同的调用占用。

    续接改成序号偏移之后，新调用总是排在账本之后，所以这里不再表示模型换了
    主意，而表示偏移量或批次占用出了问题，属于内部不变量被破坏。
    """


class ToolInvocationFailed(RuntimeError):
    pass


class ToolResultExpresser:
    """Let the chat track express its own private execution-track result."""

    def __init__(
        self,
        db: Database,
        runner: ExpressionRunner,
        *,
        generation_gate=None,
        memory_service: MemoryService | None = None,
        history_limit: int = 20,
    ) -> None:
        self.db = db
        self.runner = runner
        self.generation_gate = generation_gate or NullGenerationGate()
        self.memory_service = memory_service
        self.history_limit = max(int(history_limit), 1)

    async def express(
        self,
        *,
        channel_id: str,
        outcome: str,
        execution_results: tuple[dict[str, Any], ...],
        important_information: tuple[dict[str, str], ...],
    ) -> str:
        async with self.generation_gate:
            history = self.db.get_recent_ai_messages(
                str(channel_id), limit=self.history_limit
            )
            request = result_expression_request(
                outcome=outcome,
                execution_results=execution_results,
                important_information=important_information,
            )
            # Normalize the historical window first, then append the backend
            # envelope.  Normalizing the combined list would merge it into a
            # preceding user message and blur the trust boundary.
            messages = [
                *_ensure_valid_messages(history),
                {"role": "user", "content": request},
            ]
            system_prompt = build_result_expression_system(
                self.db, memory_service=self.memory_service
            )
            raw = await self.runner(self.db, system_prompt, messages)
            reply = raw[0] if isinstance(raw, tuple) else raw

        text = sanitize_toolless_chat_output(str(reply or ""))
        if not text or "[SILENT]" in text:
            raise ValueError("chat track did not safely express the internal result")

        private_ids = self._private_identifiers(execution_results)
        public_values = tuple(item["value"] for item in important_information)
        lowered_text = text.lower()
        leaked = [
            value
            for key, value in private_ids
            if value not in public_values and (
                key in lowered_text
                or f"id={value}" in lowered_text
                or f"id: {value}" in lowered_text
                or f"编号{value}" in text
            )
        ]
        if leaked:
            raise ValueError("chat track exposed private identifiers")
        return text

    @staticmethod
    def _private_identifiers(
        execution_results: tuple[dict[str, Any], ...]
    ) -> tuple[tuple[str, str], ...]:
        found: list[tuple[str, str]] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower().endswith("_id") and item is not None:
                        found.append((str(key).lower(), str(item)))
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(list(execution_results))
        return tuple(dict.fromkeys(found))


class ToolWorker:
    """Single coroutine that claims, executes, and finalizes tool batches."""

    def __init__(
        self,
        db: Database,
        repository: ToolBatchRepository,
        model_runner: ModelRunner,
        expresser: ToolResultExpresser,
        *,
        memory_service: MemoryService | None = None,
        max_attempts: int = 3,
        batch_timeout_seconds: float = 60.0,
        idle_poll_seconds: float = 1.0,
        on_batch_finished: FinishedCallback | None = None,
    ) -> None:
        self.db = db
        self.repository = repository
        self.model_runner = model_runner
        self.expresser = expresser
        self.memory_service = memory_service
        self.max_attempts = max(int(max_attempts), 1)
        self.batch_timeout_seconds = max(float(batch_timeout_seconds), 0.01)
        self.idle_poll_seconds = max(float(idle_poll_seconds), 0.01)
        self.on_batch_finished = on_batch_finished
        self._running = False
        self._wake = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._running

    def wake(self, _batch: dict[str, Any] | None = None) -> None:
        self._wake.set()

    def set_finished_callback(
        self, callback: FinishedCallback | None
    ) -> None:
        self.on_batch_finished = callback

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("ToolWorker is already running")
        self._running = True
        logger.info("🧰 工具 worker 已启动")
        try:
            while self._running:
                self._wake.clear()
                try:
                    batch = self.repository.claim_next()
                except Exception:
                    logger.exception("工具 worker 领取批次失败，稍后重试")
                    batch = None
                if batch is not None:
                    try:
                        await self.process(batch)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # process 自己会把正常执行错误写成 retry/completed；这层
                        # 只兜住状态回写本身失败，避免一个 SQLite 瞬时错误杀掉
                        # 整个长期 worker。running lease 会由后续领取回收。
                        logger.exception(
                            "工具 worker 批次状态回写失败: %s", batch.get("id")
                        )
                    continue
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.idle_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            logger.info("🧰 工具 worker 已停止")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()

    async def process(self, batch: dict[str, Any]) -> None:
        """Execute one already-claimed batch."""
        try:
            await asyncio.wait_for(
                self._process_claimed(batch), timeout=self.batch_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_failure(batch, exc)
        finally:
            self._announce_finished(batch)

    async def _process_claimed(self, batch: dict[str, Any]) -> None:
        batch_id = str(batch["id"])
        lease_token = str(batch["lease_token"])
        call_records = {
            int(item["call_index"]): item
            for item in self.repository.calls(batch_id)
        }
        # 位置绑定原本顺带挡住了重复写入，改成续接之后这层保证消失，改由参数
        # 指纹接替：只认上一次尝试留下的成功记录，本轮内的新调用不参与，
        # 这样同一批次里「先查询、再写入、再查询」不会被误判成重复。
        resumed_side_effects = {
            (item["tool_name"], _arguments_fingerprint(item.get("arguments"))): item
            for item in call_records.values()
            if item["succeeded"] and item["tool_name"] in SIDE_EFFECT_TOOLS
        }
        model_input, new_user_messages, prior = self._model_input(
            batch, prior_calls=list(call_records.values())
        )

        if batch["source_kind"] == "conversation" and not new_user_messages:
            if not self.repository.mark_completed(batch_id, lease_token):
                raise BatchLeaseLost(batch_id)
            return

        tool_names = self._tool_names(batch)
        system_prompt = build_tool_worker_system(
            self.db,
            context_config=(batch.get("input") or {}).get("context_config")
            if batch["source_kind"] == "check_in"
            else None,
        )
        async def execute_tool(
            name: str, arguments: dict, provider_call_index: int
        ):
            # provider 的序号每轮从 0 重数，而且 preset 回退会让它在同一次尝试
            # 里再归零一遍，所以它不能充当持久身份。序号一律从活账本推导：
            # _execute_one 是顺序 await 并就地更新 call_records 的，因此这个
            # 计数器单调递增，不受 provider 侧任何重置影响。
            del provider_call_index
            next_call_index = max(call_records) + 1 if call_records else 0
            return await self._execute_one(
                batch,
                tool_names,
                call_records,
                name,
                arguments,
                next_call_index,
                resumed_side_effects=resumed_side_effects,
            )

        raw = await self.model_runner(
            self.db,
            system_prompt,
            [{"role": "user", "content": model_input}],
            tool_names=tool_names,
            tool_executor=execute_tool,
        )
        if isinstance(raw, tuple):
            raw_text, run_id = raw
        else:
            raw_text, run_id = raw, None
        output = parse_tool_worker_output(str(raw_text or ""))
        calls = self.repository.calls(batch_id)
        self._validate_outcome(output, calls)
        delivery_kind = self._delivery_kind(batch, output, calls)
        result: dict[str, Any] = {
            "outcome": output.outcome,
            "execution_results": list(output.execution_results),
            "important_information": list(output.important_information),
            "actions": [
                {
                    "call_index": item["call_index"],
                    "tool_name": item["tool_name"],
                    "arguments": item.get("arguments"),
                    "result": item.get("result"),
                    "succeeded": item["succeeded"],
                }
                for item in calls
            ],
        }

        if batch["execution_mode"] == "shadow":
            delivery_kind = "none"
        elif delivery_kind == "message":
            result[SAY_KEY] = await self.expresser.express(
                channel_id=batch["channel_id"],
                outcome=output.outcome,
                execution_results=output.execution_results,
                important_information=output.important_information,
            )

        supersedes_batch_id = None
        if output.supersedes_previous and prior is not None:
            supersedes_batch_id = prior["id"]
        if not self.repository.mark_completed(
            batch_id,
            lease_token,
            result=result,
            delivery_kind=delivery_kind,
            last_run_id=run_id,
            supersedes_batch_id=supersedes_batch_id,
        ):
            raise BatchLeaseLost(batch_id)

    async def _execute_one(
        self,
        batch: dict[str, Any],
        allowed_tools: set[str],
        call_records: dict[int, dict[str, Any]],
        tool_name: str,
        arguments: dict[str, Any],
        call_index: int,
        *,
        resumed_side_effects: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> Any:
        if tool_name not in allowed_tools:
            raise ToolInvocationFailed(f"tool not allowed in this batch: {tool_name}")
        if not isinstance(arguments, dict):
            raise ToolInvocationFailed("tool arguments must be an object")

        # 续接之后新序号总是排在账本之后，这里命中说明偏移量或批次占用出了
        # 问题，属于内部不变量被破坏，而不是模型改了主意。
        existing = call_records.get(int(call_index))
        if existing is not None:
            if (
                existing["tool_name"] != tool_name
                or (existing.get("arguments") or {}) != arguments
            ):
                raise ToolReplayMismatch(
                    f"call {call_index} is already recorded as "
                    f"{existing['tool_name']} {existing.get('arguments')} and "
                    f"cannot be reused for {tool_name} {arguments}"
                )
            if existing["succeeded"]:
                return existing.get("result")

        # 上一次尝试已经做成过同样这件事，副作用不可以再发生一遍。
        if resumed_side_effects and tool_name in SIDE_EFFECT_TOOLS:
            duplicate = resumed_side_effects.get(
                (tool_name, _arguments_fingerprint(arguments))
            )
            if duplicate is not None:
                logger.info(
                    "批次 %s 跳过重复的 %s，复用第 %s 号调用留下的结果",
                    batch.get("id"),
                    tool_name,
                    duplicate["call_index"],
                )
                return duplicate.get("result")

        execution_args = dict(arguments)
        if tool_name == "set_scene":
            execution_args["_check_in_name"] = (
                (batch.get("input") or {}).get("check_in_name") or "unknown"
            )

        if batch["execution_mode"] == "shadow":
            result: Any = {
                "success": True,
                "shadow": True,
                "message": "proposal recorded; business tool not applied",
            }
        elif tool_name in SQLITE_TRANSACTIONAL_TOOL_NAMES:
            last_error: Exception | None = None
            for _attempt in range(2):
                try:
                    record, written = self.repository.execute_atomic_call(
                        batch["id"],
                        call_index,
                        batch["lease_token"],
                        tool_name=tool_name,
                        arguments=arguments,
                        executor=lambda conn: _execute_tool(
                            self.db,
                            tool_name,
                            execution_args,
                            memory_service=self.memory_service,
                            conn=conn,
                        ),
                    )
                    if record is None:
                        raise BatchLeaseLost(str(batch["id"]))
                    call_records[int(call_index)] = record
                    if (
                        written
                        and tool_name == "set_reminder"
                        and isinstance(record.get("result"), dict)
                        and record["result"].get("success") is True
                        and self.db._on_reminder_added is not None
                    ):
                        try:
                            self.db._on_reminder_added()
                        except Exception:
                            logger.exception(
                                "提醒已提交，但 scheduler 唤醒回调失败"
                            )
                    return record.get("result")
                except asyncio.CancelledError:
                    raise
                except BatchLeaseLost:
                    raise
                except Exception as exc:
                    last_error = exc
            failure = {
                "success": False,
                "error": f"{type(last_error).__name__}: {last_error}",
            }
            record, _ = self.repository.record_call(
                batch["id"],
                call_index,
                batch["lease_token"],
                tool_name=tool_name,
                arguments=arguments,
                result=failure,
                succeeded=False,
            )
            if record is None:
                raise BatchLeaseLost(str(batch["id"]))
            call_records[int(call_index)] = record
            raise ToolInvocationFailed(failure["error"])
        else:
            last_error: Exception | None = None
            for _attempt in range(2):
                try:
                    result = await _execute_tool_async(
                        self.db,
                        tool_name,
                        execution_args,
                        memory_service=self.memory_service,
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
            else:
                failure = {
                    "success": False,
                    "error": f"{type(last_error).__name__}: {last_error}",
                }
                record, _ = self.repository.record_call(
                    batch["id"],
                    call_index,
                    batch["lease_token"],
                    tool_name=tool_name,
                    arguments=arguments,
                    result=failure,
                    succeeded=False,
                )
                if record is None:
                    raise BatchLeaseLost(str(batch["id"]))
                call_records[int(call_index)] = record
                raise ToolInvocationFailed(failure["error"])

        record, _written = self.repository.record_call(
            batch["id"],
            call_index,
            batch["lease_token"],
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            succeeded=True,
        )
        if record is None:
            raise BatchLeaseLost(str(batch["id"]))
        call_records[int(call_index)] = record
        return record.get("result")

    def _model_input(
        self,
        batch: dict[str, Any],
        *,
        prior_calls: list[dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        prior_call_payload = [
            {
                "call_index": item["call_index"],
                "tool_name": item["tool_name"],
                "arguments": item.get("arguments"),
                "result": item.get("result"),
                "succeeded": item["succeeded"],
            }
            for item in sorted(
                prior_calls or [], key=lambda value: int(value["call_index"])
            )
        ]
        if batch["source_kind"] == "check_in":
            payload = batch.get("input") or {}
            envelope = {
                "CONTEXT_ONLY": [],
                "AUTHORIZED_NEW_INPUT": [
                    {
                        "source": "check_in",
                        "check_in_name": payload.get("check_in_name"),
                        "timestamp": payload.get("timestamp"),
                        "content": payload.get("prompt"),
                    }
                ],
                "PRIOR_UNDELIVERED_RESULT": None,
                "PRIOR_TOOL_CALLS": prior_call_payload,
            }
            return json.dumps(envelope, ensure_ascii=False), envelope[
                "AUTHORIZED_NEW_INPUT"
            ], None

        prior = self.repository.latest_pending_delivery(
            batch["channel_id"], exclude_batch_id=batch["id"]
        )

        interval = self.db.get_ai_messages_after(
            batch["channel_id"],
            int(batch["after_message_id"]),
            upto_id=int(batch["through_message_id"]),
        )
        context = self.db.get_conversation_messages_upto(
            batch["channel_id"], int(batch["after_message_id"]), limit=20
        )
        context.extend(item for item in interval if item["role"] != "user")
        new_users = [item for item in interval if item["role"] == "user"]
        envelope = {
            "CONTEXT_ONLY": [self._message_payload(item) for item in context],
            "AUTHORIZED_NEW_INPUT": [
                self._message_payload(item) for item in new_users
            ],
            "PRIOR_UNDELIVERED_RESULT": (
                self._prior_payload(prior) if prior else None
            ),
            "PRIOR_TOOL_CALLS": prior_call_payload,
        }
        return json.dumps(envelope, ensure_ascii=False), new_users, prior

    @staticmethod
    def _message_payload(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "role": item.get("role"),
            "created_at": item.get("created_at"),
            "content": item.get("content") or "",
        }

    @staticmethod
    def _prior_payload(batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "batch_id": batch["id"],
            "source_kind": batch["source_kind"],
            "result": batch.get("result"),
        }

    @staticmethod
    def _tool_names(batch: dict[str, Any]) -> set[str]:
        if batch["source_kind"] == "conversation":
            return set(CONVERSATION_TOOL_NAMES)
        payload = batch.get("input") or {}
        profile = payload.get("tool_profile") or "poll"
        if profile == "none":
            names: set[str] = set()
        elif profile == "reminder_safe":
            names = set(REMINDER_TOOL_NAMES)
        else:
            names = set(POLL_TOOL_NAMES)
        names -= MEMORY_WRITE_TOOLS
        if payload.get("track_scene"):
            names.add("set_scene")
        return names

    @staticmethod
    def _validate_outcome(
        output: ToolWorkerOutput, calls: list[dict[str, Any]]
    ) -> None:
        failures = [
            item
            for item in calls
            if isinstance(item.get("result"), dict)
            and item["result"].get("success") is False
        ]
        if failures and output.outcome == "empty":
            raise ValueError("tool failures cannot be reported as empty")
        important_values = {
            item["value"] for item in output.important_information
        }
        for item in calls:
            field = CREATED_ID_FIELDS.get(item["tool_name"])
            result = item.get("result")
            if (
                field
                and isinstance(result, dict)
                and result.get(field) is not None
                and str(result[field]) in important_values
            ):
                raise ValueError("created database IDs cannot be important information")
        names = {item["tool_name"] for item in calls}
        needs_message = bool(names - ROUTINE_WRITE_TOOLS - INTERNAL_TOOLS)
        if needs_message and output.outcome == "empty":
            raise ValueError("query/reminder results require facts or unable")

    @staticmethod
    def _delivery_kind(
        batch: dict[str, Any],
        output: ToolWorkerOutput,
        calls: list[dict[str, Any]],
    ) -> str:
        names = {item["tool_name"] for item in calls}
        has_business_failure = any(
            isinstance(item.get("result"), dict)
            and item["result"].get("success") is False
            for item in calls
        )
        if has_business_failure:
            return "message"
        if not names and output.outcome == "empty":
            return "none"
        if names and names <= INTERNAL_TOOLS:
            return "none"
        if batch["source_kind"] == "check_in":
            if output.outcome == "unable" or names - ROUTINE_WRITE_TOOLS - INTERNAL_TOOLS:
                return "message"
            return "none"
        if output.outcome == "unable":
            return "message"
        if names and names <= ROUTINE_WRITE_TOOLS:
            return "reaction"
        return "message"

    async def _handle_failure(
        self, batch: dict[str, Any], exc: Exception
    ) -> None:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("工具批次执行失败 %s: %s", batch.get("id"), error)
        if isinstance(exc, BatchLeaseLost):
            return
        if int(batch["attempt_count"]) < self.max_attempts:
            delay = min(2 ** max(int(batch["attempt_count"]) - 1, 0), 30)
            self.repository.mark_retry(
                batch["id"],
                batch["lease_token"],
                error,
                retry_after_seconds=delay,
            )
            self._wake.set()
            return

        if batch["execution_mode"] == "shadow":
            # Shadow is observational.  Its own provider/parser failure must
            # stay visible to operators in the batch audit, never to the user
            # whose authoritative request was still handled by the old path.
            if not self.repository.mark_failed(
                batch["id"], batch["lease_token"], error
            ):
                raise BatchLeaseLost(str(batch["id"]))
            return

        failure_results = ({
            "operation": "处理刚才的请求",
            "status": "failed",
            "details": {"reason": error},
        },)
        try:
            say = await self.expresser.express(
                channel_id=batch["channel_id"],
                outcome="unable",
                execution_results=failure_results,
                important_information=(),
            )
        except Exception:
            logger.exception("失败结果表达也失败，使用安全提示")
            say = "刚才那件事没有处理成功，我需要再试一下。"
        if not self.repository.mark_completed(
            batch["id"],
            batch["lease_token"],
            result={
                "outcome": "unable",
                "execution_results": list(failure_results),
                "important_information": [],
                SAY_KEY: say,
            },
            delivery_kind="message",
        ):
            raise BatchLeaseLost(str(batch["id"]))

    def _announce_finished(self, batch: dict[str, Any]) -> None:
        callback = self.on_batch_finished
        if callback is None:
            return
        try:
            result = callback(batch)
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        except Exception:
            logger.exception("工具批次 finished 回调失败: %s", batch.get("id"))
