"""批量文件事务协议的外部行为测试。"""

import unittest

from group5.coordinator.file_transactions import FileTransactionCoordinator


def _plan() -> dict:
    """返回包含三个可恢复移动操作的测试计划。"""
    return {
        "transaction_id": "transaction-001",
        "operations": [
            {
                "operation_id": f"op-{index}",
                "tool_name": "move_file",
                "arguments": {"source": f"/tmp/{index}", "destination": f"/tmp/out/{index}"},
                "undo_tool_name": "move_file",
                "undo_arguments": {"source": f"/tmp/out/{index}", "destination": f"/tmp/{index}"},
            }
            for index in range(1, 4)
        ],
    }


class _ToolDouble:
    """按工具名和路径配置失败并记录调用顺序的第四组替身。"""

    def __init__(self, fail_sources=None) -> None:
        """
        初始化测试替身。

        Args:
            fail_sources: 需要返回失败的source路径集合。
        """
        self.fail_sources = set(fail_sources or [])
        self.calls = []

    def __call__(self, tool_name: str, arguments: dict) -> dict:
        """记录公开工具调用并按source返回结果。"""
        self.calls.append((tool_name, arguments))
        if tool_name == "preflight_file_transaction":
            return {"success": True}
        return {"success": arguments.get("source") not in self.fail_sources}


class TestFileTransactions(unittest.TestCase):
    """验证预检查、全部成功、逆序回滚和部分失败。"""

    def test_all_operations_complete_after_preflight(self) -> None:
        """预检查和三个操作成功时事务应提交。"""
        tools = _ToolDouble()
        result = FileTransactionCoordinator(tools).execute(_plan())

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_operation_ids"], ["op-1", "op-2", "op-3"])
        self.assertEqual(len(tools.calls), 4)

    def test_failure_rolls_back_completed_operations_in_reverse(self) -> None:
        """第三项失败时前两项必须按op-2、op-1顺序撤销。"""
        tools = _ToolDouble(fail_sources={"/tmp/3"})
        result = FileTransactionCoordinator(tools).execute(_plan())
        rollback_sources = [
            arguments["source"]
            for tool_name, arguments in tools.calls
            if tool_name == "move_file" and str(arguments["source"]).startswith("/tmp/out")
        ]

        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["rolled_back_operation_ids"], ["op-2", "op-1"])
        self.assertEqual(rollback_sources, ["/tmp/out/2", "/tmp/out/1"])

    def test_undo_failure_returns_partial_failure_manifest(self) -> None:
        """撤销失败必须报告仍存在副作用的操作ID。"""
        tools = _ToolDouble(fail_sources={"/tmp/3", "/tmp/out/1"})
        result = FileTransactionCoordinator(tools).execute(_plan())

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["remaining_side_effects"], ["op-1"])
        self.assertEqual(result["rolled_back_operation_ids"], ["op-2"])

    def test_preflight_failure_has_no_operation_side_effects(self) -> None:
        """预检查失败时不得调用任何正向文件工具。"""
        class _PreflightFailure(_ToolDouble):
            """始终拒绝预检查的第四组替身。"""

            def __call__(self, tool_name: str, arguments: dict) -> dict:
                """仅记录预检查并返回失败。"""
                self.calls.append((tool_name, arguments))
                return {"success": False}

        tools = _PreflightFailure()
        result = FileTransactionCoordinator(tools).execute(_plan())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(tools.calls), 1)
        self.assertEqual(tools.calls[0][0], "preflight_file_transaction")

    def test_permanent_delete_is_rejected_before_preflight(self) -> None:
        """rm、shred等不可恢复工具不得进入第四组。"""
        tools = _ToolDouble()
        plan = _plan()
        plan["operations"][0]["tool_name"] = "shred"

        result = FileTransactionCoordinator(tools).execute(plan)
        self.assertEqual(result["status"], "failed")
        self.assertIn("永久删除", result["error"])
        self.assertEqual(tools.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
