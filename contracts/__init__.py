"""
接口契约模块（contracts）

定义组间通信所需的数据结构、错误码和类型。
"""

from group5.contracts.schemas import (
    IntentJSON,
    CheckJSON,
    OrchestrateResult,
    TraceRecord,
    AuditEvent,
    SecurityPolicy,
    HealthStatus,
    QueryResult,
    RiskLevel,
    Stage,
    CONTRACT_VERSION_V1,
    ErrorDetailV1,
    DetectionRequestV1,
    DetectionResultV1,
    ExecutionRequestV1,
    ExecutionResultV1,
    ToolRequestV1,
    ToolResultV1,
    IntentPayloadV1,
    PlanningRequestV1,
    PlanResultV1,
    OrchestrateResultV1,
    OrchestrationStatus,
    StageSummaryV1,
    TaskEnvelopeV1,
)
from group5.contracts.error_codes import ErrorCode, ERROR_MESSAGES, make_error
from group5.contracts.validation import (
    ContractValidationError,
    validate_orchestrate_result_v1,
    validate_task_envelope_v1,
)

__all__ = [
    "IntentJSON",
    "CheckJSON",
    "OrchestrateResult",
    "TraceRecord",
    "AuditEvent",
    "SecurityPolicy",
    "HealthStatus",
    "QueryResult",
    "RiskLevel",
    "Stage",
    "CONTRACT_VERSION_V1",
    "ErrorDetailV1",
    "DetectionRequestV1",
    "DetectionResultV1",
    "ExecutionRequestV1",
    "ExecutionResultV1",
    "ToolRequestV1",
    "ToolResultV1",
    "IntentPayloadV1",
    "PlanningRequestV1",
    "PlanResultV1",
    "OrchestrateResultV1",
    "OrchestrationStatus",
    "StageSummaryV1",
    "TaskEnvelopeV1",
    "ErrorCode",
    "ERROR_MESSAGES",
    "make_error",
    "ContractValidationError",
    "validate_orchestrate_result_v1",
    "validate_task_envelope_v1",
]
