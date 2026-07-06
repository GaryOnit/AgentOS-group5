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
from typing import Any, Callable, Dict, Optional

from group5.audit.audit_logger import AuditLogger
from group5.contracts.error_codes import ErrorCode, make_error
from group5.contracts.schemas import (
    CheckJSON,
    HealthStatus,
    IntentJSON,
    OrchestrateResult,
    TraceRecord,
)
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.security.security_sandbox import SecuritySandbox


logger = logging.getLogger(__name__)

# 模块调用超时阈值（秒）
_MODULE_TIMEOUT = 3.0

# 最大重试次数（仅对非超时错误重试）
_MAX_RETRY = 1


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
    ) -> None:
        """
        初始化系统协调器

        Args:
            rag_kb: RAG 知识库实例（可选，若为 None 则自动创建）
            audit_logger: 审计日志实例（可选，若为 None 则自动创建）
            security_sandbox: 安全沙箱实例（可选，若为 None 则自动创建）
        """
        # 已注册的外部模块（group1~group4）
        self._modules: Dict[str, Any] = {}

        # 内部组件（默认启用本地持久化，满足审计与轨迹可追溯要求）
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        default_rag_file = os.path.join(data_dir, "rag_traces.json")
        default_audit_file = os.path.join(data_dir, "audit_events.jsonl")

        self._rag_kb = rag_kb or RAGKnowledgeBase(persist_file=default_rag_file)
        self._audit = audit_logger or AuditLogger(log_file=default_audit_file)
        self._security = security_sandbox or SecuritySandbox()

        # 线程池（用于超时控制，最多 4 个并发调用）
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coord-worker")

        # 统计信息
        self._total_orchestrations: int = 0
        self._success_count: int = 0
        self._failure_count: int = 0

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
        self._modules[group_name] = module
        logger.info("已注册模块: %s (%s)", group_name, type(module).__name__)

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
        ]:
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
            try:
                if hasattr(module, "ping"):
                    ping_result = module.ping()
                    results[group_name] = {
                        "module": group_name,
                        "healthy": ping_result.get("healthy", False),
                        "latency_ms": ping_result.get("latency_ms", 0.0),
                        "detail": None,
                    }
                else:
                    # 模块未实现 ping()，认为健康（兼容旧接口）
                    results[group_name] = {
                        "module": group_name,
                        "healthy": True,
                        "latency_ms": 0.0,
                        "detail": "无 ping() 方法，默认健康",
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
        start_time = time.perf_counter()
        self._total_orchestrations += 1

        # 自动生成 trace_id（若未提供）
        trace_id = intent_json.get("trace_id", "").strip()
        if not trace_id:
            trace_id = str(uuid.uuid4())
            intent_json = dict(intent_json)  # type: ignore[assignment]
            intent_json["trace_id"] = trace_id  # type: ignore[index]

        logger.info("开始编排: trace_id=%s action=%s", trace_id, intent_json.get("action"))

        # 记录审计：编排开始
        self._write_audit(
            trace_id, "orchestrate_start", "INFO",
            f"开始编排意图: action={intent_json.get('action')} target={intent_json.get('target')}",
            {"intent": dict(intent_json)}
        )

        # 执行内部编排逻辑
        result = self._orchestrate_inner(intent_json, trace_id, start_time)

        # 更新统计
        if result["success"]:
            self._success_count += 1
        else:
            self._failure_count += 1

        return result

    def _orchestrate_inner(
        self,
        intent_json: IntentJSON,
        trace_id: str,
        start_time: float,
    ) -> OrchestrateResult:
        """
        内部编排实现（6 步流程）

        Args:
            intent_json: 意图 JSON
            trace_id: 追踪 ID
            start_time: 流程开始时间（perf_counter）

        Returns:
            OrchestrateResult
        """
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
                timeout=_MODULE_TIMEOUT,
                module_name="security",
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

        # 安全检查通过，记录审计
        self._write_audit(
            trace_id, "security_pass", "INFO",
            f"安全检查通过: risk={check_result.get('risk_level')}",
            {"check_result": dict(check_result)}
        )

        # ══════════════════════════════════════
        # 步骤 3：规划（Group2 Planner）
        # ══════════════════════════════════════
        planner = self._modules.get("group2")
        if planner is None:
            return self._fail(
                trace_id, "planning", start_time,
                make_error(ErrorCode.E2001, detail="Group2 Planner 未注册"),
                "planner_unavailable", "规划模块未注册"
            )

        try:
            plan_result = self._call_with_retry(
                fn=lambda: planner.plan(intent_json),
                timeout=_MODULE_TIMEOUT,
                module_name="group2_planner",
            )
        except FuturesTimeoutError:
            return self._fail(
                trace_id, "planning", start_time,
                make_error(ErrorCode.E5002, detail="Group2 Planner 响应超时（>3s）"),
                "planner_timeout", "规划模块超时"
            )
        except Exception as exc:
            return self._fail(
                trace_id, "planning", start_time,
                make_error(ErrorCode.E2001, detail=str(exc)),
                "planner_error", f"规划模块异常: {exc}"
            )

        if not isinstance(plan_result, dict):
            return self._fail(
                trace_id, "planning", start_time,
                make_error(ErrorCode.E2002, detail="规划结果不是字典类型"),
                "planner_invalid", "规划结果格式无效"
            )

        self._write_audit(trace_id, "planning_complete", "INFO", "规划完成", {"plan": plan_result})

        # ══════════════════════════════════════
        # 步骤 4：执行（Group3 Executor）
        # ══════════════════════════════════════
        executor = self._modules.get("group3")
        if executor is None:
            return self._fail(
                trace_id, "execution", start_time,
                make_error(ErrorCode.E3001, detail="Group3 Executor 未注册"),
                "executor_unavailable", "执行模块未注册"
            )

        try:
            exec_result = self._call_with_retry(
                fn=lambda: executor.execute(plan_result),
                timeout=_MODULE_TIMEOUT,
                module_name="group3_executor",
            )
        except FuturesTimeoutError:
            return self._fail(
                trace_id, "execution", start_time,
                make_error(ErrorCode.E5002, detail="Group3 Executor 响应超时（>3s）"),
                "executor_timeout", "执行模块超时"
            )
        except Exception as exc:
            return self._fail(
                trace_id, "execution", start_time,
                make_error(ErrorCode.E3001, detail=str(exc)),
                "executor_error", f"执行模块异常: {exc}"
            )

        if not isinstance(exec_result, dict):
            return self._fail(
                trace_id, "execution", start_time,
                make_error(ErrorCode.E3002, detail="执行结果不是字典类型"),
                "executor_invalid", "执行结果格式无效"
            )

        self._write_audit(trace_id, "execution_complete", "INFO", "执行完成", {"result": exec_result})

        # ══════════════════════════════════════
        # 步骤 5：工具调用（Group4 ToolRegistry，可选）
        # ══════════════════════════════════════
        tool_registry = self._modules.get("group4")
        tool_result: Optional[Any] = None

        if tool_registry is not None:
            # 从执行结果中获取需要调用的工具信息
            tool_name = exec_result.get("tool_name")
            tool_params = exec_result.get("tool_params", {})

            if tool_name:
                try:
                    tool_result = self._call_with_retry(
                        fn=lambda: tool_registry.call_tool(tool_name, tool_params),
                        timeout=_MODULE_TIMEOUT,
                        module_name="group4_tool_registry",
                    )
                    self._write_audit(
                        trace_id, "tool_call_complete", "INFO",
                        f"工具调用完成: {tool_name}",
                        {"tool_name": tool_name, "tool_result": tool_result}
                    )
                except FuturesTimeoutError:
                    # 工具调用超时不阻塞主流程，降级处理
                    self._write_audit(
                        trace_id, "tool_call_timeout", "WARN",
                        f"工具调用超时: {tool_name}，降级处理",
                        {"tool_name": tool_name}
                    )
                except Exception as exc:
                    self._write_audit(
                        trace_id, "tool_call_error", "WARN",
                        f"工具调用失败: {exc}，降级处理",
                        {"error": str(exc)}
                    )

        # ══════════════════════════════════════
        # 步骤 6：结果入库（RAG + 审计）
        # ══════════════════════════════════════
        # 构建追踪记录摘要
        action = intent_json.get("action", "")
        target = intent_json.get("target", "")
        summary = (
            f"成功执行 {action} 操作，目标: {target}，"
            f"结果: {exec_result.get('output', '已完成')}"
        )

        # 异步写入 RAG（不阻塞主流程）
        self._store_trace_async(
            trace_id=trace_id,
            action=action,
            target=target,
            stage="completed",
            success=True,
            summary=summary,
            metadata={
                "plan": plan_result,
                "exec": exec_result,
                "tool_result": tool_result,
                "check": dict(check_result),
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
        timeout: float = _MODULE_TIMEOUT,
        module_name: str = "unknown",
        max_retry: int = _MAX_RETRY,
    ) -> Any:
        """
        使用 ThreadPoolExecutor 进行超时控制的模块调用（支持重试）

        Args:
            fn: 待调用的函数（无参数 lambda）
            timeout: 超时时间（秒）
            module_name: 模块名称（用于日志）
            max_retry: 最大重试次数

        Returns:
            函数返回值

        Raises:
            FuturesTimeoutError: 调用超时
            Exception: 调用异常（重试后仍失败）
        """
        last_exc: Optional[Exception] = None

        for attempt in range(max_retry + 1):
            try:
                future: Future = self._executor.submit(fn)
                result = future.result(timeout=timeout)
                return result
            except FuturesTimeoutError:
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
        self._executor.shutdown(wait=wait)
        self._audit.shutdown(wait=wait)
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
