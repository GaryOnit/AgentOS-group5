"""第一组适配和第五组双层入口的外部行为测试。"""

import unittest

from group5.audit.audit_logger import AuditLogger
from group5.contracts import CONTRACT_VERSION_V1, validate_orchestrate_result_v1
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import (
    MockGroup2Planner,
    MockGroup3Executor,
    MockGroup4ToolRegistry,
)


class _SuccessfulGroup1:
    """返回第一组四字段成功结果的测试适配器。"""

    def understand_intent(self, user_input: str, history=None) -> dict:
        """
        返回固定文件管理器意图。

        Args:
            user_input: 当前用户指令。
            history: 可选历史，仅用于模拟真实签名。

        Returns:
            第一组成功结果。
        """
        return {
            "intent": "应用控制",
            "target": "Files",
            "action": "open",
            "params": {},
        }


class _ErrorGroup1:
    """返回指定第一组错误码的测试适配器。"""

    def __init__(self, code: int) -> None:
        """
        初始化错误适配器。

        Args:
            code: 第一组 1001 至 1004 错误码。
        """
        self.code = code

    def understand_intent(self, user_input: str, history=None) -> dict:
        """
        返回标准上游错误。

        Args:
            user_input: 当前用户指令。
            history: 可选历史。

        Returns:
            第一组错误对象。
        """
        return {"error": "上游错误", "code": self.code, "hint": "测试提示"}


class _MalformedGroup1:
    """返回语义不合法成功对象的测试适配器。"""

    def understand_intent(self, user_input: str, history=None) -> dict:
        """返回缺少 target 的非法结果。"""
        return {"intent": "应用控制", "action": "open", "params": {}}


class _DangerousGroup1:
    """返回受保护系统路径访问的第一组测试适配器。"""

    def understand_intent(self, user_input: str, history=None) -> dict:
        """返回会在安全阶段被拒绝的合法第一组意图。"""
        return {
            "intent": "文件操作",
            "target": "/etc/passwd",
            "action": "navigate",
            "params": {},
        }


class _ExplodingGroup1:
    """调用时抛异常的测试适配器。"""

    def understand_intent(self, user_input: str, history=None) -> dict:
        """模拟第一组运行时故障。"""
        raise RuntimeError("group1 unavailable")


class _ContextualGroup1:
    """需要第二轮补充目标目录才能形成完整意图的测试适配器。"""

    def understand_intent(self, user_input: str, history=None) -> dict:
        """
        根据历史上下文返回缺参或完整移动意图。

        Args:
            user_input: 当前用户输入。
            history: 之前保存的任务上下文。

        Returns:
            第一轮返回1002，带正确上下文的第二轮返回成功意图。
        """
        history = history or []
        if "移动文件" in history and "下载" in user_input:
            return {
                "intent": "文件操作",
                "target": "report.txt",
                "action": "move",
                "params": {
                    "source": "/home/user/report.txt",
                    "destination": "/home/user/Downloads",
                },
            }
        return {"error": "提取参数缺失", "code": 1002, "hint": "请补充目标目录"}


def _coordinator(group1=None, pending_ttl_seconds: float = 300.0) -> SystemCoordinator:
    """
    构造使用纯内存组件的完整协调器。

    Args:
        group1: 可选第一组测试适配器。
        pending_ttl_seconds: 待补参上下文有效秒数。

    Returns:
        已注册第2至第4组Mock的协调器。
    """
    coordinator = SystemCoordinator(
        rag_kb=RAGKnowledgeBase(),
        audit_logger=AuditLogger(),
        pending_intent_ttl_seconds=pending_ttl_seconds,
    )
    if group1 is not None:
        coordinator.register("group1", group1)
    coordinator.register("group2", MockGroup2Planner())
    coordinator.register("group3", MockGroup3Executor())
    coordinator.register("group4", MockGroup4ToolRegistry())
    return coordinator


