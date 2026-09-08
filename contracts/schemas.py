"""
数据结构契约定义（schemas.py）

使用 TypedDict 定义组间通信所需的数据结构。
所有组件必须遵守本模块定义的接口规范。
"""

from typing import Any, Dict, List, Literal, Optional, TypedDict


# 风险等级类型，从低到高
RiskLevel = Literal["low", "medium", "high", "critical"]

# 流程阶段类型。保留旧的 tool_call 名称，并补充 v1 编排状态机需要的阶段，
# 使旧结果仍可被读取，新流程也能精确标识意图、检测和持久化失败。
Stage = Literal[
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
]

# v1 编排结果状态。暂停状态与失败状态分开，调用方可据此决定追问、确认、
# 恢复或结束任务，而不需要解析自然语言错误消息。
OrchestrationStatus = Literal[
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
]

# 当前标准契约版本。后续不兼容变更必须提升版本，不能静默改变 v1 字段语义。
CONTRACT_VERSION_V1 = "1.0"


class IntentJSON(TypedDict):
    """
    用户意图 JSON 结构（来自 Group1 HostAgent 解析结果）

    字段说明：
        trace_id: 全链路追踪 ID（可选，系统自动生成 UUID 兜底）
        action: 操作动词，如 "open", "navigate", "create", "delete"
        target: 操作目标，如文件路径、应用名称
        params: 额外参数字典，如 {"recursive": True}
        raw_text: 原始用户输入文本
    """
    trace_id: str           # 追踪 ID
    action: str             # 操作动词
    target: str             # 操作目标
    params: Dict[str, Any]  # 额外参数
    raw_text: str           # 原始用户输入


class IntentPayloadV1(TypedDict):
    """
    v1 标准意图载荷。

    字段说明：
        category: 第一组返回的意图分类，如“应用控制”“文件操作”。
        action: 规范化动作，如 open、move、create。
        target: 操作目标实体或路径。
        params: 与动作相关的结构化参数。
        raw_text: 当前任务对应的原始用户指令。
    """
    category: str
    action: str
    target: str
    params: Dict[str, Any]
    raw_text: str


class TaskEnvelopeV1(TypedDict):
    """
    第五组内部使用的 v1 任务信封。

    第一组无需返回这些系统字段；第五组集成入口负责在调用第一组前生成
    task_trace_id 和 attempt_id，并在解析成功后补齐意图载荷。
    """
    contract_version: str
    task_trace_id: str
    attempt_id: str
    timestamp: str
    intent: IntentPayloadV1


class PlanningRequestV1(TypedDict):
    """第五组提交给第二组规划能力的 v1 请求。"""
    contract_version: str
    task_trace_id: str
    attempt_id: str
    intent: IntentPayloadV1
    retrieval_context: List[Dict[str, Any]]


class PlanResultV1(TypedDict):
    """第二组规划结果的标准化视图。"""
    contract_version: str
    task_trace_id: str
    plan_id: str
    steps: List[Dict[str, Any]]
    metadata: Dict[str, Any]


class DetectionRequestV1(TypedDict):
    """第五组提交给第二组控件检测能力的 v1 请求。"""
    contract_version: str
    task_trace_id: str
    attempt_id: str
    plan: PlanResultV1


class DetectionResultV1(TypedDict):
    """控件检测的标准化结果；degraded 表示外组尚未提供检测能力。"""
    contract_version: str
    task_trace_id: str
    status: str
    elements: Any
    metadata: Dict[str, Any]


class ExecutionRequestV1(TypedDict):
    """第五组提交给第三组的可恢复整计划执行请求。"""
    contract_version: str
    task_trace_id: str
    attempt_id: str
    plan: PlanResultV1
    detection: DetectionResultV1
    checkpoint: Optional[str]
    tool_result: Optional[Dict[str, Any]]


class ExecutionResultV1(TypedDict):
    """第三组执行结果的标准化视图。"""
    contract_version: str
    task_trace_id: str
    status: str
    completed_step_ids: List[str]
    failed_step_id: Optional[str]
    checkpoint: Optional[str]
    output: Optional[Any]
    tool_request: Optional[Dict[str, Any]]
    metadata: Dict[str, Any]


class ToolRequestV1(TypedDict):
    """第三组提交给第五组的结构化工具请求。"""
    contract_version: str
    task_trace_id: str
    attempt_id: str
    tool_call_id: str
    tool_name: str
    arguments: Dict[str, Any]


class ToolResultV1(TypedDict):
    """第五组调用第四组后返回给第三组的标准工具结果。"""
    contract_version: str
    task_trace_id: str
    tool_call_id: str
    success: bool
    output: Optional[Any]
    error: Optional[Dict[str, Any]]


class StageSummaryV1(TypedDict):
    """对外可见的精简阶段摘要，不包含模型提示词或工具敏感载荷。"""
    name: Stage
    status: str
    latency_ms: float
    attempts: int


class ErrorDetailV1(TypedDict, total=False):
    """v1 统一错误对象；cause 用于保留经过脱敏的上游原始错误。"""
    code: str
    message: str
    retryable: bool
    detail: str
    hint: str
    missing_fields: List[str]
    cause: Dict[str, Any]


class OrchestrateResultV1(TypedDict):
    """v1 编排结果，支持成功、失败和需要用户操作的暂停状态。"""
    contract_version: str
    success: bool
    status: OrchestrationStatus
    task_trace_id: str
    attempt_id: str
    stage: Stage
    total_latency_ms: float
    stages: List[StageSummaryV1]
    result: Optional[Any]
    error: Optional[ErrorDetailV1]


