"""第三组检查点执行和控件重新检测恢复的外部行为测试。"""

import unittest

from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase


def _task() -> dict:
    """返回包含两个步骤的v1测试任务。"""
    return {
        "contract_version": "1.0",
        "task_trace_id": "group3-task-001",
        "attempt_id": "group3-attempt-001",
        "timestamp": "2026-09-07T08:00:00+00:00",
        "intent": {
            "category": "应用控制",
            "action": "open",
            "target": "Files",
            "params": {},
            "raw_text": "打开文件管理器",
        },
    }


class _DetectingGroup2:
    """支持重复控件检测的v1第二组适配器。"""

    contract_version = "1.0"

    def __init__(self) -> None:
        """初始化检测调用次数。"""
        self.detect_calls = 0

    def plan(self, request: dict) -> dict:
        """返回两个顺序步骤。"""
        return {
            "plan_id": "recoverable-plan",
            "steps": [
                {"step_id": "step-1", "action": "open"},
                {"step_id": "step-2", "action": "click"},
            ],
        }

    def detect(self, request: dict) -> dict:
        """每次返回带版本标记的新控件坐标。"""
        self.detect_calls += 1
        return {
            "elements": {
                "Files": {"bbox": [self.detect_calls, 2, 3, 4]}
            }
        }


class _RecoverableGroup3:
    """首次要求重新检测，收到检查点后完成的v1第三组。"""

    contract_version = "1.0"

    def __init__(self) -> None:
        """初始化公开请求记录。"""
        self.requests = []

    def execute(self, request: dict) -> dict:
        """根据checkpoint返回暂停或完成结果。"""
        self.requests.append(request)
        if request["checkpoint"] is None:
            return {
                "status": "needs_redetection",
                "completed_step_ids": ["step-1"],
                "failed_step_id": "step-2",
                "checkpoint": "resume-step-2",
            }
        return {
            "status": "completed",
            "completed_step_ids": ["step-1", "step-2"],
            "failed_step_id": None,
            "checkpoint": None,
            "output": "done",
        }


class _NeverRecoveringGroup3(_RecoverableGroup3):
    """每次都要求重新检测的第三组适配器。"""

    def execute(self, request: dict) -> dict:
        """持续返回同一恢复请求。"""
        self.requests.append(request)
        return {
            "status": "needs_redetection",
            "completed_step_ids": ["step-1"],
            "failed_step_id": "step-2",
            "checkpoint": "resume-step-2",
        }


class _MissingCheckpointGroup3:
    """暂停时遗漏检查点的非法第三组适配器。"""

    contract_version = "1.0"

    def execute(self, request: dict) -> dict:
        """返回缺少checkpoint的重新检测状态。"""
        return {
            "status": "needs_redetection",
            "completed_step_ids": ["step-1"],
            "failed_step_id": "step-2",
        }


class _MalformedGroup3:
    """返回非对象结果的非法第三组适配器。"""

    contract_version = "1.0"

    def execute(self, request: dict):
        """返回无法解析的标量值。"""
        return "invalid"


def _coordinator(group2, group3) -> SystemCoordinator:
    """
    构造第三组恢复测试协调器。

    Args:
        group2: 支持检测的第二组适配器。
        group3: 待测试的第三组适配器。

    Returns:
        使用纯内存依赖的协调器。
    """
    coordinator = SystemCoordinator(
        rag_kb=RAGKnowledgeBase(),
        audit_logger=AuditLogger(),
    )
    coordinator.register("group2", group2)
    coordinator.register("group3", group3)
    return coordinator


class TestGroup3Integration(unittest.TestCase):
    """验证第三组完成、暂停和恢复失败的结果语义。"""

    def test_redetection_resumes_once_from_checkpoint(self) -> None:
        """首次控件失效应重新检测并从检查点完成。"""
        group2 = _DetectingGroup2()
        group3 = _RecoverableGroup3()
        coordinator = _coordinator(group2, group3)
        try:
            result = coordinator.orchestrate_task(_task())
            stage_names = [stage["name"] for stage in result["stages"]]

            self.assertTrue(result["success"])
            self.assertEqual(group2.detect_calls, 2)
            self.assertEqual(len(group3.requests), 2)
            self.assertEqual(group3.requests[1]["checkpoint"], "resume-step-2")
            self.assertEqual(
                stage_names,
                [
                    "security",
                    "planning",
                    "detection",
                    "execution",
                    "detection",
                    "execution",
                    "persistence",
                ],
            )
        finally:
            coordinator.shutdown(wait=True)

    def test_second_redetection_request_returns_e3003(self) -> None:
        """重新检测后仍无法执行时必须停止，不得进入循环。"""
        group2 = _DetectingGroup2()
        group3 = _NeverRecoveringGroup3()
        coordinator = _coordinator(group2, group3)
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertFalse(result["success"])
            self.assertEqual(result["error"]["code"], "E3003")
            self.assertEqual(len(group3.requests), 2)
            self.assertEqual(group2.detect_calls, 2)
        finally:
            coordinator.shutdown(wait=True)

    def test_missing_checkpoint_returns_e3004(self) -> None:
        """暂停执行缺少检查点时应在执行阶段拒绝。"""
        coordinator = _coordinator(_DetectingGroup2(), _MissingCheckpointGroup3())
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "execution")
            self.assertEqual(result["error"]["code"], "E3004")
        finally:
            coordinator.shutdown(wait=True)

    def test_non_object_execution_result_returns_e3002(self) -> None:
        """非对象执行结果不得进入工具或持久化阶段。"""
        coordinator = _coordinator(_DetectingGroup2(), _MalformedGroup3())
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "execution")
            self.assertEqual(result["error"]["code"], "E3002")
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
