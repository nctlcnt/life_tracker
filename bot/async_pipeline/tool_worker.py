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
SAY_KEY = "say"


class BatchLeaseLost(RuntimeError):
    pass


class ToolReplayMismatch(RuntimeError):
    pass


class ToolInvocationFailed(RuntimeError):
    pass


class ToolResultExpresser:
    """Re-read the latest conversation and give backend facts the chat voice."""

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
        batch_id: str,
        facts: tuple[str, ...],
        verbatim_terms: tuple[str, ...],
    ) -> str:
        async with self.generation_gate:
            history = self.db.get_recent_ai_messages(
                str(channel_id), limit=self.history_limit
            )
            request = result_expression_request(
                facts=facts,
                verbatim_terms=verbatim_terms,
                batch_id=batch_id,
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

        text = str(reply or "").strip()
        missing = [term for term in verbatim_terms if term not in text]
        if not text or "[SILENT]" in text or missing:
            # Facts are already the durable, exact fallback.  A style-model
            # failure must not turn into a silent delivery failure or mutate a
            # number while trying to sound natural.
            if missing:
                logger.warning(
                    "工具结果表达遗漏 verbatim terms，退回事实清单: %s", missing
                )
            text = "\n".join(facts).strip()
        if not text:
            raise ValueError("tool result expression produced no text")
        return text


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
            return await self._execute_one(
                batch,
                tool_names,
                call_records,
                name,
                arguments,
                provider_call_index,
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
            "facts": list(output.facts),
            "verbatim_terms": list(output.verbatim_terms),
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
                batch_id=batch_id,
                facts=output.facts,
                verbatim_terms=output.verbatim_terms,
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
    ) -> Any:
        if tool_name not in allowed_tools:
            raise ToolInvocationFailed(f"tool not allowed in this batch: {tool_name}")
        if not isinstance(arguments, dict):
            raise ToolInvocationFailed("tool arguments must be an object")

        existing = call_records.get(int(call_index))
        if existing is not None:
            if (
                existing["tool_name"] != tool_name
                or (existing.get("arguments") or {}) != arguments
            ):
                raise ToolReplayMismatch(
                    f"call {call_index} changed from "
                    f"{existing['tool_name']} {existing.get('arguments')} to "
                    f"{tool_name} {arguments}"
                )
            if existing["succeeded"]:
                return existing.get("result")

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

        facts = (f"后台处理失败：{error}",)
        try:
            say = await self.expresser.express(
                channel_id=batch["channel_id"],
                batch_id=batch["id"],
                facts=facts,
                verbatim_terms=(),
            )
        except Exception:
            logger.exception("失败结果表达也失败，退回原始错误")
            say = facts[0]
        if not self.repository.mark_completed(
            batch["id"],
            batch["lease_token"],
            result={
                "outcome": "unable",
                "facts": list(facts),
                "verbatim_terms": [],
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
