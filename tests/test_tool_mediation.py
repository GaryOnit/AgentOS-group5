"""第五组工具请求中介与第三组恢复的外部行为测试。"""

import unittest

from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase


def _task() -> dict:
    """返回需要工具参与的v1文件任务。"""
    return {
        "contract_version": "1.0",
        "task_trace_id": "tool-task-001",
        "attempt_id": "tool-attempt-001",
        "timestamp": "2026-09-07T08:00:00+00:00",
        "intent": {
            "category": "文件操作",
            "action": "organize",
            "target": "/home/user/Downloads",
            "params": {"source": "/home/user/Downloads"},
            "raw_text": "整理下载目录",
        },
    }


class _Group2:
    """提供最小计划和组合检测结果的v1第二组。"""

    contract_version = "1.0"

    def plan(self, request: dict) -> dict:
        """返回一个需要工具的步骤。"""
        return {
            "steps": [{"step_id": "step-tool", "action": "organize"}],
            "elements": {},
        }


class _ToolRequestingGroup3:
    """暂停请求工具，收到工具结果后完成的v1第三组。"""

    contract_version = "1.0"

    def __init__(self) -> None:
        """初始化公开请求记录。"""
        self.requests = []

    def execute(self, request: dict) -> dict:
        """根据检查点和工具结果返回暂停或完成。"""
        self.requests.append(request)
        if request["checkpoint"] is None:
            return {
                "status": "tool_required",
                "completed_step_ids": [],
                "failed_step_id": "step-tool",
                "checkpoint": "resume-after-tool",
                "tool_request": {
                    "tool_call_id": "tool-call-001",
                    "tool_name": "organize_downloads",
                    "arguments": {"path": "/home/user/Downloads"},
                },
            }
        return {
            "status": "completed",
            "completed_step_ids": ["step-tool"],
            "failed_step_id": None,
            "checkpoint": None,
            "output": request["tool_result"]["output"],
        }


class _SuccessfulGroup4:
    """返回成功并记录调用次数的第四组适配器。"""

    def __init__(self) -> None:
        """初始化公开调用记录。"""
        self.calls = []

    def call_tool(self, tool_name: str, params: dict) -> dict:
        """记录参数并返回成功结果。"""
        self.calls.append((tool_name, params))
        return {"success": True, "result": "organized"}


class _FailingGroup4:
    """以标准失败结果拒绝工具调用的第四组。"""

    def call_tool(self, tool_name: str, params: dict) -> dict:
        """返回业务失败。"""
        return {"success": False, "error": "tool failed"}


def _coordinator(group3, group4=None) -> SystemCoordinator:
    """
    构造工具中介测试协调器。

    Args:
        group3: 请求工具的第三组适配器。
        group4: 可选第四组工具适配器。

    Returns:
        使用纯内存持久组件的协调器。
    """
    coordinator = SystemCoordinator(
        rag_kb=RAGKnowledgeBase(),
        audit_logger=AuditLogger(),
    )
    coordinator.register("group2", _Group2())
    coordinator.register("group3", group3)
    if group4 is not None:
        coordinator.register("group4", group4)
    return coordinator


class TestToolMediation(unittest.TestCase):
    """验证工具成功、失败、缺失和重复调用路径。"""

    def test_tool_success_resumes_group3(self) -> None:
        """工具成功后应携带同一检查点恢复第三组并完成。"""
        group3 = _ToolRequestingGroup3()
        group4 = _SuccessfulGroup4()
        coordinator = _coordinator(group3, group4)
        try:
            result = coordinator.orchestrate_task(_task())
            stage_names = [stage["name"] for stage in result["stages"]]

            self.assertTrue(result["success"])
            self.assertEqual(len(group4.calls), 1)
            self.assertEqual(len(group3.requests), 2)
            self.assertEqual(group3.requests[1]["checkpoint"], "resume-after-tool")
            self.assertEqual(group3.requests[1]["tool_result"]["output"], "organized")
            self.assertIn("tool_authorization", stage_names)
            self.assertIn("tool_execution", stage_names)
        finally:
            coordinator.shutdown(wait=True)

    def test_missing_group4_stops_before_tool_execution(self) -> None:
        """第三组请求工具但第四组未注册时应返回E4001。"""
        coordinator = _coordinator(_ToolRequestingGroup3())
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "tool_authorization")
            self.assertEqual(result["error"]["code"], "E4001")
        finally:
            coordinator.shutdown(wait=True)

    def test_group4_failure_is_not_reported_as_success(self) -> None:
        """第四组返回success=False时整体必须失败。"""
        coordinator = _coordinator(_ToolRequestingGroup3(), _FailingGroup4())
        try:
            result = coordinator.orchestrate_task(_task())

            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "tool_execution")
            self.assertEqual(result["error"]["code"], "E4002")
        finally:
            coordinator.shutdown(wait=True)

    def test_same_tool_call_id_cannot_execute_twice(self) -> None:
        """同一协调器内重复提交工具调用ID应返回E4004。"""
        group4 = _SuccessfulGroup4()
        coordinator = _coordinator(_ToolRequestingGroup3(), group4)
        try:
            first = coordinator.orchestrate_task(_task())
            second = coordinator.orchestrate_task(_task())

            self.assertTrue(first["success"])
            self.assertFalse(second["success"])
            self.assertEqual(second["error"]["code"], "E4004")
            self.assertEqual(len(group4.calls), 1)
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
