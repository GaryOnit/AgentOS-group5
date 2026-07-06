"""
第5组 - 系统协调层（System Coordinator Layer）
Linux Agentic OS 课程项目

本包负责：
- 安全沙箱检查（SecuritySandbox）
- 系统协调器（SystemCoordinator）
- 知识检索（RAGKnowledgeBase）
- 审计日志（AuditLogger）
- 接口契约（contracts）
"""

__version__ = "1.0.0"
__author__ = "Group 5"

# NOTE:
# 为了避免在仅使用子模块（如 group5.security）时被动触发重依赖导入，
# 这里不再做顶层 re-export。请按需从子模块导入：
# from group5.coordinator.system_coordinator import SystemCoordinator
# from group5.security.security_sandbox import SecuritySandbox
# from group5.knowledge.rag_kb import RAGKnowledgeBase
# from group5.audit.audit_logger import AuditLogger

__all__ = []
