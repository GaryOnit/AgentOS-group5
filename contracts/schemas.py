"""
数据结构契约定义（schemas.py）

使用 TypedDict 定义组间通信所需的数据结构。
所有组件必须遵守本模块定义的接口规范。
"""

from typing import Any, Dict, List, Literal, Optional, TypedDict


# 风险等级类型，从低到高
RiskLevel = Literal["low", "medium", "high", "critical"]

# 流程阶段类型，包含安全检查、规划、执行、工具调用、完成
Stage = Literal["security", "planning", "execution", "tool_call", "completed"]


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
