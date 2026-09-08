"""
第五组 v1 公共契约的运行时校验。

TypedDict 只服务于静态类型检查，无法阻止外组在运行时传入缺字段、错类型或
未知版本的数据。本模块在跨组边界执行显式校验，并以稳定异常类型报告问题。
"""

from datetime import datetime
from typing import Any, Dict, Iterable

from group5.contracts.schemas import (
    CONTRACT_VERSION_V1,
    OrchestrateResultV1,
    OrchestrationStatus,
    Stage,
    TaskEnvelopeV1,
)


_VALID_STAGES = {
    "intent",
    "security",
    "planning",
    "detection",
    "execution",
    "tool_authorization",
    "tool_execution",
    "tool_call",
    "persistence",
    "completed",
}

_VALID_STATUSES = {
    "running",
    "completed",
    "failed",
    "needs_input",
    "needs_confirmation",
    "needs_redetection",
    "tool_required",
    "cancelled",
    "cancelled_with_side_effects",
    "partial_failure",
}


class ContractValidationError(ValueError):
    """
    公共契约校验失败。

    Args:
        message: 面向开发和联调人员的稳定错误说明。
        field: 出错字段路径；顶层错误时可为空。
    """

    def __init__(self, message: str, field: str = "") -> None:
        self.field = field
        prefix = f"{field}: " if field else ""
        super().__init__(prefix + message)


def _require_mapping(value: Any, field: str) -> Dict[str, Any]:
    """
    校验并返回字典对象。

    Args:
        value: 待校验的任意对象。
        field: 用于错误消息的字段路径。

    Returns:
        通过校验的字典对象。
    """
    if not isinstance(value, dict):
        raise ContractValidationError("必须是对象", field)
    return value


def _require_fields(value: Dict[str, Any], fields: Iterable[str], field: str) -> None:
    """
    校验对象包含全部必需字段。

    Args:
        value: 已确认是字典的对象。
        fields: 必需字段名称集合。
        field: 当前对象的字段路径。
    """
    missing = [name for name in fields if name not in value]
    if missing:
        raise ContractValidationError(
            f"缺少必需字段: {', '.join(sorted(missing))}", field
        )


def _require_non_empty_string(value: Any, field: str) -> str:
    """
    校验非空字符串。

    Args:
        value: 待校验值。
        field: 字段路径。

    Returns:
        去除首尾空白后的字符串。
    """
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError("必须是非空字符串", field)
    return value.strip()


def _validate_timestamp(value: Any, field: str) -> None:
    """
    校验 ISO-8601 时间戳。

    Args:
        value: 时间戳字符串。
        field: 字段路径。
    """
    timestamp = _require_non_empty_string(value, field)
    try:
        # ``Z`` 是常见的 UTC 表示；转换为显式偏移后兼容更多 Python 版本。
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractValidationError("必须是有效的 ISO-8601 时间戳", field) from exc


def validate_task_envelope_v1(value: Any) -> TaskEnvelopeV1:
    """
    校验第五组 v1 标准任务信封。

    Args:
        value: 外组适配器或结构化核心入口提交的对象。

    Returns:
        通过校验的原始任务信封。

    Raises:
        ContractValidationError: 缺字段、错类型或契约版本不受支持。
    """
    envelope = _require_mapping(value, "task")
    _require_fields(
        envelope,
        ("contract_version", "task_trace_id", "attempt_id", "timestamp", "intent"),
        "task",
    )

    if envelope["contract_version"] != CONTRACT_VERSION_V1:
        raise ContractValidationError("不支持的契约版本", "task.contract_version")

    _require_non_empty_string(envelope["task_trace_id"], "task.task_trace_id")
    _require_non_empty_string(envelope["attempt_id"], "task.attempt_id")
    _validate_timestamp(envelope["timestamp"], "task.timestamp")

    intent = _require_mapping(envelope["intent"], "task.intent")
    _require_fields(
        intent,
        ("category", "action", "target", "params", "raw_text"),
        "task.intent",
    )
    _require_non_empty_string(intent["category"], "task.intent.category")
    _require_non_empty_string(intent["action"], "task.intent.action")
    _require_non_empty_string(intent["target"], "task.intent.target")
    _require_mapping(intent["params"], "task.intent.params")
    if not isinstance(intent["raw_text"], str):
        raise ContractValidationError("必须是字符串", "task.intent.raw_text")

    return envelope  # type: ignore[return-value]


def validate_orchestrate_result_v1(value: Any) -> OrchestrateResultV1:
    """
    校验 v1 对外编排结果及阶段摘要。

    Args:
        value: 待返回给 AI Shell 或其他调用方的结果对象。

    Returns:
        通过校验的原始结果对象。

    Raises:
        ContractValidationError: 结果字段、状态关联或阶段摘要不合法。
    """
    result = _require_mapping(value, "result")
    _require_fields(
        result,
        (
            "contract_version",
            "success",
            "status",
            "task_trace_id",
            "attempt_id",
            "stage",
            "total_latency_ms",
            "stages",
            "result",
            "error",
        ),
        "result",
    )

    if result["contract_version"] != CONTRACT_VERSION_V1:
        raise ContractValidationError("不支持的契约版本", "result.contract_version")
    if not isinstance(result["success"], bool):
        raise ContractValidationError("必须是布尔值", "result.success")
    if result["status"] not in _VALID_STATUSES:
        raise ContractValidationError("未知编排状态", "result.status")
    if result["stage"] not in _VALID_STAGES:
        raise ContractValidationError("未知流程阶段", "result.stage")

    # completed 与 success 必须保持一致，暂停和失败状态不能被报告为成功。
    if result["success"] != (result["status"] == "completed"):
        raise ContractValidationError("success 与 status 不一致", "result.success")

    _require_non_empty_string(result["task_trace_id"], "result.task_trace_id")
    _require_non_empty_string(result["attempt_id"], "result.attempt_id")

    latency = result["total_latency_ms"]
    if not isinstance(latency, (int, float)) or isinstance(latency, bool) or latency < 0:
        raise ContractValidationError("必须是非负数", "result.total_latency_ms")

    stages = result["stages"]
    if not isinstance(stages, list):
        raise ContractValidationError("必须是数组", "result.stages")
    for index, item in enumerate(stages):
        summary = _require_mapping(item, f"result.stages[{index}]")
        _require_fields(summary, ("name", "status", "latency_ms", "attempts"), f"result.stages[{index}]")
        if summary["name"] not in _VALID_STAGES:
            raise ContractValidationError("未知流程阶段", f"result.stages[{index}].name")
        if not isinstance(summary["latency_ms"], (int, float)) or isinstance(summary["latency_ms"], bool) or summary["latency_ms"] < 0:
            raise ContractValidationError("必须是非负数", f"result.stages[{index}].latency_ms")
        if not isinstance(summary["attempts"], int) or isinstance(summary["attempts"], bool) or summary["attempts"] < 1:
            raise ContractValidationError("必须是大于等于 1 的整数", f"result.stages[{index}].attempts")

    if result["success"] and result["error"] is not None:
        raise ContractValidationError("成功结果的 error 必须为空", "result.error")
    if not result["success"]:
        error = _require_mapping(result["error"], "result.error")
        _require_non_empty_string(error.get("code"), "result.error.code")
        _require_non_empty_string(error.get("message"), "result.error.message")
        if not isinstance(error.get("retryable"), bool):
            raise ContractValidationError("必须是布尔值", "result.error.retryable")

    return result  # type: ignore[return-value]


__all__ = [
    "ContractValidationError",
    "validate_task_envelope_v1",
    "validate_orchestrate_result_v1",
]
