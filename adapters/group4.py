"""第四组工具请求与结果的标准化适配。"""

from typing import Any, Dict

from group5.contracts.error_codes import ErrorCode
from group5.contracts.schemas import (
    CONTRACT_VERSION_V1,
    TaskEnvelopeV1,
    ToolRequestV1,
    ToolResultV1,
)


class Group4AdapterError(ValueError):
    """
    工具请求或工具结果违反公开契约。

    Args:
        code: 第五组工具错误码。
        detail: 可安全用于联调的错误说明。
    """

    def __init__(self, code: ErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def normalize_tool_request(
    raw_request: Dict[str, Any],
    envelope: TaskEnvelopeV1,
) -> ToolRequestV1:
    """
    将第三组工具请求转换为v1标准结构。

    Args:
        raw_request: 第三组返回的原始工具请求。
        envelope: 当前任务信封。

    Returns:
        字段完整的ToolRequestV1。

    Raises:
        Group4AdapterError: 工具ID、名称或参数不合法。
    """
    if not isinstance(raw_request, dict):
        raise Group4AdapterError(ErrorCode.E4003, "工具请求必须是对象")
    tool_call_id = raw_request.get("tool_call_id")
    tool_name = raw_request.get("tool_name")
    arguments = raw_request.get("arguments", raw_request.get("tool_params", {}))
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        raise Group4AdapterError(ErrorCode.E4003, "tool_call_id必须是非空字符串")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise Group4AdapterError(ErrorCode.E4003, "tool_name必须是非空字符串")
    if not isinstance(arguments, dict):
        raise Group4AdapterError(ErrorCode.E4003, "arguments必须是对象")
    return {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": envelope["task_trace_id"],
        "attempt_id": envelope["attempt_id"],
        "tool_call_id": tool_call_id.strip(),
        "tool_name": tool_name.strip(),
        "arguments": dict(arguments),
    }


def adapt_group4_call(module: Any, request: ToolRequestV1) -> ToolResultV1:
    """
    调用第四组并标准化工具结果。

    Args:
        module: 已注册的第四组适配器。
        request: 已校验的v1工具请求。

    Returns:
        标准工具结果。

    Raises:
        Group4AdapterError: 返回类型或success字段无效。
    """
    raw_result = module.call_tool(request["tool_name"], request["arguments"])
    if not isinstance(raw_result, dict):
        raise Group4AdapterError(ErrorCode.E4002, "工具结果必须是对象")
    success = raw_result.get("success")
    if not isinstance(success, bool):
        raise Group4AdapterError(ErrorCode.E4002, "工具结果缺少布尔success字段")
    if not success:
        message = str(raw_result.get("error", "第四组工具返回失败"))
        raise Group4AdapterError(ErrorCode.E4002, message)
    return {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": request["task_trace_id"],
        "tool_call_id": request["tool_call_id"],
        "success": True,
        "output": raw_result.get("result", raw_result.get("output")),
        "error": None,
    }


__all__ = [
    "Group4AdapterError",
    "adapt_group4_call",
    "normalize_tool_request",
]
