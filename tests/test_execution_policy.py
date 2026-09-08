"""阶段超时与幂等重试策略的外部行为测试。"""

import time
import unittest

from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import MockGroup2Planner, MockGroup3Executor


def _task() -> dict:
    """
    构造合法v1结构化任务。

    Returns:
        可提交给核心入口的任务信封。
    """
    return {
        "contract_version": "1.0",
        "task_trace_id": "policy-task-001",
        "attempt_id": "policy-attempt-001",
        "timestamp": "2026-09-07T08:00:00+00:00",
        "intent": {
            "category": "应用控制",
            "action": "open",
            "target": "Files",
            "params": {},
            "raw_text": "打开文件管理器",
        },
    }


class _TransientPlanner:
    """首次失败、第二次成功的幂等规划器。"""

    def __init__(self) -> None:
        """初始化公开调用计数。"""
        self.calls = 0

    def plan(self, intent: dict) -> dict:
        """
        首次抛出瞬时异常，第二次返回计划。

        Args:
            intent: 旧核心当前传入的意图对象。

        Returns:
            第二次调用时返回最小有效计划。
        """
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary planner error")
        return {"action": "open", "target": "Files", "steps": []}


class _FailingExecutor:
    """每次执行都失败并暴露调用次数的执行器。"""

    def __init__(self) -> None:
        """初始化公开调用计数。"""
        self.calls = 0

    def execute(self, plan: dict) -> dict:
        """
        模拟副作用状态未知的执行异常。

        Args:
            plan: 规划器返回的计划。

        Raises:
            RuntimeError: 每次调用均抛出，验证协调器不会盲目重试。
        """
        self.calls += 1
        raise RuntimeError("execution state unknown")


class _SlowGroup1:
    """超过测试意图超时阈值的第一组适配器。"""

    def understand_intent(self, user_input: str, history=None) -> dict:
        """等待后返回合法意图，用于验证第五组外层超时。"""
        time.sleep(0.1)
        return {
            "intent": "应用控制",
            "target": "Files",
            "action": "open",
            "params": {},
        }


class TestExecutionPolicy(unittest.TestCase):
    """验证不同阶段对超时和重试的可观察行为。"""

    def test_idempotent_planner_retries_once(self) -> None:
        """瞬时规划异常应重试一次并记录两次尝试。"""
        planner = _TransientPlanner()
        coordinator = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=AuditLogger(),
        )
        coordinator.register("group2", planner)
        coordinator.register("group3", MockGroup3Executor())
        try:
            result = coordinator.orchestrate_task(_task())
            planning = next(
                stage for stage in result["stages"] if stage["name"] == "planning"
            )

            self.assertTrue(result["success"])
            self.assertEqual(planner.calls, 2)
            self.assertEqual(planning["attempts"], 2)
        finally:
            coordinator.shutdown(wait=True)

    def test_executor_exception_is_not_retried(self) -> None:
        """副作用状态未知的执行异常只能调用一次。"""
        executor = _FailingExecutor()
        coordinator = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=AuditLogger(),
        )
        coordinator.register("group2", MockGroup2Planner())
        coordinator.register("group3", executor)
        try:
            result = coordinator.orchestrate_task(_task())
            execution = next(
                stage for stage in result["stages"] if stage["name"] == "execution"
            )

            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], "E3001")
            self.assertEqual(executor.calls, 1)
            self.assertEqual(execution["attempts"], 1)
        finally:
            coordinator.shutdown(wait=True)

    def test_intent_stage_uses_its_own_timeout(self) -> None:
        """第一组超时应映射为可重试E0103。"""
        coordinator = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=AuditLogger(),
            stage_timeouts={"intent": 0.01},
        )
        coordinator.register("group1", _SlowGroup1())
        try:
            result = coordinator.orchestrate_text("打开文件管理器")

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "intent")
            self.assertEqual(result["error"]["code"], "E0103")
            self.assertEqual(result["error"]["cause"]["reason"], "timeout")
        finally:
            coordinator.shutdown(wait=True)

    def test_unknown_timeout_stage_is_rejected(self) -> None:
        """拼写错误的超时配置不得被静默忽略。"""
        with self.assertRaisesRegex(ValueError, "未知超时阶段"):
            SystemCoordinator(stage_timeouts={"plannning": 1.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