class TestGroup1Integration(unittest.TestCase):
    """验证自然语言入口和结构化核心入口。"""

    def test_text_entry_wraps_success_and_runs_core_flow(self) -> None:
        """第一组成功结果应包装后进入既有核心编排流程。"""
        coordinator = _coordinator(_SuccessfulGroup1())
        try:
            result = coordinator.orchestrate_text("打开文件管理器")

            validate_orchestrate_result_v1(result)
            self.assertTrue(result["success"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["contract_version"], CONTRACT_VERSION_V1)
            self.assertTrue(result["task_trace_id"])
            self.assertTrue(result["attempt_id"])
            self.assertEqual(
                [stage["name"] for stage in result["stages"]],
                [
                    "intent",
                    "security",
                    "planning",
                    "detection",
                    "execution",
                    "tool_authorization",
                    "tool_execution",
                    "persistence",
                ],
            )
        finally:
            coordinator.shutdown(wait=True)

    def test_upstream_errors_are_mapped_without_entering_core(self) -> None:
        """第一组四种错误应使用独立E01xx错误域。"""
        expected = {
            1001: ("E0101", "failed"),
            1002: ("E0102", "needs_input"),
            1003: ("E0103", "failed"),
            1004: ("E0104", "failed"),
        }
        for upstream_code, (mapped_code, status) in expected.items():
            with self.subTest(upstream_code=upstream_code):
                coordinator = _coordinator(_ErrorGroup1(upstream_code))
                try:
                    result = coordinator.orchestrate_text("测试指令")
                    validate_orchestrate_result_v1(result)
                    self.assertFalse(result["success"])
                    self.assertEqual(result["status"], status)
                    self.assertEqual(result["error"]["code"], mapped_code)
                    self.assertEqual(
                        result["error"]["cause"]["code"], upstream_code
                    )
                    self.assertEqual(result["stage"], "intent")
                finally:
                    coordinator.shutdown(wait=True)

    def test_malformed_group1_result_maps_to_e0104(self) -> None:
        """格式不完整的第一组成功对象必须被拒绝。"""
        coordinator = _coordinator(_MalformedGroup1())
        try:
            result = coordinator.orchestrate_text("打开文件管理器")
            self.assertEqual(result["error"]["code"], "E0104")
            self.assertEqual(result["stage"], "intent")
        finally:
            coordinator.shutdown(wait=True)

    def test_group1_exception_maps_to_retryable_e0103(self) -> None:
        """第一组运行时异常应标准化且不得泄漏原始异常文本。"""
        coordinator = _coordinator(_ExplodingGroup1())
        try:
            result = coordinator.orchestrate_text("打开文件管理器")
            self.assertEqual(result["error"]["code"], "E0103")
            self.assertTrue(result["error"]["retryable"])
            self.assertNotIn("group1 unavailable", str(result))
        finally:
            coordinator.shutdown(wait=True)

    def test_unregistered_group1_returns_e0103(self) -> None:
        """自然语言入口缺少第一组时应返回结构化失败而非抛异常。"""
        coordinator = _coordinator()
        try:
            result = coordinator.orchestrate_text("打开文件管理器")
            self.assertEqual(result["error"]["code"], "E0103")
            self.assertEqual(result["stage"], "intent")
        finally:
            coordinator.shutdown(wait=True)

    def test_structured_entry_does_not_call_group1(self) -> None:
        """直接提交标准任务时不得再次调用第一组。"""
        coordinator = _coordinator(_ExplodingGroup1())
        task = {
            "contract_version": CONTRACT_VERSION_V1,
            "task_trace_id": "task-direct-001",
            "attempt_id": "attempt-direct-001",
            "timestamp": "2026-09-07T08:00:00+00:00",
            "intent": {
                "category": "应用控制",
                "action": "open",
                "target": "Files",
                "params": {},
                "raw_text": "打开文件管理器",
            },
        }
        try:
            result = coordinator.orchestrate_task(task)
            validate_orchestrate_result_v1(result)
            self.assertTrue(result["success"])
            self.assertEqual(result["task_trace_id"], "task-direct-001")
        finally:
            coordinator.shutdown(wait=True)

    def test_security_failure_has_intent_and_security_summaries(self) -> None:
        """意图成功但安全拒绝时应准确标识停止阶段。"""
        coordinator = _coordinator(_DangerousGroup1())
        try:
            result = coordinator.orchestrate_text("删除所有文件")

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "security")
            self.assertEqual(
                [stage["name"] for stage in result["stages"]],
                ["intent", "security"],
            )
            self.assertEqual(result["stages"][-1]["status"], "failed")
        finally:
            coordinator.shutdown(wait=True)

    def test_invalid_legacy_input_returns_error_instead_of_crashing(self) -> None:
        """旧入口收到非字典输入时应返回E5001而不是调用.get崩溃。"""
        coordinator = _coordinator(_SuccessfulGroup1())
        try:
            result = coordinator.orchestrate(None)  # type: ignore[arg-type]

            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], "E5001")
            self.assertEqual(result["stage"], "security")
        finally:
            coordinator.shutdown(wait=True)

    def test_needs_input_can_continue_same_task_with_new_attempt(self) -> None:
        """补充文本应续接原任务并使用新的尝试ID。"""
        coordinator = _coordinator(_ContextualGroup1())
        try:
            first = coordinator.orchestrate_text("移动文件")
            second = coordinator.continue_text(
                first["task_trace_id"], "移到下载目录"
            )

            self.assertEqual(first["status"], "needs_input")
            self.assertTrue(second["success"])
            self.assertEqual(second["task_trace_id"], first["task_trace_id"])
            self.assertNotEqual(second["attempt_id"], first["attempt_id"])
        finally:
            coordinator.shutdown(wait=True)

    def test_unknown_pending_task_cannot_continue(self) -> None:
        """不存在的任务ID不得借用其他会话上下文。"""
        coordinator = _coordinator(_ContextualGroup1())
        try:
            result = coordinator.continue_text("missing-task", "移到下载目录")

            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], "E5001")
        finally:
            coordinator.shutdown(wait=True)

    def test_expired_pending_task_cannot_continue(self) -> None:
        """超过有效期的待补参任务必须失效。"""
        coordinator = _coordinator(_ContextualGroup1(), pending_ttl_seconds=0)
        try:
            first = coordinator.orchestrate_text("移动文件")
            result = coordinator.continue_text(
                first["task_trace_id"], "移到下载目录"
            )

            self.assertEqual(first["status"], "needs_input")
            self.assertEqual(result["error"]["code"], "E5001")
        finally:
            coordinator.shutdown(wait=True)

    def test_cancelled_pending_task_cannot_continue(self) -> None:
        """用户取消后上下文应立即清除。"""
        coordinator = _coordinator(_ContextualGroup1())
        try:
            first = coordinator.orchestrate_text("移动文件")

            self.assertTrue(
                coordinator.cancel_pending_task(first["task_trace_id"])
            )
            self.assertFalse(
                coordinator.cancel_pending_task(first["task_trace_id"])
            )
            result = coordinator.continue_text(
                first["task_trace_id"], "移到下载目录"
            )
            self.assertEqual(result["error"]["code"], "E5001")
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
