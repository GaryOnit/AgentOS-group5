"""第三组可恢复整计划执行接口适配。"""

from typing import Any, Dict, List, Optional

from group5.adapters.group2 import build_legacy_execution_plan
from group5.contracts.error_codes import ErrorCode
from group5.contracts.schemas import (
    CONTRACT_VERSION_V1,
    DetectionResultV1,
    ExecutionRequestV1,
    ExecutionResultV1,
    PlanResultV1,
    TaskEnvelopeV1,
)


class Group3AdapterError(ValueError):
    """
    第三组执行结果违反公开契约。

    Args:
        code: 第五组执行错误码。
        detail: 可安全写入联调日志的错误说明。
    """

    def __init__(self, code: ErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def adapt_group3_execute(
    module: Any,
    envelope: TaskEnvelopeV1,
    plan: PlanResultV1,
    detection: DetectionResultV1,
    checkpoint: Optional[str] = None,
    tool_result: Optional[Dict[str, Any]] = None,
) -> ExecutionResultV1:
    """
    调用第三组并标准化完成、暂停或失败结果。

    Args:
        module: 已注册的第三组真实或Mock适配器。
        envelope: 当前v1任务信封。
        plan: 第二组标准计划。
        detection: 当前控件检测结果。
        checkpoint: 恢复执行时由第三组先前返回的不透明检查点。
        tool_result: 工具执行完成后恢复第三组时提供的结果。

    Returns:
        通过基础语义校验的标准执行结果。

    Raises:
        Group3AdapterError: 返回类型、状态或检查点不合法。
    """
    request: ExecutionRequestV1 = {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": envelope["task_trace_id"],
        "attempt_id": envelope["attempt_id"],
        "plan": plan,
        "detection": detection,
        "checkpoint": checkpoint,
        "tool_result": tool_result,
    }
    contract_version = str(getattr(module, "contract_version", "legacy"))
    if contract_version == CONTRACT_VERSION_V1:
        raw_result = module.execute(request)
    else:
        legacy_plan = build_legacy_execution_plan(plan, detection)
        if checkpoint is not None:
            legacy_plan["checkpoint"] = checkpoint
        if tool_result is not None:
            legacy_plan["tool_result"] = tool_result
        raw_result = module.execute(legacy_plan)

    if not isinstance(raw_result, dict):
        raise Group3AdapterError(ErrorCode.E3002, "执行结果必须是对象")

    raw_status = str(raw_result.get("status", "success")).lower()
    status_map = {
        "success": "completed",
        "completed": "completed",
        "needs_redetection": "needs_redetection",
        "tool_required": "tool_required",
        "failed": "failed",
        "error": "failed",
    }
    status = status_map.get(raw_status)
    if status is None:
        raise Group3AdapterError(ErrorCode.E3002, f"未知执行状态: {raw_status}")

    completed_step_ids = _completed_step_ids(raw_result, plan, status)
    failed_step_id = raw_result.get("failed_step_id")
    if failed_step_id is not None and not isinstance(failed_step_id, str):
        raise Group3AdapterError(ErrorCode.E3002, "failed_step_id 必须是字符串")

    raw_checkpoint = raw_result.get("checkpoint")
    if raw_checkpoint is not None and not isinstance(raw_checkpoint, str):
        raise Group3AdapterError(ErrorCode.E3004, "checkpoint 必须是字符串")
    if status in {"needs_redetection", "tool_required"} and not raw_checkpoint:
        raise Group3AdapterError(ErrorCode.E3004, "暂停执行必须返回非空checkpoint")
    if status == "needs_redetection" and not failed_step_id:
        raise Group3AdapterError(
            ErrorCode.E3002,
            "needs_redetection 必须返回failed_step_id",
        )

    tool_request = raw_result.get("tool_request")
    if tool_request is not None and not isinstance(tool_request, dict):
        raise Group3AdapterError(ErrorCode.E3002, "tool_request 必须是对象")
    if status == "tool_required" and tool_request is None:
        raise Group3AdapterError(
            ErrorCode.E3002,
            "tool_required 必须返回tool_request",
        )

    return {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": envelope["task_trace_id"],
        "status": status,
        "completed_step_ids": completed_step_ids,
        "failed_step_id": failed_step_id,
        "checkpoint": raw_checkpoint,
        "output": raw_result.get("output"),
        "tool_request": tool_request,
        "metadata": {
            "source_contract": contract_version,
            "legacy_payload": dict(raw_result),
        },
    }


def _completed_step_ids(
    raw_result: Dict[str, Any],
    plan: PlanResultV1,
    status: str,
) -> List[str]:
    """
    标准化已完成步骤标识。

    Args:
        raw_result: 第三组原始执行结果。
        plan: 当前标准计划。
        status: 已标准化的执行状态。

    Returns:
        字符串形式的已完成步骤ID列表。
    """
    declared = raw_result.get("completed_step_ids")
    if declared is not None:
        if not isinstance(declared, list) or any(
            not isinstance(item, (str, int)) for item in declared
        ):
            raise Group3AdapterError(
                ErrorCode.E3002,
                "completed_step_ids 必须是字符串或整数数组",
            )
        return [str(item) for item in declared]

    if status != "completed":
        return []

    result: List[str] = []
    for index, step in enumerate(plan["steps"], start=1):
        step_id = step.get("step_id", step.get("step", index))
        result.append(str(step_id))
    return result


__all__ = ["Group3AdapterError", "adapt_group3_execute"]
