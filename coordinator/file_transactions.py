"""批量文件操作的预检查、提交与逆序回滚协议。"""

from typing import Any, Callable, Dict, List, TypedDict


class FileOperation(TypedDict):
    """一个可恢复文件操作及其撤销动作。"""

    operation_id: str
    tool_name: str
    arguments: Dict[str, Any]
    undo_tool_name: str
    undo_arguments: Dict[str, Any]


class FileTransactionPlan(TypedDict):
    """第五组提交给第四组执行前检查的批量操作计划。"""

    transaction_id: str
    operations: List[FileOperation]


class FileTransactionCoordinator:
    """
    使用第四组公开工具调用接口协调可恢复批量文件事务。

    本类不直接操作文件系统。所有正向和撤销动作都由调用方注入的tool_call
    执行，从而保持第四组业务实现边界。
    """

    def __init__(
        self,
        tool_call: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    ) -> None:
        """
        初始化文件事务协调器。

        Args:
            tool_call: 第四组标准工具调用函数。
        """
        self._tool_call = tool_call

    def execute(self, plan: FileTransactionPlan) -> Dict[str, Any]:
        """
        预检查并原子执行批量文件操作。

        Args:
            plan: 带唯一操作ID和撤销动作的事务计划。

        Returns:
            completed、rolled_back或partial_failure结果及操作清单。
        """
        validation_error = self._validate_plan(plan)
        if validation_error:
            return {
                "success": False,
                "status": "failed",
                "transaction_id": plan.get("transaction_id", ""),
                "error": validation_error,
                "completed_operation_ids": [],
                "rolled_back_operation_ids": [],
                "remaining_side_effects": [],
            }

        preflight = self._tool_call(
            "preflight_file_transaction",
            {
                "transaction_id": plan["transaction_id"],
                "operations": plan["operations"],
            },
        )
        if not isinstance(preflight, dict) or preflight.get("success") is not True:
            return {
                "success": False,
                "status": "failed",
                "transaction_id": plan["transaction_id"],
                "error": "文件事务预检查失败",
                "completed_operation_ids": [],
                "rolled_back_operation_ids": [],
                "remaining_side_effects": [],
            }

        completed: List[FileOperation] = []
        for operation in plan["operations"]:
            result = self._tool_call(operation["tool_name"], operation["arguments"])
            if isinstance(result, dict) and result.get("success") is True:
                completed.append(operation)
                continue
            return self._rollback(plan["transaction_id"], completed, operation)

        return {
            "success": True,
            "status": "completed",
            "transaction_id": plan["transaction_id"],
            "error": None,
            "completed_operation_ids": [item["operation_id"] for item in completed],
            "rolled_back_operation_ids": [],
            "remaining_side_effects": [],
        }

    def _rollback(
        self,
        transaction_id: str,
        completed: List[FileOperation],
        failed_operation: FileOperation,
    ) -> Dict[str, Any]:
        """
        逆序撤销已经完成的操作。

        Args:
            transaction_id: 当前文件事务ID。
            completed: 失败前已经成功的操作。
            failed_operation: 首个执行失败的操作。

        Returns:
            rolled_back或partial_failure结果。
        """
        rolled_back: List[str] = []
        remaining: List[str] = []
        for operation in reversed(completed):
            undo_result = self._tool_call(
                operation["undo_tool_name"],
                operation["undo_arguments"],
            )
            if isinstance(undo_result, dict) and undo_result.get("success") is True:
                rolled_back.append(operation["operation_id"])
            else:
                remaining.append(operation["operation_id"])

        status = "rolled_back" if not remaining else "partial_failure"
        return {
            "success": False,
            "status": status,
            "transaction_id": transaction_id,
            "error": f"操作失败: {failed_operation['operation_id']}",
            "completed_operation_ids": [item["operation_id"] for item in completed],
            "rolled_back_operation_ids": rolled_back,
            "remaining_side_effects": remaining,
        }

    @staticmethod
    def _validate_plan(plan: Any) -> str:
        """
        校验事务计划并拒绝不可恢复操作。

        Args:
            plan: 待执行的任意对象。

        Returns:
            空字符串表示合法，否则返回错误说明。
        """
        if not isinstance(plan, dict):
            return "文件事务计划必须是对象"
        if not isinstance(plan.get("transaction_id"), str) or not plan["transaction_id"]:
            return "transaction_id必须是非空字符串"
        operations = plan.get("operations")
        if not isinstance(operations, list) or not operations:
            return "operations必须是非空数组"

        seen_ids = set()
        forbidden_tools = {"rm", "shred", "empty_trash", "permanent_delete"}
        for operation in operations:
            if not isinstance(operation, dict):
                return "每个文件操作必须是对象"
            required = {
                "operation_id",
                "tool_name",
                "arguments",
                "undo_tool_name",
                "undo_arguments",
            }
            if not required.issubset(operation):
                return "文件操作缺少正向或撤销字段"
            operation_id = operation["operation_id"]
            if not isinstance(operation_id, str) or not operation_id:
                return "operation_id必须是非空字符串"
            if operation_id in seen_ids:
                return "operation_id不得重复"
            seen_ids.add(operation_id)
            if str(operation["tool_name"]).lower() in forbidden_tools:
                return "首版禁止永久删除或清空回收站"
            if not isinstance(operation["arguments"], dict) or not isinstance(
                operation["undo_arguments"], dict
            ):
                return "正向和撤销参数必须是对象"
            if not isinstance(operation["undo_tool_name"], str) or not operation[
                "undo_tool_name"
            ]:
                return "每个操作必须提供撤销工具"
        return ""


__all__ = [
    "FileOperation",
    "FileTransactionCoordinator",
    "FileTransactionPlan",
]
