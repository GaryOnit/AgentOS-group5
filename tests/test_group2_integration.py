"""第二组规划与控件检测适配的外部行为测试。"""

import time
import unittest

from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import MockGroup2Planner


def _task() -> dict:
    """返回可提交给v1核心入口的应用控制任务。"""
    return {
        "contract_version": "1.0",
        "task_trace_id": "group2-task-001",
        "attempt_id": "group2-attempt-001",
        "timestamp": "2026-09-07T08:00:00+00:00",
        "intent": {
            "category": "应用控制",
            "action": "open",
            "target": "Files",
            "params": {},
            "raw_text": "打开文件管理器",
        },
    }


class _RecordingExecutor:
    """记录第三组收到计划的测试执行器。"""

    def __init__(self) -> None:
        """初始化最后一次计划。"""
        self.last_plan = None

    def execute(self, plan: dict) -> dict:
        """
        记录计划并返回成功。

        Args:
            plan: 第二组适配器构造的兼容执行计划。

        Returns:
            最小成功执行结果。
        """
        self.last_plan = plan
        return {"output": "done", "status": "success"}


class _CombinedGroup2:
    """一次返回steps和elements的v1第二组适配器。"""

    contract_version = "1.0"

    def plan(self, request: dict) -> dict:
        """返回组合式规划检测结果。"""
        return {
            "plan_id": "combined-plan",
            "steps": [{"step_id": "step-1", "action": "click"}],
            "elements": {"Files": {"bbox": [1, 2, 3, 4]}},
        }


class _SeparateGroup2:
    """分别提供plan和detect的v1第二组适配器。"""

    contract_version = "1.0"

    def plan(self, request: dict) -> dict:
        """返回不含控件的计划。"""
        return {"plan_id": "separate-plan", "steps": [{"step_id": "step-1"}]}

    def detect(self, request: dict) -> dict:
        """返回标准elements包装。"""
        return {"elements": [{"name": "Files", "bbox": [1, 2, 3, 4]}]}


class _InvalidPlanGroup2:
    """返回非法steps的第二组适配器。"""

    def plan(self, request: dict) -> dict:
        """返回非数组steps。"""
        return {"steps": "invalid"}


class _InvalidDetectionGroup2:
    """返回非法elements的第二组适配器。"""

    def plan(self, request: dict) -> dict:
        """返回基本计划。"""
        return {"steps": []}

    def detect_elements(self):
        """返回不受支持的标量检测结果。"""
        return 123


class _SlowDetectionGroup2(_InvalidDetectionGroup2):
    """检测阶段超过测试超时的第二组适配器。"""

    def detect_elements(self):
        """等待后返回空控件集合。"""
        time.sleep(0.1)
        return {}


def _coordinator(group2, stage_timeouts=None):
    """
    构造仅用于第二组适配测试的协调器。

    Args:
        group2: 待测试的第二组实现。
        stage_timeouts: 可选阶段超时覆盖。

    Returns:
        协调器和记录型第三组执行器。
    """
    executor = _RecordingExecutor()
    coordinator = SystemCoordinator(
        rag_kb=RAGKnowledgeBase(),
        audit_logger=AuditLogger(),
        stage_timeouts=stage_timeouts,
    )
    coordinator.register("group2", group2)
    coordinator.register("group3", executor)
    return coordinator, executor


class TestGroup2Integration(unittest.TestCase):
    """验证组合、分离、降级和失败检测路径。"""

    def test_combined_output_becomes_detection_stage(self) -> None:
        """组合式elements应进入独立completed检测阶段。"""
        coordinator, executor = _coordinator(_CombinedGroup2())
        try:
            result = coordinator.orchestrate_task(_task())
            detection = next(
                stage for stage in result["stages"] if stage["name"] == "detection"
            )

            self.assertTrue(result["success"])
            self.assertEqual(detection["status"], "completed")
            self.assertIn("Files", executor.last_plan["elements"])
        finally:
            coordinator.shutdown(wait=True)

    def test_separate_detector_receives_own_stage(self) -> None:
        """独立detect接口应产生控件数组并进入执行计划。"""
        coordinator, executor = _coordinator(_SeparateGroup2())
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertTrue(result["success"])
            self.assertEqual(executor.last_plan["elements"][0]["name"], "Files")
        finally:
            coordinator.shutdown(wait=True)

    def test_legacy_planner_without_detector_is_explicitly_degraded(self) -> None:
        """旧Mock缺少检测能力时应标记degraded而非伪装完成。"""
        coordinator, executor = _coordinator(MockGroup2Planner())
        try:
            result = coordinator.orchestrate_task(_task())
            detection = next(
                stage for stage in result["stages"] if stage["name"] == "detection"
            )

            self.assertTrue(result["success"])
            self.assertEqual(detection["status"], "degraded")
            self.assertEqual(executor.last_plan["detection_status"], "degraded")
        finally:
            coordinator.shutdown(wait=True)

    def test_invalid_plan_stops_in_planning(self) -> None:
        """非法steps应返回E2002且不得调用第三组。"""
        coordinator, executor = _coordinator(_InvalidPlanGroup2())
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "planning")
            self.assertEqual(result["error"]["code"], "E2002")
            self.assertIsNone(executor.last_plan)
        finally:
            coordinator.shutdown(wait=True)

    def test_invalid_detection_stops_before_execution(self) -> None:
        """非法elements应返回E2004且不得调用第三组。"""
        coordinator, executor = _coordinator(_InvalidDetectionGroup2())
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "detection")
            self.assertEqual(result["error"]["code"], "E2004")
            self.assertIsNone(executor.last_plan)
        finally:
            coordinator.shutdown(wait=True)

    def test_detection_uses_independent_timeout(self) -> None:
        """检测超时应停止在detection阶段并返回E5002。"""
        coordinator, executor = _coordinator(
            _SlowDetectionGroup2(), {"detection": 0.01}
        )
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "detection")
            self.assertEqual(result["error"]["code"], "E5002")
            self.assertIsNone(executor.last_plan)
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
