"""第二组规划与控件检测接口的兼容适配。"""

from typing import Any, Dict, List, Optional, Tuple

from group5.contracts.error_codes import ErrorCode
from group5.contracts.schemas import (
    CONTRACT_VERSION_V1,
    DetectionRequestV1,
    DetectionResultV1,
    PlanResultV1,
    PlanningRequestV1,
    TaskEnvelopeV1,
)


class Group2AdapterError(ValueError):
    """
    第二组规划或检测结果不符合契约。

    Args:
        code: 对应规划或检测阶段的第五组错误码。
        detail: 不包含敏感桌面内容的错误说明。
    """

    def __init__(self, code: ErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def adapt_group2_plan(
    module: Any,
    envelope: TaskEnvelopeV1,
    legacy_intent: Dict[str, Any],
    retrieval_context: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[PlanResultV1, Optional[Any]]:
    """
    调用第二组规划能力并标准化结果。

    Args:
        module: 已注册的第二组真实或Mock模块。
        envelope: v1标准任务信封。
        legacy_intent: 为未迁移模块保留的旧意图视图。
        retrieval_context: 经过脱敏筛选的历史规划参考。

    Returns:
        标准计划和组合式接口可能同时返回的原始elements。

    Raises:
        Group2AdapterError: 规划返回类型或steps字段不合法。
    """
    planning_request: PlanningRequestV1 = {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": envelope["task_trace_id"],
        "attempt_id": envelope["attempt_id"],
        "intent": dict(envelope["intent"]),  # type: ignore[typeddict-item]
        "retrieval_context": list(retrieval_context or []),
    }
    contract_version = str(getattr(module, "contract_version", "legacy"))
    raw_result = module.plan(
        planning_request if contract_version == CONTRACT_VERSION_V1 else legacy_intent
    )
    if not isinstance(raw_result, dict):
        raise Group2AdapterError(ErrorCode.E2002, "规划结果必须是对象")
    steps = raw_result.get("steps")
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise Group2AdapterError(ErrorCode.E2002, "规划结果 steps 必须是对象数组")

    # legacy_fields 只用于第三组尚未迁移期间重建旧计划视图；elements由检测
    # 阶段单独处理，不能继续隐藏在规划阶段内部。
    legacy_fields = {
        key: value
        for key, value in raw_result.items()
        if key not in {"steps", "elements"}
    }
    plan: PlanResultV1 = {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": envelope["task_trace_id"],
        "plan_id": str(raw_result.get("plan_id") or f"plan-{envelope['attempt_id']}"),
        "steps": [dict(step) for step in steps],
        "metadata": {
            "source_contract": contract_version,
            "legacy_fields": legacy_fields,
        },
    }
    return plan, raw_result.get("elements")


def adapt_group2_detection(
    module: Any,
    envelope: TaskEnvelopeV1,
    plan: PlanResultV1,
    combined_elements: Optional[Any] = None,
) -> DetectionResultV1:
    """
    调用或兼容第二组控件检测能力。

    Args:
        module: 已注册的第二组模块。
        envelope: v1标准任务信封。
        plan: 已标准化的规划结果。
        combined_elements: 组合式规划接口已经返回的elements。

    Returns:
        completed或degraded状态的标准检测结果。

    Raises:
        Group2AdapterError: 检测接口返回不可识别的数据结构。
    """
    source = "combined"
    raw_result = combined_elements
    if raw_result is None and callable(getattr(module, "detect", None)):
        source = "detect"
        detection_request: DetectionRequestV1 = {
            "contract_version": CONTRACT_VERSION_V1,
            "task_trace_id": envelope["task_trace_id"],
            "attempt_id": envelope["attempt_id"],
            "plan": plan,
        }
        contract_version = str(getattr(module, "contract_version", "legacy"))
        raw_result = module.detect(
            detection_request if contract_version == CONTRACT_VERSION_V1 else plan
        )
    elif raw_result is None and callable(getattr(module, "detect_elements", None)):
        source = "detect_elements"
        raw_result = module.detect_elements()

    if raw_result is None:
        return {
            "contract_version": CONTRACT_VERSION_V1,
            "task_trace_id": envelope["task_trace_id"],
            "status": "degraded",
            "elements": {},
            "metadata": {"reason": "group2_detection_not_supported"},
        }

    if isinstance(raw_result, dict) and "elements" in raw_result:
        elements = raw_result["elements"]
    else:
        elements = raw_result
    if not isinstance(elements, (dict, list)):
        raise Group2AdapterError(
            ErrorCode.E2004,
            "控件检测结果 elements 必须是对象或数组",
        )

    return {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": envelope["task_trace_id"],
        "status": "completed",
        "elements": elements,
        "metadata": {"source": source},
    }


def build_legacy_execution_plan(
    plan: PlanResultV1,
    detection: DetectionResultV1,
) -> Dict[str, Any]:
    """
    为尚未迁移的第三组构造旧计划视图。

    Args:
        plan: 标准规划结果。
        detection: 标准控件检测结果。

    Returns:
        包含steps、elements及旧规划扩展字段的字典。
    """
    legacy_fields = plan.get("metadata", {}).get("legacy_fields", {})
    result = dict(legacy_fields) if isinstance(legacy_fields, dict) else {}
    result.update(
        {
            "plan_id": plan["plan_id"],
            "trace_id": plan["task_trace_id"],
            "steps": [dict(step) for step in plan["steps"]],
            "elements": detection["elements"],
            "detection_status": detection["status"],
        }
    )
    return result


__all__ = [
    "Group2AdapterError",
    "adapt_group2_detection",
    "adapt_group2_plan",
    "build_legacy_execution_plan",
]
