"""
Mock 模块（mocks）

提供其他组（Group1~4）的 Mock 实现，用于：
1. 独立测试第5组组件
2. 集成测试时替换真实模块
3. 演示示例（main.py）
"""

from group5.mocks.mock_modules import (
    MockGroup1HostAgent,
    MockGroup2Planner,
    MockGroup3Executor,
    MockGroup4ToolRegistry,
)

__all__ = [
    "MockGroup1HostAgent",
    "MockGroup2Planner",
    "MockGroup3Executor",
    "MockGroup4ToolRegistry",
]
