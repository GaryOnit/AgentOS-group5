"""
系统协调器模块（coordinator）

SystemCoordinator 是第5组的核心组件，负责：
1. 注册并管理各组模块（Group1-4）
2. 编排意图处理的完整流程
3. 超时控制和错误处理
"""

from group5.coordinator.system_coordinator import SystemCoordinator

__all__ = [
    "SystemCoordinator",
]
