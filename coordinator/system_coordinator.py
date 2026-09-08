"""
系统协调器模块（system_coordinator.py）

SystemCoordinator 是第5组的核心组件，负责：
1. 注册并管理各组模块（Group1-4 及安全沙箱）
2. 按 6 步流程编排意图处理：校验→安全→规划→执行→工具→入库
3. 使用 ThreadPoolExecutor 进行超时控制（3s）
4. 集成审计日志和 RAG 知识库
"""

import logging
import os
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from group5.adapters.group1 import Group1AdapterError, adapt_group1_intent
from group5.adapters.group2 import (
    Group2AdapterError,
    adapt_group2_detection,
    adapt_group2_plan,
)
from group5.adapters.group3 import Group3AdapterError, adapt_group3_execute
from group5.adapters.group4 import (
    Group4AdapterError,
    adapt_group4_call,
    normalize_tool_request,
)
from group5.adapters.registry import ModuleRegistry
from group5.audit.audit_logger import AuditLogger
from group5.contracts.error_codes import ErrorCode, make_error
from group5.contracts.schemas import (
    CheckJSON,
    CONTRACT_VERSION_V1,
    HealthStatus,
    IntentJSON,
    OrchestrateResult,
    OrchestrateResultV1,
    StageSummaryV1,
    TaskEnvelopeV1,
    TraceRecord,
)
from group5.contracts.validation import (
    ContractValidationError,
    validate_task_envelope_v1,
)
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.coordinator.pending_intents import PendingIntentStore
from group5.coordinator.confirmations import ConfirmationStore, digest_intent
from group5.coordinator.stage_tracker import StageTracker
from group5.coordinator.tool_calls import ToolCallLedger
from group5.coordinator.task_scheduler import (
    CancellationToken,
    ScheduledTask,
    TaskScheduler,
)
from group5.security.security_sandbox import SecuritySandbox
from group5.storage.database import SQLiteStore


logger = logging.getLogger(__name__)

# 各阶段默认超时（秒）。不同阶段的工作量差异明显，不能继续使用统一3秒。
_DEFAULT_STAGE_TIMEOUTS: Dict[str, float] = {
    "intent": 20.0,
    "security": 1.0,
    "planning": 15.0,
    "detection": 5.0,
    "execution": 60.0,
    "tool_execution": 15.0,
}


