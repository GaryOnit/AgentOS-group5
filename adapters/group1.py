"""第一组 HostAgent 到第五组 v1 任务信封的适配器。"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

from group5.contracts.error_codes import ErrorCode, ERROR_MESSAGES
from group5.contracts.schemas import CONTRACT_VERSION_V1, TaskEnvelopeV1
from group5.contracts.validation import validate_task_envelope_v1


# 第一组接口文档定义的意图与动作组合。第五组在适配边界再次校验，防止
# Mock、真实模型或未来版本返回格式正确但语义不合法的数据。
_VALID_ACTIONS = {
    "应用控制": {"open", "close", "switch"},
    "文件操作": {
        "move",
        "copy",
        "delete",
        "rename",
        "create",
        "navigate",
        "organize",
    },
    "系统设置": {"adjust", "enable", "disable"},
    "信息查询": {"query", "check"},
}

_UPSTREAM_ERROR_MAP = {
    1001: ErrorCode.E0101,
    1002: ErrorCode.E0102,
    1003: ErrorCode.E0103,
    1004: ErrorCode.E0104,
}


class Group1AdapterError(ValueError):
    """
    第一组返回错误或非法结果。

    Args:
        code: 第五组统一意图错误码。
        cause: 经过适配、尚未持久化的上游原始错误信息。
        hint: 可向用户展示的补充或修复提示。
        retryable: 当前错误是否允许调用方恢复后重试。
    """

    def __init__(
        self,
        code: ErrorCode,
        cause: Optional[Dict[str, Any]] = None,
        hint: str = "",
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.cause = dict(cause or {})
        self.hint = hint
        self.retryable = retryable
        super().__init__(ERROR_MESSAGES[code])

    def to_error_detail(self) -> Dict[str, Any]:
        """
        转换为 v1 编排结果使用的统一错误对象。

        Returns:
            包含统一错误码、可恢复标志和上游原因的字典。
        """
        detail: Dict[str, Any] = {
            "code": self.code.value,
            "message": ERROR_MESSAGES[self.code],
            "retryable": self.retryable,
            "cause": dict(self.cause),
        }
        if self.hint:
            detail["hint"] = self.hint
        return detail


def adapt_group1_intent(
    module: Any,
    user_input: str,
    history: Optional[Sequence[str]] = None,
    task_trace_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> TaskEnvelopeV1:
    """
    调用第一组并包装为第五组 v1 标准任务信封。

    Args:
        module: 已通过可信注册的第一组适配器或 HostAgent 实例。
        user_input: 当前用户自然语言指令。
        history: 可选的最近历史指令。
        task_trace_id: 调用方预生成的任务追踪 ID；为空时自动生成。
        attempt_id: 当前解析尝试 ID；为空时自动生成。
        timestamp: ISO-8601 时间戳；为空时使用当前 UTC 时间。

    Returns:
        通过运行时校验的 TaskEnvelopeV1。

    Raises:
        Group1AdapterError: 第一组报错、抛异常或返回非法结构。
    """
    trace_id = task_trace_id or str(uuid.uuid4())
    current_attempt_id = attempt_id or str(uuid.uuid4())
    current_timestamp = timestamp or datetime.now(timezone.utc).isoformat()

    try:
        if callable(getattr(module, "understand_intent", None)):
            upstream = module.understand_intent(user_input, list(history or []))
        elif callable(getattr(module, "parse", None)):
            # legacy MockGroup1HostAgent.parse() 不接受 history 参数。
            upstream = module.parse(user_input)
        else:
            raise Group1AdapterError(ErrorCode.E0103)
    except Group1AdapterError:
        raise
    except Exception as exc:
        raise Group1AdapterError(
            ErrorCode.E0103,
            cause={"module": "group1", "exception": type(exc).__name__},
            hint="第一组 HostAgent 调用失败，请检查模块状态",
            retryable=True,
        ) from exc

    if not isinstance(upstream, dict):
        raise _invalid_result("第一组返回值不是对象")

    if "error" in upstream or "code" in upstream:
        raise _map_upstream_error(upstream)

    required = {"intent", "target", "action", "params"}
    missing = sorted(required - set(upstream))
    if missing:
        raise _invalid_result(f"第一组成功结果缺少字段: {', '.join(missing)}")

    category = upstream.get("intent")
    action = upstream.get("action")
    target = upstream.get("target")
    params = upstream.get("params")
    if category not in _VALID_ACTIONS:
        raise _invalid_result("第一组返回未知 intent")
    if action not in _VALID_ACTIONS[category]:
        raise _invalid_result("第一组返回的 action 与 intent 不匹配")
    if not isinstance(target, str) or not target.strip():
        raise _invalid_result("第一组返回的 target 不是非空字符串")
    if not isinstance(params, dict):
        raise _invalid_result("第一组返回的 params 不是对象")

    envelope: TaskEnvelopeV1 = {
        "contract_version": CONTRACT_VERSION_V1,
        "task_trace_id": trace_id,
        "attempt_id": current_attempt_id,
        "timestamp": current_timestamp,
        "intent": {
            "category": str(category),
            "action": str(action),
            "target": target,
            "params": dict(params),
            "raw_text": user_input,
        },
    }
    return validate_task_envelope_v1(envelope)


def _map_upstream_error(upstream: Dict[str, Any]) -> Group1AdapterError:
    """
    映射第一组 1001 至 1004 错误。

    Args:
        upstream: 第一组原始错误对象。

    Returns:
        带第五组统一错误码的适配器异常。
    """
    try:
        upstream_code = int(upstream.get("code"))
    except (TypeError, ValueError):
        upstream_code = 1004
    code = _UPSTREAM_ERROR_MAP.get(upstream_code, ErrorCode.E0104)
    cause = {
        "module": "group1",
        "code": upstream_code,
        "error": str(upstream.get("error", "")),
    }
    hint = upstream.get("hint")
    return Group1AdapterError(
        code,
        cause=cause,
        hint=hint if isinstance(hint, str) else "",
        retryable=code in {ErrorCode.E0102, ErrorCode.E0103},
    )


def _invalid_result(detail: str) -> Group1AdapterError:
    """
    构造第一组格式无效错误。

    Args:
        detail: 不包含敏感业务载荷的格式错误说明。

    Returns:
        E0104 适配器异常。
    """
    return Group1AdapterError(
        ErrorCode.E0104,
        cause={"module": "group1", "detail": detail},
        retryable=False,
    )


__all__ = ["Group1AdapterError", "adapt_group1_intent"]
