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
)
from group5.contracts.error_codes import ErrorCode, ERROR_MESSAGES, make_error

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
    "ErrorCode",
    "ERROR_MESSAGES",
    "make_error",
]