class SystemCoordinator:
    """
    系统协调器

    作为各组模块的统一入口，编排意图处理的完整生命周期。

    注册模块约定（所有模块需实现以下接口）：
    - Group1（host_agent）: parse(raw_text) → IntentJSON
    - Group2（planner）   : plan(intent) → Dict[plan_result]
    - Group3（executor）  : execute(plan) → Dict[exec_result]
    - Group4（tool_registry）: call_tool(tool_name, params) → Dict
    - Group5（security）  : SecuritySandbox 实例（自动创建）
    """

    def __init__(
        self,
        rag_kb: Optional[RAGKnowledgeBase] = None,
        audit_logger: Optional[AuditLogger] = None,
        security_sandbox: Optional[SecuritySandbox] = None,
        pending_intent_ttl_seconds: float = 300.0,
        confirmation_ttl_seconds: float = 120.0,
        stage_timeouts: Optional[Dict[str, float]] = None,
        state_store: Optional[SQLiteStore] = None,
        task_scheduler: Optional[TaskScheduler] = None,
    ) -> None:
        """
        初始化系统协调器

        Args:
            rag_kb: RAG 知识库实例（可选，若为 None 则自动创建）
            audit_logger: 审计日志实例（可选，若为 None 则自动创建）
            security_sandbox: 安全沙箱实例（可选，若为 None 则自动创建）
            pending_intent_ttl_seconds: 待补参任务在内存中的有效秒数
            confirmation_ttl_seconds: 高风险确认请求的有效秒数
            stage_timeouts: 可选的阶段超时覆盖，单位为秒
            state_store: 可选的SQLite任务事实存储
            task_scheduler: 可选的GUI/变更/只读任务调度器
        """
        # 在创建后台线程或持久化组件前先校验配置，避免无效配置留下资源。
        validated_timeouts = dict(_DEFAULT_STAGE_TIMEOUTS)
        for stage_name, timeout_value in (stage_timeouts or {}).items():
            if stage_name not in validated_timeouts:
                raise ValueError(f"未知超时阶段: {stage_name}")
            if not isinstance(timeout_value, (int, float)) or timeout_value <= 0:
                raise ValueError(f"阶段超时必须是正数: {stage_name}")
            validated_timeouts[stage_name] = float(timeout_value)

        # 已注册的外部模块（group1~group4）。注册表在模块进入编排流程前
        # 校验组名、最小能力和契约版本，避免运行到中途才发现接口不可用。
        self._modules = ModuleRegistry()

        # 内部组件（默认启用本地持久化，满足审计与轨迹可追溯要求）
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        default_rag_file = os.path.join(data_dir, "rag_traces.json")
        default_audit_file = os.path.join(data_dir, "audit_events.jsonl")

        self._state_store = state_store
        self._task_scheduler = task_scheduler or TaskScheduler()
        self._rag_kb = rag_kb or RAGKnowledgeBase(
            persist_file=default_rag_file if state_store is None else None,
            state_store=state_store,
        )
        self._audit = audit_logger or AuditLogger(
            log_file=default_audit_file,
            state_store=state_store,
        )
        self._security = security_sandbox or SecuritySandbox()
        self._pending_intents = PendingIntentStore(pending_intent_ttl_seconds)
        self._confirmations = ConfirmationStore(confirmation_ttl_seconds)
        self._tool_calls = ToolCallLedger()

        self._stage_timeouts = validated_timeouts

        # 线程池（用于超时控制，最多 4 个并发调用）
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coord-worker")

        # 统计信息
        self._total_orchestrations: int = 0
        self._success_count: int = 0
        self._failure_count: int = 0
        self._paused_count: int = 0
        self._cancelled_count: int = 0

        logger.info("SystemCoordinator 初始化完成")

    def register(self, group_name: str, module: Any) -> None:
        """
        注册外部模块

        Args:
            group_name: 模块标识，如 "group1", "group2", "group3", "group4"
            module: 模块实例（需实现对应接口）

        Example:
            >>> coordinator.register("group2", planner_instance)
        """
        self._modules.register(group_name, module)
        logger.info("已注册模块: %s (%s)", group_name, type(module).__name__)

    def orchestrate_text(
        self,
        raw_text: str,
        history: Optional[Sequence[str]] = None,
    ) -> OrchestrateResultV1:
        """
        从自然语言开始执行第五组集成入口。

        Args:
            raw_text: 用户当前自然语言指令。
            history: 可选的最近历史指令，仅传递给第一组用于理解上下文。

        Returns:
            v1 编排结果。第一组失败时停在 intent 阶段；解析成功后进入与
            结构化核心入口相同的既有编排主流程。
        """
        started_at = time.perf_counter()
        task_trace_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        group1 = self._modules.get("group1")

        if group1 is None:
            error = {
                "code": ErrorCode.E0103.value,
                "message": "第一组 HostAgent 未注册或不可用",
                "retryable": True,
                "cause": {"module": "group1"},
            }
            return self._make_intent_failure_v1(
                task_trace_id, attempt_id, started_at, error, "failed"
            )

        try:
            envelope = self._call_with_retry(
                fn=lambda: adapt_group1_intent(
                    group1,
                    raw_text,
                    history=history,
                    task_trace_id=task_trace_id,
                    attempt_id=attempt_id,
                ),
                timeout=self._stage_timeouts["intent"],
                module_name="group1_host_agent",
                max_retry=0,
            )
        except FuturesTimeoutError:
            error = {
                "code": ErrorCode.E0103.value,
                "message": "第一组 HostAgent 响应超时",
                "retryable": True,
                "cause": {"module": "group1", "reason": "timeout"},
            }
            return self._make_intent_failure_v1(
                task_trace_id, attempt_id, started_at, error, "failed"
            )
        except Group1AdapterError as exc:
            status = "needs_input" if exc.code == ErrorCode.E0102 else "failed"
            if status == "needs_input":
                # 只保存继续解析所需的用户文本，不保存模型响应或安全凭据。
                context = [str(item) for item in (history or []) if str(item)]
                context.append(raw_text)
                self._pending_intents.put(task_trace_id, context)
            return self._make_intent_failure_v1(
                task_trace_id,
                attempt_id,
                started_at,
                exc.to_error_detail(),
                status,
            )

        intent_latency_ms = (time.perf_counter() - started_at) * 1000
        result = self.orchestrate_task(envelope)
        # 第一组解析是独立可观察阶段；后续状态机重构前，核心入口暂时保留
        # 一个汇总阶段，确保调用方已经能区分意图耗时和后续总耗时。
        result["stages"].insert(
            0,
            {
                "name": "intent",
                "status": "completed",
                "latency_ms": round(intent_latency_ms, 2),
                "attempts": 1,
            },
        )
        result["total_latency_ms"] = round(
            (time.perf_counter() - started_at) * 1000, 2
        )
        return result

    def continue_text(
        self,
        task_trace_id: str,
        supplemental_text: str,
    ) -> OrchestrateResultV1:
        """
        使用用户补充文本继续一个 needs_input 任务。

        Args:
            task_trace_id: 首次请求返回的稳定任务追踪 ID。
            supplemental_text: 用户针对缺失参数提供的新文本。

        Returns:
            新 attempt_id 对应的 v1 结果；任务不存在或过期时返回 E5001。
        """
        started_at = time.perf_counter()
        attempt_id = str(uuid.uuid4())
        pending = self._pending_intents.get(task_trace_id)
        if pending is None:
            error = {
                "code": ErrorCode.E5001.value,
                "message": "待补参任务不存在或已过期",
                "retryable": False,
            }
            return self._make_intent_failure_v1(
                task_trace_id or str(uuid.uuid4()),
                attempt_id,
                started_at,
                error,
                "failed",
            )

        group1 = self._modules.get("group1")
        if group1 is None:
            error = {
                "code": ErrorCode.E0103.value,
                "message": "第一组 HostAgent 未注册或不可用",
                "retryable": True,
                "cause": {"module": "group1"},
            }
            return self._make_intent_failure_v1(
                task_trace_id, attempt_id, started_at, error, "failed"
            )

        try:
            envelope = self._call_with_retry(
                fn=lambda: adapt_group1_intent(
                    group1,
                    supplemental_text,
                    history=pending["context"],
                    task_trace_id=task_trace_id,
                    attempt_id=attempt_id,
                ),
                timeout=self._stage_timeouts["intent"],
                module_name="group1_host_agent",
                max_retry=0,
            )
        except FuturesTimeoutError:
            error = {
                "code": ErrorCode.E0103.value,
                "message": "第一组 HostAgent 响应超时",
                "retryable": True,
                "cause": {"module": "group1", "reason": "timeout"},
            }
            return self._make_intent_failure_v1(
                task_trace_id, attempt_id, started_at, error, "failed"
            )
        except Group1AdapterError as exc:
            status = "needs_input" if exc.code == ErrorCode.E0102 else "failed"
            if status == "needs_input":
                self._pending_intents.put(
                    task_trace_id,
                    pending["context"] + [supplemental_text],
                )
            return self._make_intent_failure_v1(
                task_trace_id,
                attempt_id,
                started_at,
                exc.to_error_detail(),
                status,
            )

        # 只有成功形成完整意图后才移除待补参上下文，避免中途异常让用户
        # 无法继续原任务。
        self._pending_intents.remove(task_trace_id)
        intent_latency_ms = (time.perf_counter() - started_at) * 1000
        result = self.orchestrate_task(envelope)
        result["stages"].insert(
            0,
            {
                "name": "intent",
                "status": "completed",
                "latency_ms": round(intent_latency_ms, 2),
                "attempts": 1,
            },
        )
        result["total_latency_ms"] = round(
            (time.perf_counter() - started_at) * 1000, 2
        )
        return result

    def cancel_pending_task(self, task_trace_id: str) -> bool:
        """
        取消并清除尚未进入执行阶段的待补参任务。

        Args:
            task_trace_id: 待取消任务的稳定追踪 ID。

        Returns:
            任务存在且已清除时返回 True，否则返回 False。
        """
        removed = self._pending_intents.remove(task_trace_id)
        if removed:
            self._write_audit(
                task_trace_id,
                "intent_cancelled",
                "INFO",
                "用户取消待补参任务",
                {},
            )
        return removed

    def confirm_task(
        self,
        confirmation_id: str,
        approved: bool,
    ) -> OrchestrateResultV1:
        """
        响应一个规划前的高风险任务确认请求。

        Args:
            confirmation_id: needs_confirmation结果中返回的一次性确认ID。
            approved: 用户是否明确批准原任务快照。

        Returns:
            批准时首次进入规划和执行；拒绝、过期或重复确认时返回标准结果。
        """
        started_at = time.perf_counter()
        pending = self._confirmations.consume(confirmation_id)
        if pending is None:
            return {
                "contract_version": CONTRACT_VERSION_V1,
                "success": False,
                "status": "failed",
                "task_trace_id": str(uuid.uuid4()),
                "attempt_id": str(uuid.uuid4()),
                "stage": "security",
                "total_latency_ms": round(
                    (time.perf_counter() - started_at) * 1000, 2
                ),
                "stages": [
                    {
                        "name": "security",
                        "status": "failed",
                        "latency_ms": 0.0,
                        "attempts": 1,
                    }
                ],
                "result": None,
                "error": {
                    "code": ErrorCode.E1102.value,
                    "message": "确认请求不存在、已过期或已消费",
                    "retryable": False,
                },
            }

        task = pending["task"]
        if not approved:
            self._write_audit(
                task["task_trace_id"],
                "security_confirmation_denied",
                "INFO",
                "用户拒绝高风险操作",
                {"confirmation_id": confirmation_id},
            )
            return {
                "contract_version": CONTRACT_VERSION_V1,
                "success": False,
                "status": "cancelled",
                "task_trace_id": task["task_trace_id"],
                "attempt_id": task["attempt_id"],
                "stage": "security",
                "total_latency_ms": round(
                    (time.perf_counter() - started_at) * 1000, 2
                ),
                "stages": [
                    {
                        "name": "security",
                        "status": "cancelled",
                        "latency_ms": 0.0,
                        "attempts": 1,
                    }
                ],
                "result": None,
                "error": {
                    "code": ErrorCode.E1102.value,
                    "message": "用户拒绝高风险操作",
                    "retryable": False,
                },
            }

        intent = task["intent"]
        legacy_intent: IntentJSON = {
            "trace_id": task["task_trace_id"],
            "action": intent["action"],
            "target": intent["target"],
            "params": dict(intent["params"]),
            "raw_text": intent["raw_text"],
        }
        legacy_result, stage_summaries = self._orchestrate_with_stages(
            legacy_intent,
            task_envelope=task,
            confirmed_intent_digest=pending["intent_digest"],
        )
        result = self._legacy_result_to_v1(
            legacy_result,
            task["attempt_id"],
            started_at,
            stage_summaries,
        )
        self._record_v1_result(task, result)
        return result

    def submit_task(
        self,
        task_envelope: Any,
        kind: str,
    ) -> ScheduledTask:
        """
        通过共享桌面调度器异步提交v1任务。

        Args:
            task_envelope: 合法v1任务信封。
            kind: gui、mutation或read_only。

        Returns:
            可等待和取消的任务句柄。

        Raises:
            ContractValidationError: 提交前任务契约不合法。
            ValueError: 任务类型未知或同ID任务仍在运行。
        """
        envelope = validate_task_envelope_v1(task_envelope)
        return self._task_scheduler.submit(
            envelope["task_trace_id"],
            kind,
            lambda token: self.orchestrate_task(
                envelope,
                cancellation_token=token,
            ),
        )

    def cancel_task(self, task_trace_id: str) -> bool:
        """
        取消排队任务或请求运行任务在安全点停止。

        Args:
            task_trace_id: 待取消任务的稳定追踪ID。

        Returns:
            找到活动任务时返回True。
        """
        return self._task_scheduler.cancel(task_trace_id)

    def orchestrate_task(
        self,
        task_envelope: Any,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> OrchestrateResultV1:
        """
        执行已经结构化的 v1 核心入口。

        Args:
            task_envelope: 符合 TaskEnvelopeV1 的任务对象。
            cancellation_token: 调度器提供的可选协作式取消令牌。

        Returns:
            兼容现有协调器主流程的 v1 结果；非法输入以标准 E5001 返回，
            不向调用方泄漏运行时校验异常。
        """
        started_at = time.perf_counter()
        fallback_trace_id = str(uuid.uuid4())
        fallback_attempt_id = str(uuid.uuid4())
        if isinstance(task_envelope, dict):
            if isinstance(task_envelope.get("task_trace_id"), str):
                fallback_trace_id = task_envelope["task_trace_id"] or fallback_trace_id
            if isinstance(task_envelope.get("attempt_id"), str):
                fallback_attempt_id = task_envelope["attempt_id"] or fallback_attempt_id

        try:
            envelope = validate_task_envelope_v1(task_envelope)
        except ContractValidationError as exc:
            error = {
                "code": ErrorCode.E5001.value,
                "message": "结构化任务输入校验失败",
                "retryable": False,
                "detail": str(exc),
            }
            return self._make_intent_failure_v1(
                fallback_trace_id,
                fallback_attempt_id,
                started_at,
                error,
                "failed",
            )

        intent = envelope["intent"]
        self._record_task_state(envelope, "running")
        legacy_intent: IntentJSON = {
            "trace_id": envelope["task_trace_id"],
            "action": intent["action"],
            "target": intent["target"],
            "params": dict(intent["params"]),
            "raw_text": intent["raw_text"],
        }
        legacy_result, stage_summaries = self._orchestrate_with_stages(
            legacy_intent,
            task_envelope=envelope,
            cancellation_token=cancellation_token,
        )
        result = self._legacy_result_to_v1(
            legacy_result,
            envelope["attempt_id"],
            started_at,
            stage_summaries,
        )
        self._record_v1_result(envelope, result)
        return result

    def _record_task_state(self, task: TaskEnvelopeV1, status: str) -> None:
        """
        将任务和当前尝试状态写入可选SQLite存储。

        Args:
            task: 已通过校验的v1任务信封。
            status: 当前任务状态。
        """
        if self._state_store is None:
            return
        try:
            snapshot = dict(task)
            snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()
            self._state_store.record_task_envelope(
                snapshot, status  # type: ignore[arg-type]
            )
        except Exception as exc:
            # Ticket 14会将持久化故障纳入统一健康状态；当前兼容阶段不让
            # 可选存储破坏旧调用，但必须留下明确运行日志。
            logger.error("SQLite任务状态写入失败: %s", exc)

    def _record_v1_result(
        self,
        task: TaskEnvelopeV1,
        result: OrchestrateResultV1,
    ) -> None:
        """
        写入v1任务最终状态和阶段摘要。

        Args:
            task: 本次编排使用的标准任务信封。
            result: 已构建的v1编排结果。
        """
        if self._state_store is None:
            return
        self._record_task_state(task, result["status"])
        try:
            self._state_store.record_stage_summaries(
                task["attempt_id"],
                result["stages"],
            )
        except Exception as exc:
            logger.error("SQLite阶段摘要写入失败: %s", exc)

    def _record_tool_call_state(
        self,
        request: Dict[str, Any],
        status: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        将标准工具调用状态写入可选SQLite存储。

        Args:
            request: 已校验的工具请求。
            status: authorized、completed或failed。
            result: 可选的标准工具结果或错误摘要。
        """
        if self._state_store is None:
            return
        try:
            self._state_store.record_tool_call(
                request["tool_call_id"],
                request["attempt_id"],
                request["tool_name"],
                status,
                request["arguments"],
                result,
            )
        except Exception as exc:
            logger.error("SQLite工具调用状态写入失败: %s", exc)

    def _cancel_if_requested(
        self,
        token: Optional[CancellationToken],
        tracker: StageTracker,
        trace_id: str,
        stage: str,
        start_time: float,
    ) -> Optional[OrchestrateResult]:
        """
        在阶段安全点构造取消结果。

        Args:
            token: 调度器提供的可选取消令牌。
            tracker: 当前阶段追踪器。
            trace_id: 稳定任务追踪ID。
            stage: 当前准备进入的阶段。
            start_time: 本次核心编排起始时间。

        Returns:
            未取消时返回None；已取消时返回旧格式取消结果，由v1包装层转换。
        """
        if token is None or not token.cancelled:
            return None
        tracker.finish_if_active("cancelled")
        error = make_error(
            ErrorCode.E5004,
            extra={"status": "cancelled"},
        )
        self._write_audit(
            trace_id,
            "orchestrate_cancelled",
            "INFO",
            f"任务在{stage}阶段前取消",
            {"stage": stage},
        )
        return {
            "success": False,
            "trace_id": trace_id,
            "stage": stage,  # type: ignore[typeddict-item]
            "result": None,
            "error": error,
            "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
        }

    def _make_intent_failure_v1(
        self,
        task_trace_id: str,
        attempt_id: str,
        started_at: float,
        error: Dict[str, Any],
        status: str,
    ) -> OrchestrateResultV1:
        """
        构造意图阶段的 v1 失败或暂停结果。

        Args:
            task_trace_id: 在调用第一组前生成的任务追踪 ID。
            attempt_id: 当前意图解析尝试 ID。
            started_at: 本次入口调用的 perf_counter 起点。
            error: 已转换为第五组错误域的结构化错误。
            status: failed 或 needs_input。

        Returns:
            可直接返回给 AI Shell 的 v1 结果。
        """
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        self._total_orchestrations += 1
        self._failure_count += 1
        self._write_audit(
            task_trace_id,
            "intent_needs_input" if status == "needs_input" else "intent_failed",
            "WARN" if status == "needs_input" else "ERROR",
            error.get("message", "意图解析失败"),
            {"attempt_id": attempt_id, "error": dict(error)},
        )
        return {
            "contract_version": CONTRACT_VERSION_V1,
            "success": False,
            "status": status,  # type: ignore[typeddict-item]
            "task_trace_id": task_trace_id,
            "attempt_id": attempt_id,
            "stage": "intent",
            "total_latency_ms": latency_ms,
            "stages": [
                {
                    "name": "intent",
                    "status": status,
                    "latency_ms": latency_ms,
                    "attempts": 1,
                }
            ],
            "result": None,
            "error": error,  # type: ignore[typeddict-item]
        }

    def _legacy_result_to_v1(
        self,
        legacy_result: OrchestrateResult,
        attempt_id: str,
        started_at: float,
        stage_summaries: Optional[List[StageSummaryV1]] = None,
    ) -> OrchestrateResultV1:
        """
        将现有协调器结果包装为 v1 结果。

        Args:
            legacy_result: 旧编排核心返回结果。
            attempt_id: 当前结构化尝试 ID。
            started_at: 核心入口调用的 perf_counter 起点。
            stage_summaries: 核心编排实际产生的精简阶段摘要。

        Returns:
            保留旧结果语义并补齐版本、状态和阶段摘要的 v1 结果。
        """
        success = bool(legacy_result.get("success"))
        stage = legacy_result.get("stage", "completed")
        legacy_error = legacy_result.get("error") or {}
        error: Optional[Dict[str, Any]] = None
        result_status = "completed" if success else "failed"
        if not success:
            code = str(legacy_error.get("code", ErrorCode.E5003.value))
            if legacy_error.get("status") in {
                "needs_confirmation",
                "cancelled",
                "cancelled_with_side_effects",
                "partial_failure",
            }:
                result_status = str(legacy_error["status"])
            error = {
                "code": code,
                "message": str(legacy_error.get("message", "编排失败")),
                "retryable": code in {
                    ErrorCode.E5002.value,
                    ErrorCode.E1101.value,
                },
                "cause": dict(legacy_error),
            }
            for field in ("confirmation_id", "intent_digest", "risk_level"):
                if field in legacy_error:
                    error[field] = legacy_error[field]

        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        return {
            "contract_version": CONTRACT_VERSION_V1,
            "success": success,
            "status": result_status,
            "task_trace_id": legacy_result["trace_id"],
            "attempt_id": attempt_id,
            "stage": stage,
            "total_latency_ms": latency_ms,
            "stages": stage_summaries or [
                {
                    "name": stage,
                    "status": "completed" if success else "failed",
                    "latency_ms": legacy_result.get("latency_ms", latency_ms),
                    "attempts": 1,
                }
            ],
            "result": legacy_result.get("result"),
            "error": error,  # type: ignore[typeddict-item]
        }

    def health_check(self) -> Dict[str, HealthStatus]:
        """
        对所有已注册模块及内部组件进行健康检查

        Returns:
            模块名 → HealthStatus 的字典
        """
        results: Dict[str, HealthStatus] = {}

        # 检查内部组件
        for name, component in [
            ("security", self._security),
            ("rag_kb", self._rag_kb),
            ("audit", self._audit),
        ] + ([('state_store', self._state_store)] if self._state_store else []):
            try:
                ping_result = component.ping()
                results[name] = {
                    "module": name,
                    "healthy": ping_result.get("healthy", False),
                    "latency_ms": ping_result.get("latency_ms", 0.0),
                    "detail": None,
                }
            except Exception as exc:
                results[name] = {
                    "module": name,
                    "healthy": False,
                    "latency_ms": 0.0,
                    "detail": str(exc),
                }

        # 检查外部注册模块
        for group_name, module in self._modules.items():
            metadata = self._modules.metadata(group_name) or {}
            adapter_detail = (
                f"mode={metadata.get('mode', 'unknown')}, "
                f"contract={metadata.get('contract_version', 'unknown')}, "
                f"capabilities={','.join(metadata.get('capabilities', []))}"
            )
            try:
                if hasattr(module, "ping"):
                    ping_result = module.ping()
                    results[group_name] = {
                        "module": group_name,
                        "healthy": ping_result.get("healthy", False),
                        "latency_ms": ping_result.get("latency_ms", 0.0),
                        "detail": adapter_detail,
                    }
                else:
                    # 模块未实现 ping()，认为健康（兼容旧接口）
                    results[group_name] = {
                        "module": group_name,
                        "healthy": True,
                        "latency_ms": 0.0,
                        "detail": f"无 ping() 方法，默认健康；{adapter_detail}",
                    }
            except Exception as exc:
                results[group_name] = {
                    "module": group_name,
                    "healthy": False,
                    "latency_ms": 0.0,
                    "detail": str(exc),
                }

        return results

    def orchestrate(self, intent_json: IntentJSON) -> OrchestrateResult:
        """
        编排主方法：对用户意图执行完整的 6 步处理流程

        流程：
        1. 校验输入（validate_input）
        2. 安全检查（security sandbox）
        3. 规划（Group2 Planner）
        4. 执行（Group3 Executor）
        5. 工具调用（Group4 ToolRegistry）
        6. 入库（写 RAG + 审计）

        Args:
            intent_json: 用户意图 JSON（IntentJSON 格式）

        Returns:
            OrchestrateResult 编排结果
        """
        result, _ = self._orchestrate_with_stages(intent_json)
        return result

    def _orchestrate_with_stages(
        self,
        intent_json: Any,
        task_envelope: Optional[TaskEnvelopeV1] = None,
        confirmed_intent_digest: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Tuple[OrchestrateResult, List[StageSummaryV1]]:
        """
        执行旧核心流程并同时返回阶段摘要。

        Args:
            intent_json: 旧 IntentJSON 调用方提交的对象。
            task_envelope: v1入口已经校验的标准任务信封。
            confirmed_intent_digest: 已消费确认请求绑定的意图摘要。
            cancellation_token: 可选协作式取消令牌。

        Returns:
            旧 OrchestrateResult 与本次执行的阶段摘要元组。

        说明：
            该方法是 v1 入口与旧公开入口共享的唯一核心执行路径。它先安全
            处理非字典输入，再将每个顺序阶段交给 StageTracker 计时。
        """
        start_time = time.perf_counter()
        tracker = StageTracker()
        self._total_orchestrations += 1

        # 输入尚未通过校验时不能直接调用 .get()。这里仅提取可用追踪 ID，
        # 详细字段错误仍由 _validate_input() 统一返回 E5001。
        safe_intent = intent_json if isinstance(intent_json, dict) else {}
        raw_trace_id = safe_intent.get("trace_id", "")
        trace_id = raw_trace_id.strip() if isinstance(raw_trace_id, str) else ""
        if not trace_id:
            trace_id = str(uuid.uuid4())
            if isinstance(intent_json, dict):
                intent_json = dict(intent_json)
                intent_json["trace_id"] = trace_id

        action = safe_intent.get("action")
        target = safe_intent.get("target")
        logger.info("开始编排: trace_id=%s action=%s", trace_id, action)
        self._write_audit(
            trace_id,
            "orchestrate_start",
            "INFO",
            f"开始编排意图: action={action} target={target}",
            {"intent": dict(safe_intent)},
        )

        result = self._orchestrate_inner(
            intent_json,
            trace_id,
            start_time,
            tracker,
            task_envelope,
            confirmed_intent_digest,
            cancellation_token,
        )
        tracker.finish_if_active("completed" if result["success"] else "failed")

        result_status = (result.get("error") or {}).get("status")
        if result["success"]:
            self._success_count += 1
        elif result_status == "needs_confirmation":
            self._paused_count += 1
        elif result_status in {"cancelled", "cancelled_with_side_effects"}:
            self._cancelled_count += 1
        else:
            self._failure_count += 1

        return result, tracker.summaries()

    def _orchestrate_inner(
        self,
        intent_json: Any,
        trace_id: str,
        start_time: float,
        tracker: StageTracker,
        task_envelope: Optional[TaskEnvelopeV1],
        confirmed_intent_digest: Optional[str],
        cancellation_token: Optional[CancellationToken],
    ) -> OrchestrateResult:
        """
        内部编排实现（6 步流程）

        Args:
            intent_json: 意图 JSON
            trace_id: 追踪 ID
            start_time: 流程开始时间（perf_counter）
            tracker: 本次调用独占的阶段追踪器
            task_envelope: 可选的v1标准任务信封
            confirmed_intent_digest: 可选的已确认意图摘要
            cancellation_token: 可选协作式取消令牌

        Returns:
            OrchestrateResult
        """
        tracker.begin("security")
        cancelled = self._cancel_if_requested(
            cancellation_token, tracker, trace_id, "security", start_time
        )
        if cancelled is not None:
            return cancelled
        # ══════════════════════════════════════
        # 步骤 1：输入校验
        # ══════════════════════════════════════
        validation_error = self._validate_input(intent_json)
        if validation_error:
            return self._fail(
                trace_id, "security", start_time,
                make_error(ErrorCode.E5001, detail=validation_error),
                "orchestrate_validation_fail", f"输入校验失败: {validation_error}"
            )

        # ══════════════════════════════════════
        # 步骤 2：安全检查
        # ══════════════════════════════════════
        try:
            check_result: CheckJSON = self._call_with_retry(
                fn=lambda: self._security.check(intent_json),
                timeout=self._stage_timeouts["security"],
                module_name="security",
                max_retry=1,
                on_attempt=tracker.set_attempts,
            )
        except FuturesTimeoutError:
            return self._fail(
                trace_id, "security", start_time,
                make_error(ErrorCode.E5002, detail="安全沙箱响应超时"),
                "security_timeout", "安全沙箱超时"
            )
        except Exception as exc:
            return self._fail(
                trace_id, "security", start_time,
                make_error(ErrorCode.E5003, detail=str(exc)),
                "security_error", f"安全沙箱异常: {exc}"
            )

        if not check_result.get("approved", False):
            # 安全检查拒绝：记录审计并返回错误
            self._write_audit(
                trace_id, "security_block", "WARN",
                f"安全检查拒绝: {check_result.get('reason')}",
                {"check_result": dict(check_result)}
            )

            matched_policy = check_result.get("matched_policy")
            error_code = ErrorCode.E1001
            reason = str(check_result.get("reason", ""))
            target = str(intent_json.get("target", ""))

            # 路径穿越场景细分为 E1002，提升审计定位能力
            if target and "/../" in target:
                error_code = ErrorCode.E1002
            if "路径穿越" in reason:
                error_code = ErrorCode.E1002

            return self._fail(
                trace_id, "security", start_time,
                make_error(
                    error_code,
                    detail=check_result.get("reason", "安全检查未通过"),
                    extra={"risk_level": check_result.get("risk_level"), "policy": matched_policy},
                ),
                "security_block", "安全拦截"
            )

        if (
            check_result.get("risk_level") == "high"
            and task_envelope is not None
            and confirmed_intent_digest != digest_intent(task_envelope)
        ):
            # high风险在任何规划或执行副作用之前暂停。确认存储保存经过
            # 校验的任务快照，用户无法在确认响应中替换动作或参数。
            confirmation = self._confirmations.create(task_envelope)
            tracker.finish("needs_confirmation")
            error = make_error(
                ErrorCode.E1101,
                extra={
                    "status": "needs_confirmation",
                    "confirmation_id": confirmation["confirmation_id"],
                    "intent_digest": confirmation["intent_digest"],
                    "risk_level": "high",
                },
            )
            self._write_audit(
                trace_id,
                "security_needs_confirmation",
                "WARN",
                "高风险操作等待用户确认",
                {
                    "confirmation_id": confirmation["confirmation_id"],
                    "attempt_id": task_envelope["attempt_id"],
                    "risk_level": "high",
                },
            )
            return {
                "success": False,
                "trace_id": trace_id,
                "stage": "security",
                "result": None,
                "error": error,
                "latency_ms": round((time.perf_counter() - start_time) * 1000, 2),
            }

        # 安全检查通过，记录审计
        self._write_audit(
            trace_id, "security_pass", "INFO",
            f"安全检查通过: risk={check_result.get('risk_level')}",
            {"check_result": dict(check_result)}
        )
        tracker.finish("completed")

        # ══════════════════════════════════════
        # 步骤 3：规划（Group2 Planner）
        # ══════════════════════════════════════
        tracker.begin("planning")
        cancelled = self._cancel_if_requested(
            cancellation_token, tracker, trace_id, "planning", start_time
        )
        if cancelled is not None:
            return cancelled
        planner = self._modules.get("group2")
        if planner is None:
            return self._fail(
                trace_id, "planning", start_time,
                make_error(ErrorCode.E2001, detail="Group2 Planner 未注册"),
                "planner_unavailable", "规划模块未注册"
            )

        # 旧入口没有完整v1信封时构造兼容视图；该视图仅用于适配器调用，
        # 不改变旧IntentJSON的公开返回语义。
        effective_envelope: TaskEnvelopeV1 = task_envelope or {
            "contract_version": CONTRACT_VERSION_V1,
            "task_trace_id": trace_id,
            "attempt_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "intent": {
                "category": str(intent_json.get("intent", "legacy")),
                "action": str(intent_json.get("action", "")),
                "target": str(intent_json.get("target", "")),
                "params": dict(intent_json.get("params", {})),
                "raw_text": str(intent_json.get("raw_text", "")),
            },
        }

        try:
            planning_context = self._rag_kb.build_planning_context(
                effective_envelope["intent"]["raw_text"],
                top_k=3,
            )
        except Exception as exc:
            # 知识增强是可选能力，异常只记录类型并使用空上下文，不能阻塞
            # 基础规划和桌面任务执行。
            logger.warning("RAG规划上下文不可用: %s", type(exc).__name__)
            planning_context = []

        try:
            plan_result, combined_elements = self._call_with_retry(
                fn=lambda: adapt_group2_plan(
                    planner,
                    effective_envelope,
                    dict(intent_json),
                    planning_context,
                ),
                timeout=self._stage_timeouts["planning"],
                module_name="group2_planner",
                max_retry=1,
                on_attempt=tracker.set_attempts,
            )
        except FuturesTimeoutError:
            return self._fail(
                trace_id, "planning", start_time,
                make_error(ErrorCode.E5002, detail="Group2 Planner 响应超时"),
                "planner_timeout", "规划模块超时"
            )
        except Group2AdapterError as exc:
            return self._fail(
                trace_id, "planning", start_time,
                make_error(exc.code, detail=exc.detail),
                "planner_invalid", "规划结果格式无效"
            )
        except Exception as exc:
            return self._fail(
                trace_id, "planning", start_time,
                make_error(ErrorCode.E2001, detail=str(exc)),
                "planner_error", f"规划模块异常: {exc}"
            )

        self._write_audit(trace_id, "planning_complete", "INFO", "规划完成", {"plan": plan_result})
        tracker.finish("completed")

        # ══════════════════════════════════════
        # 步骤 4：控件检测（Group2 ControlDetector）
        # ══════════════════════════════════════
        tracker.begin("detection")
        cancelled = self._cancel_if_requested(
            cancellation_token, tracker, trace_id, "detection", start_time
        )
        if cancelled is not None:
            return cancelled
        try:
            detection_result = self._call_with_retry(
                fn=lambda: adapt_group2_detection(
                    planner,
                    effective_envelope,
                    plan_result,
                    combined_elements,
                ),
                timeout=self._stage_timeouts["detection"],
                module_name="group2_control_detector",
                max_retry=1,
                on_attempt=tracker.set_attempts,
            )
        except FuturesTimeoutError:
            return self._fail(
                trace_id, "detection", start_time,
                make_error(ErrorCode.E5002, detail="Group2 ControlDetector 响应超时"),
                "detection_timeout", "控件检测超时"
            )
        except Group2AdapterError as exc:
            return self._fail(
                trace_id, "detection", start_time,
                make_error(exc.code, detail=exc.detail),
                "detection_invalid", "控件检测结果格式无效"
            )
        except Exception as exc:
            return self._fail(
                trace_id, "detection", start_time,
                make_error(ErrorCode.E2003, detail=str(exc)),
                "detection_error", "控件检测模块异常"
            )

        self._write_audit(
            trace_id,
            "detection_complete",
            "INFO",
            f"控件检测完成: status={detection_result['status']}",
            {"detection": detection_result},
        )
        tracker.finish(detection_result["status"])

        # ══════════════════════════════════════
        # 步骤 5：执行（Group3 Executor）
        # ══════════════════════════════════════
        tracker.begin("execution")
        cancelled = self._cancel_if_requested(
            cancellation_token, tracker, trace_id, "execution", start_time
        )
        if cancelled is not None:
            return cancelled
        executor = self._modules.get("group3")
        if executor is None:
            return self._fail(
                trace_id, "execution", start_time,
                make_error(ErrorCode.E3001, detail="Group3 Executor 未注册"),
                "executor_unavailable", "执行模块未注册"
            )

        try:
            execution_result = self._call_with_retry(
                fn=lambda: adapt_group3_execute(
                    executor,
                    effective_envelope,
                    plan_result,
                    detection_result,
                ),
                timeout=self._stage_timeouts["execution"],
                module_name="group3_executor",
                max_retry=0,
                on_attempt=tracker.set_attempts,
            )
        except FuturesTimeoutError:
            return self._fail(
                trace_id, "execution", start_time,
                make_error(ErrorCode.E5002, detail="Group3 Executor 响应超时"),
                "executor_timeout", "执行模块超时"
            )
        except Group3AdapterError as exc:
            return self._fail(
                trace_id, "execution", start_time,
                make_error(exc.code, detail=exc.detail),
                "executor_invalid", "执行结果格式无效"
            )
        except Exception as exc:
            return self._fail(
                trace_id, "execution", start_time,
                make_error(ErrorCode.E3001, detail=str(exc)),
                "executor_error", f"执行模块异常: {exc}"
            )

        if execution_result["status"] == "needs_redetection":
            tracker.finish("needs_redetection")
            self._write_audit(
                trace_id,
                "execution_needs_redetection",
                "WARN",
                f"执行暂停，需重新检测控件: {execution_result['failed_step_id']}",
                {
                    "failed_step_id": execution_result["failed_step_id"],
                    "completed_step_ids": execution_result["completed_step_ids"],
                },
            )

            # 控件坐标依赖当前桌面状态，恢复时必须重新调用检测能力，不能
            # 重用规划阶段组合结果中的旧elements。
            tracker.begin("detection")
            cancelled = self._cancel_if_requested(
                cancellation_token, tracker, trace_id, "detection", start_time
            )
            if cancelled is not None:
                return cancelled
            try:
                refreshed_detection = self._call_with_retry(
                    fn=lambda: adapt_group2_detection(
                        planner,
                        effective_envelope,
                        plan_result,
                        combined_elements=None,
                    ),
                    timeout=self._stage_timeouts["detection"],
                    module_name="group2_control_detector_recovery",
                    max_retry=0,
                    on_attempt=tracker.set_attempts,
                )
            except FuturesTimeoutError:
                return self._fail(
                    trace_id, "detection", start_time,
                    make_error(ErrorCode.E5002, detail="恢复阶段控件检测超时"),
                    "redetection_timeout", "恢复阶段控件检测超时"
                )
            except Group2AdapterError as exc:
                return self._fail(
                    trace_id, "detection", start_time,
                    make_error(exc.code, detail=exc.detail),
                    "redetection_invalid", "恢复阶段控件检测结果无效"
                )
            except Exception as exc:
                return self._fail(
                    trace_id, "detection", start_time,
                    make_error(ErrorCode.E2003, detail=str(exc)),
                    "redetection_error", "恢复阶段控件检测异常"
                )

            if refreshed_detection["status"] != "completed":
                return self._fail(
                    trace_id, "detection", start_time,
                    make_error(ErrorCode.E2003, detail="第二组不支持恢复阶段重新检测"),
                    "redetection_unavailable", "恢复阶段缺少控件检测能力"
                )
            tracker.finish("completed")

            tracker.begin("execution")
            cancelled = self._cancel_if_requested(
                cancellation_token, tracker, trace_id, "execution", start_time
            )
            if cancelled is not None:
                return cancelled
            try:
                execution_result = self._call_with_retry(
                    fn=lambda: adapt_group3_execute(
                        executor,
                        effective_envelope,
                        plan_result,
                        refreshed_detection,
                        checkpoint=execution_result["checkpoint"],
                    ),
                    timeout=self._stage_timeouts["execution"],
                    module_name="group3_executor_recovery",
                    max_retry=0,
                    on_attempt=tracker.set_attempts,
                )
            except FuturesTimeoutError:
                return self._fail(
                    trace_id, "execution", start_time,
                    make_error(ErrorCode.E5002, detail="Group3 恢复执行超时"),
                    "executor_recovery_timeout", "执行恢复超时"
                )
            except Group3AdapterError as exc:
                return self._fail(
                    trace_id, "execution", start_time,
                    make_error(exc.code, detail=exc.detail),
                    "executor_recovery_invalid", "执行恢复结果无效"
                )
            except Exception as exc:
                return self._fail(
                    trace_id, "execution", start_time,
                    make_error(ErrorCode.E3001, detail=str(exc)),
                    "executor_recovery_error", "执行恢复异常"
                )

            if execution_result["status"] == "needs_redetection":
                return self._fail(
                    trace_id, "execution", start_time,
                    make_error(ErrorCode.E3003),
                    "executor_recovery_exhausted", "重新检测后执行仍无法恢复"
                )

        if execution_result["status"] == "failed":
            return self._fail(
                trace_id, "execution", start_time,
                make_error(ErrorCode.E3001, detail="第三组返回执行失败"),
                "executor_failed", "第三组执行失败"
            )

        if execution_result["status"] == "completed":
            tracker.finish("completed")
        else:
            # tool_required 将在下一票实现正式中介；此处保留活动阶段，避免
            # 将尚未完成的执行错误记录为 completed。
            tracker.finish(execution_result["status"])

        self._write_audit(
            trace_id,
            "execution_complete",
            "INFO",
            f"执行阶段返回: {execution_result['status']}",
            {"result": execution_result},
        )
        exec_result = execution_result["metadata"].get("legacy_payload", {})

        # ══════════════════════════════════════
        # 步骤 6：工具中介（Group3请求 → Group5调用Group4）
        # ══════════════════════════════════════
        tool_result: Optional[Any] = None
        raw_tool_request: Optional[Dict[str, Any]] = None
        resume_after_tool = execution_result["status"] == "tool_required"
        if resume_after_tool:
            raw_tool_request = execution_result.get("tool_request")
        elif exec_result.get("tool_name"):
            # legacy第三组已经完成执行后才返回工具提示，因此无需恢复；仍将
            # 其转换为标准请求并使用相同的去重与失败语义。
            raw_tool_request = {
                "tool_call_id": (
                    f"{trace_id}:{effective_envelope['attempt_id']}:"
                    f"{exec_result.get('tool_name')}"
                ),
                "tool_name": exec_result.get("tool_name"),
                "arguments": exec_result.get("tool_params", {}),
            }

        if raw_tool_request is not None:
            tracker.begin("tool_authorization")
            cancelled = self._cancel_if_requested(
                cancellation_token,
                tracker,
                trace_id,
                "tool_authorization",
                start_time,
            )
            if cancelled is not None:
                return cancelled
            tool_registry = self._modules.get("group4")
            if tool_registry is None:
                return self._fail(
                    trace_id, "tool_authorization", start_time,
                    make_error(ErrorCode.E4001, detail="Group4 ToolRegistry 未注册"),
                    "tool_registry_unavailable", "工具注册表未注册"
                )
            try:
                tool_request = normalize_tool_request(
                    raw_tool_request,
                    effective_envelope,
                )
            except Group4AdapterError as exc:
                return self._fail(
                    trace_id, "tool_authorization", start_time,
                    make_error(exc.code, detail=exc.detail),
                    "tool_request_invalid", "工具请求格式无效"
                )
            if not self._tool_calls.claim(tool_request["tool_call_id"]):
                return self._fail(
                    trace_id, "tool_authorization", start_time,
                    make_error(ErrorCode.E4004),
                    "tool_call_duplicate", "拒绝重复工具调用"
                )
            self._record_tool_call_state(tool_request, "authorized")
            tracker.finish("completed")

            tracker.begin("tool_execution")
            cancelled = self._cancel_if_requested(
                cancellation_token,
                tracker,
                trace_id,
                "tool_execution",
                start_time,
            )
            if cancelled is not None:
                return cancelled
            try:
                tool_result = self._call_with_retry(
                    fn=lambda: adapt_group4_call(tool_registry, tool_request),
                    timeout=self._stage_timeouts["tool_execution"],
                    module_name="group4_tool_registry",
                    max_retry=0,
                    on_attempt=tracker.set_attempts,
                )
            except FuturesTimeoutError:
                self._record_tool_call_state(
                    tool_request,
                    "failed",
                    {"code": ErrorCode.E5002.value, "reason": "timeout"},
                )
                return self._fail(
                    trace_id, "tool_execution", start_time,
                    make_error(ErrorCode.E5002, detail="Group4工具调用超时，执行状态未知"),
                    "tool_call_timeout", "工具调用超时"
                )
            except Group4AdapterError as exc:
                self._record_tool_call_state(
                    tool_request,
                    "failed",
                    {"code": exc.code.value, "reason": exc.detail},
                )
                return self._fail(
                    trace_id, "tool_execution", start_time,
                    make_error(exc.code, detail=exc.detail),
                    "tool_call_failed", "工具调用失败"
                )
            except Exception as exc:
                self._record_tool_call_state(
                    tool_request,
                    "failed",
                    {"code": ErrorCode.E4002.value},
                )
                return self._fail(
                    trace_id, "tool_execution", start_time,
                    make_error(ErrorCode.E4002, detail=str(exc)),
                    "tool_call_error", "工具调用异常"
                )
            tracker.finish("completed")
            self._record_tool_call_state(tool_request, "completed", tool_result)
            self._write_audit(
                trace_id,
                "tool_call_complete",
                "INFO",
                f"工具调用完成: {tool_request['tool_name']}",
                {"tool_call_id": tool_request["tool_call_id"], "result": tool_result},
            )

            if resume_after_tool:
                tracker.begin("execution")
                cancelled = self._cancel_if_requested(
                    cancellation_token, tracker, trace_id, "execution", start_time
                )
                if cancelled is not None:
                    return cancelled
                try:
                    execution_result = self._call_with_retry(
                        fn=lambda: adapt_group3_execute(
                            executor,
                            effective_envelope,
                            plan_result,
                            detection_result,
                            checkpoint=execution_result["checkpoint"],
                            tool_result=tool_result,
                        ),
                        timeout=self._stage_timeouts["execution"],
                        module_name="group3_executor_tool_resume",
                        max_retry=0,
                        on_attempt=tracker.set_attempts,
                    )
                except FuturesTimeoutError:
                    return self._fail(
                        trace_id, "execution", start_time,
                        make_error(ErrorCode.E5002, detail="工具完成后恢复执行超时"),
                        "tool_resume_timeout", "工具完成后恢复执行超时"
                    )
                except Group3AdapterError as exc:
                    return self._fail(
                        trace_id, "execution", start_time,
                        make_error(exc.code, detail=exc.detail),
                        "tool_resume_invalid", "工具完成后恢复结果无效"
                    )
                except Exception as exc:
                    return self._fail(
                        trace_id, "execution", start_time,
                        make_error(ErrorCode.E3001, detail=str(exc)),
                        "tool_resume_error", "工具完成后恢复执行异常"
                    )

                if execution_result["status"] != "completed":
                    return self._fail(
                        trace_id, "execution", start_time,
                        make_error(
                            ErrorCode.E3003,
                            detail=f"工具完成后返回状态: {execution_result['status']}",
                        ),
                        "tool_resume_incomplete", "工具完成后第三组未完成执行"
                    )
                tracker.finish("completed")
                exec_result = execution_result["metadata"].get("legacy_payload", {})

        # ══════════════════════════════════════
        # 步骤 6：结果入库（RAG + 审计）
        # ══════════════════════════════════════
        tracker.begin("persistence")
        cancelled = self._cancel_if_requested(
            cancellation_token, tracker, trace_id, "persistence", start_time
        )
        if cancelled is not None:
            return cancelled
        # 构建追踪记录摘要
        action = intent_json.get("action", "")
        target = intent_json.get("target", "")
        summary = (
            f"成功执行 {action} 操作，目标: {target}，"
            f"结果: {exec_result.get('output', '已完成')}"
        )

        # 异步写入 RAG（不阻塞主流程）
        module_metadata = [
            self._modules.metadata(name) or {}
            for name in self._modules.keys()
        ]
        is_mock_run = any(
            metadata.get("mode") == "mock"
            or str(metadata.get("implementation", "")).startswith("Mock")
            for metadata in module_metadata
        )
        self._store_trace_async(
            trace_id=trace_id,
            action=action,
            target=target,
            stage="completed",
            success=True,
            summary=summary,
            metadata={
                "plan": plan_result,
                "detection": detection_result,
                "exec": exec_result,
                "tool_result": tool_result,
                "check": dict(check_result),
                "is_mock": is_mock_run,
                "environment": (
                    self._state_store.environment
                    if self._state_store is not None
                    else "legacy"
                ),
                "orchestration_status": "completed",
            }
        )

        # 记录审计：编排成功完成
        self._write_audit(
            trace_id, "orchestrate_complete", "INFO",
            "编排完成，操作成功",
            {
                "action": action,
                "target": target,
                "exec_result": exec_result,
                "tool_result": tool_result,
            }
        )
        tracker.finish("completed")

        # 构建最终结果
        latency_ms = (time.perf_counter() - start_time) * 1000
        final_result = {
            "exec": exec_result,
            "tool": tool_result,
            "check": dict(check_result),
        }

        return {
            "success": True,
            "trace_id": trace_id,
            "stage": "completed",
            "result": final_result,
            "error": None,
            "latency_ms": round(latency_ms, 2),
        }

    def _call_with_retry(
        self,
        fn: Callable,
        timeout: float,
        module_name: str = "unknown",
        max_retry: int = 0,
        on_attempt: Optional[Callable[[int], None]] = None,
    ) -> Any:
        """
        使用 ThreadPoolExecutor 进行超时控制的模块调用（支持重试）

        Args:
            fn: 待调用的函数（无参数 lambda）
            timeout: 超时时间（秒）
            module_name: 模块名称（用于日志）
            max_retry: 最大重试次数
            on_attempt: 每次开始调用时接收当前尝试次数的可选回调

        Returns:
            函数返回值

        Raises:
            FuturesTimeoutError: 调用超时
            Exception: 调用异常（重试后仍失败）
        """
        last_exc: Optional[Exception] = None

        for attempt in range(max_retry + 1):
            try:
                if on_attempt is not None:
                    on_attempt(attempt + 1)
                future: Future = self._executor.submit(fn)
                result = future.result(timeout=timeout)
                return result
            except FuturesTimeoutError:
                # 尝试取消尚未开始的任务；已经运行的线程无法强制终止，调用方
                # 仍会立即收到超时结果，后续副作用调用不会因此自动重试。
                future.cancel()
                logger.warning(
                    "模块调用超时: module=%s, timeout=%.1fs (attempt %d/%d)",
                    module_name, timeout, attempt + 1, max_retry + 1
                )
                raise  # 超时不重试，直接上报
            except Exception as exc:
                last_exc = exc
                if attempt < max_retry:
                    logger.warning(
                        "模块调用失败，重试: module=%s, error=%s (attempt %d/%d)",
                        module_name, exc, attempt + 1, max_retry + 1
                    )
                    time.sleep(0.05)  # 短暂等待后重试
                else:
                    raise

        raise last_exc  # type: ignore[misc]

    def _validate_input(self, intent_json: IntentJSON) -> Optional[str]:
        """
        校验输入的 IntentJSON 是否包含必要字段

        Args:
            intent_json: 待校验的意图 JSON

        Returns:
            校验失败时返回错误描述字符串；通过则返回 None
        """
        if not isinstance(intent_json, dict):
            return "intent_json 必须是字典类型"

        required_fields = ["action", "target"]
        missing = [f for f in required_fields if not intent_json.get(f)]
        if missing:
            return f"缺少必要字段: {', '.join(missing)}"

        return None

    def _fail(
        self,
        trace_id: str,
        stage: str,
        start_time: float,
        error: Dict[str, Any],
        audit_event_type: str,
        audit_message: str,
    ) -> OrchestrateResult:
        """
        构建失败结果并记录审计

        Args:
            trace_id: 追踪 ID
            stage: 失败阶段
            start_time: 开始时间（perf_counter）
            error: 错误字典（make_error 构造）
            audit_event_type: 审计事件类型
            audit_message: 审计消息

        Returns:
            OrchestrateResult（success=False）
        """
        latency_ms = (time.perf_counter() - start_time) * 1000

        # 记录审计：失败事件
        self._write_audit(
            trace_id, audit_event_type, "ERROR",
            audit_message,
            {"error": error, "stage": stage}
        )

        # 将失败记录写入 RAG
        self._store_trace_async(
            trace_id=trace_id,
            action="",
            target="",
            stage=stage,  # type: ignore[arg-type]
            success=False,
            summary=f"编排失败（阶段: {stage}）: {audit_message}",
            metadata={"error": error}
        )

        logger.warning(
            "编排失败: trace_id=%s stage=%s error=%s",
            trace_id, stage, error.get("code")
        )

        return {
            "success": False,
            "trace_id": trace_id,
            "stage": stage,  # type: ignore[typeddict-item]
            "result": None,
            "error": error,
            "latency_ms": round(latency_ms, 2),
        }

    def _write_audit(
        self,
        trace_id: str,
        event_type: str,
        level: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        写入审计日志（异步，不阻塞主流程）

        Args:
            trace_id: 追踪 ID
            event_type: 事件类型
            level: 日志级别
            message: 消息描述
            payload: 额外数据
        """
        try:
            self._audit.log(
                trace_id=trace_id,
                event_type=event_type,
                message=message,
                level=level,
                payload=payload or {},
            )
        except Exception as exc:
            # 审计失败不影响主流程
            logger.error("审计日志写入失败: %s", exc)

    def _store_trace_async(
        self,
        trace_id: str,
        action: str,
        target: str,
        stage: str,
        success: bool,
        summary: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        异步将追踪记录写入 RAG 知识库（不阻塞主流程）

        Args:
            trace_id: 追踪 ID
            action: 操作动词
            target: 操作目标
            stage: 最终阶段
            success: 是否成功
            summary: 自然语言摘要
            metadata: 额外元数据
        """
        def _store():
            record: TraceRecord = {
                "trace_id": trace_id,
                "action": action,
                "target": target,
                "stage": stage,  # type: ignore[typeddict-item]
                "success": success,
                "summary": summary,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata or {},
            }
            try:
                self._rag_kb.add_trace(record)
            except Exception as exc:
                logger.error("RAG 写入失败: %s", exc)

        # 异步提交到线程池（fire-and-forget）
        self._executor.submit(_store)

    def shutdown(self, wait: bool = True) -> None:
        """
        优雅关闭协调器（停止线程池和审计日志后台线程）

        Args:
            wait: 是否等待线程池任务完成（默认 True）
        """
        logger.info("协调器正在关闭...")
        self._task_scheduler.shutdown(wait=wait)
        self._executor.shutdown(wait=wait)
        self._audit.shutdown(wait=wait)
        if self._state_store is not None:
            self._state_store.close()
        logger.info("协调器已关闭")

    def stats(self) -> Dict[str, Any]:
        """
        返回协调器统计信息

        Returns:
            统计字典
        """
        return {
            "total_orchestrations": self._total_orchestrations,
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "paused_count": self._paused_count,
            "cancelled_count": self._cancelled_count,
            "success_rate": round(
                self._success_count / self._total_orchestrations, 4
            ) if self._total_orchestrations > 0 else 0.0,
            "registered_modules": list(self._modules.keys()),
            "rag_stats": self._rag_kb.stats(),
            "audit_count": self._audit.count(),
            "security_stats": self._security.risk_score(),
        }


if __name__ == "__main__":
    # 独立运行示例（需要先创建 mock 模块）
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

    from group5.mocks.mock_modules import (
        MockGroup2Planner,
        MockGroup3Executor,
        MockGroup4ToolRegistry,
    )

    print("=== SystemCoordinator 独立运行示例 ===\n")

    coordinator = SystemCoordinator()
    coordinator.register("group2", MockGroup2Planner())
    coordinator.register("group3", MockGroup3Executor())
    coordinator.register("group4", MockGroup4ToolRegistry())

    print("健康检查:")
    health = coordinator.health_check()
    for name, status in health.items():
        mark = "✅" if status["healthy"] else "❌"
        print(f"  {mark} {name}: latency={status['latency_ms']:.1f}ms")

    print("\n=== 场景1：正常打开文件夹 ===")
    intent: IntentJSON = {
        "trace_id": "test-001",
        "action": "open",
        "target": "/home/user/Documents",
        "params": {},
        "raw_text": "打开文档文件夹",
    }
    result = coordinator.orchestrate(intent)
    print(f"成功: {result['success']}, 阶段: {result['stage']}, 耗时: {result['latency_ms']:.1f}ms")

    print("\n=== 场景2：危险操作被拦截 ===")
    intent2: IntentJSON = {
        "trace_id": "test-002",
        "action": "rm",
        "target": "-rf /",
        "params": {},
        "raw_text": "删除所有文件",
    }
    result2 = coordinator.orchestrate(intent2)
    print(f"成功: {result2['success']}, 错误码: {result2['error']['code']}")

    time.sleep(0.3)  # 等待异步入库完成
    print("\n=== 统计信息 ===")
    stats = coordinator.stats()
    print(f"总编排次数: {stats['total_orchestrations']}")
    print(f"成功率: {stats['success_rate'] * 100:.1f}%")
    print(f"RAG 记录数: {stats['rag_stats']['total_records']}")

    coordinator.shutdown()
    print("\n✅ system_coordinator.py 验证通过")
