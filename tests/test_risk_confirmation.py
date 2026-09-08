"""高风险任务确认与四级风险处置的外部行为测试。"""

import unittest

from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase


def _task(action: str, target: str) -> dict:
    """
    构造指定动作和目标的v1文件任务。

    Args:
        action: 文件操作动作。
        target: 操作目标路径。

    Returns:
        可提交给核心入口的任务信封。
    """
    return {
        "contract_version": "1.0",
        "task_trace_id": f"risk-{action}-task",
        "attempt_id": f"risk-{action}-attempt",
        "timestamp": "2026-09-07T08:00:00+00:00",
        "intent": {
            "category": "文件操作",
            "action": action,
            "target": target,
            "params": {},
            "raw_text": f"{action} {target}",
        },
    }


class _RecordingGroup2:
    """记录收到意图并返回空计划的v1第二组。"""

    contract_version = "1.0"

    def __init__(self) -> None:
        """初始化公开请求记录。"""
        self.requests = []

    def plan(self, request: dict) -> dict:
        """记录规划请求并返回组合式空计划。"""
        self.requests.append(request)
        return {"steps": [], "elements": {}}


class _CompletedGroup3:
    """直接完成计划的v1第三组。"""

    contract_version = "1.0"

    def execute(self, request: dict) -> dict:
        """返回标准完成结果。"""
        return {
            "status": "completed",
            "completed_step_ids": [],
            "failed_step_id": None,
            "checkpoint": None,
            "output": "done",
        }


def _coordinator(confirmation_ttl_seconds: float = 120.0):
    """
    构造风险确认测试协调器。

    Args:
        confirmation_ttl_seconds: 确认请求有效秒数。

    Returns:
        协调器和记录型第二组。
    """
    group2 = _RecordingGroup2()
    coordinator = SystemCoordinator(
        rag_kb=RAGKnowledgeBase(),
        audit_logger=AuditLogger(),
        confirmation_ttl_seconds=confirmation_ttl_seconds,
    )
    coordinator.register("group2", group2)
    coordinator.register("group3", _CompletedGroup3())
    return coordinator, group2


class TestRiskConfirmation(unittest.TestCase):
    """验证high暂停、批准、拒绝、过期和critical拒绝。"""

    def test_high_risk_stops_before_planning(self) -> None:
        """delete任务必须先返回needs_confirmation。"""
        coordinator, group2 = _coordinator()
        try:
            result = coordinator.orchestrate_task(
                _task("delete", "/home/user/old.txt")
            )

            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["error"]["code"], "E1101")
            self.assertTrue(result["error"]["confirmation_id"])
            self.assertEqual(group2.requests, [])
        finally:
            coordinator.shutdown(wait=True)

    def test_approved_high_risk_runs_original_snapshot_once(self) -> None:
        """批准后才首次进入规划，且使用确认时保存的参数。"""
        coordinator, group2 = _coordinator()
        task = _task("delete", "/home/user/old.txt")
        try:
            paused = coordinator.orchestrate_task(task)
            task["intent"]["target"] = "/home/user/changed.txt"
            resumed = coordinator.confirm_task(
                paused["error"]["confirmation_id"], True
            )

            self.assertTrue(resumed["success"])
            self.assertEqual(len(group2.requests), 1)
            self.assertEqual(
                group2.requests[0]["intent"]["target"],
                "/home/user/old.txt",
            )
        finally:
            coordinator.shutdown(wait=True)

    def test_denied_high_risk_is_cancelled(self) -> None:
        """拒绝确认后任务应取消且不进入规划。"""
        coordinator, group2 = _coordinator()
        try:
            paused = coordinator.orchestrate_task(
                _task("delete", "/home/user/old.txt")
            )
            result = coordinator.confirm_task(
                paused["error"]["confirmation_id"], False
            )

            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["error"]["code"], "E1102")
            self.assertEqual(group2.requests, [])
        finally:
            coordinator.shutdown(wait=True)

    def test_confirmation_is_single_use(self) -> None:
        """同一确认ID再次提交必须返回E1102。"""
        coordinator, _ = _coordinator()
        try:
            paused = coordinator.orchestrate_task(
                _task("delete", "/home/user/old.txt")
            )
            confirmation_id = paused["error"]["confirmation_id"]
            first = coordinator.confirm_task(confirmation_id, True)
            second = coordinator.confirm_task(confirmation_id, True)

            self.assertTrue(first["success"])
            self.assertEqual(second["error"]["code"], "E1102")
        finally:
            coordinator.shutdown(wait=True)

    def test_expired_confirmation_cannot_run(self) -> None:
        """过期确认不得进入规划或执行。"""
        coordinator, group2 = _coordinator(confirmation_ttl_seconds=0)
        try:
            paused = coordinator.orchestrate_task(
                _task("delete", "/home/user/old.txt")
            )
            result = coordinator.confirm_task(
                paused["error"]["confirmation_id"], True
            )

            self.assertEqual(result["error"]["code"], "E1102")
            self.assertEqual(group2.requests, [])
        finally:
            coordinator.shutdown(wait=True)

    def test_critical_path_cannot_be_confirmed(self) -> None:
        """系统受保护路径必须直接拒绝，不生成确认ID。"""
        coordinator, group2 = _coordinator()
        try:
            result = coordinator.orchestrate_task(
                _task("navigate", "/etc/passwd")
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "E1001")
            self.assertNotIn("confirmation_id", result["error"])
            self.assertEqual(group2.requests, [])
        finally:
            coordinator.shutdown(wait=True)

    def test_medium_risk_runs_without_confirmation(self) -> None:
        """安全目录内create操作应直接进入规划。"""
        coordinator, group2 = _coordinator()
        try:
            result = coordinator.orchestrate_task(
                _task("create", "/home/user/new-folder")
            )

            self.assertTrue(result["success"])
            self.assertEqual(len(group2.requests), 1)
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