class CheckJSON(TypedDict):
    """
    安全检查结果 JSON 结构（来自 SecuritySandbox.check()）

    字段说明：
        approved: 是否通过安全检查
        risk_level: 风险等级
        reason: 拒绝原因（通过时为 None）
        matched_policy: 命中的策略 ID（未命中时为 None）
    """
    approved: bool                       # 是否放行
    risk_level: RiskLevel                # 风险等级
    reason: Optional[str]                # 拒绝原因
    matched_policy: Optional[str]        # 命中的策略ID


class OrchestrateResult(TypedDict):
    """
    协调器编排结果（SystemCoordinator.orchestrate() 返回值）

    字段说明：
        success: 是否整体成功
        trace_id: 全链路追踪 ID
        stage: 最终停止的阶段（失败时表示哪个阶段失败）
        result: 最终执行结果（成功时有值，失败时为 None）
        error: 错误信息字典（成功时为 null）
        latency_ms: 总耗时（毫秒）
    """
    success: bool                        # 是否成功
    trace_id: str                        # 追踪 ID
    stage: Stage                         # 最终阶段
    result: Optional[Any]                # 执行结果
    error: Optional[Dict[str, Any]]      # 错误信息（成功时为 None）
    latency_ms: float                    # 总耗时毫秒


class TraceRecord(TypedDict):
    """
    链路追踪记录（存入 RAG 知识库的单条记录）

    字段说明：
        trace_id: 全链路追踪 ID
        action: 操作动词
        target: 操作目标
        stage: 最终阶段
        success: 是否成功
        summary: 自然语言摘要（用于 TF-IDF 检索）
        timestamp: ISO8601 时间戳
        metadata: 额外元数据
    """
    trace_id: str                        # 追踪 ID
    action: str                          # 操作动词
    target: str                          # 操作目标
    stage: Stage                         # 最终阶段
    success: bool                        # 是否成功
    summary: str                         # 自然语言摘要
    timestamp: str                       # 时间戳（ISO8601）
    metadata: Dict[str, Any]             # 额外元数据


class AuditEvent(TypedDict):
    """
    审计日志事件（AuditLogger 记录的单条事件）

    字段说明：
        event_id: 事件唯一 ID
        trace_id: 关联的追踪 ID
        event_type: 事件类型，如 "orchestrate_start", "security_block"
        level: 日志级别，如 "INFO", "WARN", "ERROR"
        message: 事件描述
        payload: 事件详细数据
        timestamp: ISO8601 时间戳
    """
    event_id: str                        # 事件 ID
    trace_id: str                        # 追踪 ID
    event_type: str                      # 事件类型
    level: str                           # 日志级别（INFO/WARN/ERROR）
    message: str                         # 事件描述
    payload: Dict[str, Any]              # 事件详情
    timestamp: str                       # 时间戳（ISO8601）


class SecurityPolicy(TypedDict):
    """
    安全策略定义（security_policies.json 中的单条规则）

    字段说明：
        id: 策略唯一标识，如 "SEC-001"
        name: 策略名称
        description: 策略描述
        action_pattern: 操作动词正则（可选）
        target_pattern: 目标路径正则（可选）
        risk_level: 违规时的风险等级
        block: 是否拦截（False 则仅标记风险）
        enabled: 是否启用
    """
    id: str                              # 策略 ID
    name: str                            # 策略名称
    description: str                     # 策略描述
    action_pattern: Optional[str]        # 操作正则（可选）
    target_pattern: Optional[str]        # 目标路径正则（可选）
    risk_level: RiskLevel                # 风险等级
    block: bool                          # 是否拦截
    enabled: bool                        # 是否启用


class HealthStatus(TypedDict):
    """
    模块健康状态（health_check() 返回值）

    字段说明：
        module: 模块名称
        healthy: 是否健康
        latency_ms: ping 耗时（毫秒）
        detail: 详细信息（可选）
    """
    module: str                          # 模块名称
    healthy: bool                        # 是否健康
    latency_ms: float                    # ping 耗时
    detail: Optional[str]                # 详细信息


class QueryResult(TypedDict):
    """
    RAG 知识库查询结果（RAGKnowledgeBase.query() 返回的单条结果）

    字段说明：
        trace_id: 追踪 ID
        score: 相似度分数（0.0~1.0）
        summary: 记录摘要
        action: 操作动词
        target: 操作目标
        success: 是否成功
        timestamp: 时间戳
    """
    trace_id: str                        # 追踪 ID
    score: float                         # 相似度分数
    summary: str                         # 摘要
    action: str                          # 操作动词
    target: str                          # 操作目标
    success: bool                        # 是否成功
    timestamp: str                       # 时间戳


if __name__ == "__main__":
    # 独立运行示例：验证 TypedDict 结构可正常实例化
    intent: IntentJSON = {
        "trace_id": "trace-001",
        "action": "open",
        "target": "/home/user/Documents",
        "params": {},
        "raw_text": "打开文档文件夹",
    }
    print("IntentJSON 示例:", intent)

    result: OrchestrateResult = {
        "success": True,
        "trace_id": "trace-001",
        "stage": "completed",
        "result": {"output": "已打开文件管理器"},
        "error": None,
        "latency_ms": 42.5,
    }
    print("OrchestrateResult 示例:", result)
    print("✅ schemas.py 验证通过")
