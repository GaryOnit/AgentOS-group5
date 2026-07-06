"""
Mock 模块实现（mock_modules.py）

提供 Group1~4 各组的 Mock 实现，用于测试和演示。
每个 Mock 返回符合接口契约的静态测试数据。

Mock 实现约定：
- MockGroup1HostAgent: parse(raw_text) → IntentJSON
- MockGroup2Planner: plan(intent) → Dict（包含步骤列表）
- MockGroup3Executor: execute(plan) → Dict（包含执行结果）
- MockGroup4ToolRegistry: call_tool(tool_name, params) → Dict
"""

import time
from typing import Any, Dict, Optional

from group5.contracts.schemas import IntentJSON


class MockGroup1HostAgent:
    """
    Mock Group1 HostAgent（宿主代理）

    模拟将自然语言文本解析为结构化 IntentJSON。
    根据输入文本中的关键词返回不同的意图数据。
    """

    def __init__(self, latency_ms: float = 10.0) -> None:
        """
        Args:
            latency_ms: 模拟响应延迟（毫秒）
        """
        self._latency_ms = latency_ms

    def parse(self, raw_text: str) -> IntentJSON:
        """
        将自然语言文本解析为 IntentJSON

        Args:
            raw_text: 用户输入的原始文本

        Returns:
            解析后的 IntentJSON
        """
        time.sleep(self._latency_ms / 1000)

        # 根据关键词识别意图（简化实现）
        raw_lower = raw_text.lower()

        if "rm" in raw_lower or "删除所有" in raw_text or "rm -rf" in raw_lower:
            return {
                "trace_id": "",
                "action": "rm",
                "target": "-rf /",
                "params": {"force": True, "recursive": True},
                "raw_text": raw_text,
            }
        elif "etc/passwd" in raw_lower or "密码文件" in raw_text:
            return {
                "trace_id": "",
                "action": "navigate",
                "target": "/etc/passwd",
                "params": {},
                "raw_text": raw_text,
            }
        elif "打开" in raw_text or "open" in raw_lower:
            return {
                "trace_id": "",
                "action": "open",
                "target": "/home/user/Documents",
                "params": {},
                "raw_text": raw_text,
            }
        elif "导航" in raw_text or "navigate" in raw_lower:
            return {
                "trace_id": "",
                "action": "navigate",
                "target": "/home/user/Documents",
                "params": {},
                "raw_text": raw_text,
            }
        elif "创建" in raw_text or "create" in raw_lower:
            return {
                "trace_id": "",
                "action": "create",
                "target": "/home/user/new_folder",
                "params": {"type": "directory"},
                "raw_text": raw_text,
            }
        elif "下载" in raw_text or "download" in raw_lower:
            return {
                "trace_id": "",
                "action": "navigate",
                "target": "/home/user/Downloads",
                "params": {},
                "raw_text": raw_text,
            }
        else:
            # 默认：open 操作
            return {
                "trace_id": "",
                "action": "open",
                "target": "/home/user",
                "params": {},
                "raw_text": raw_text,
            }

    def ping(self) -> Dict[str, Any]:
        """健康检查"""
        return {
            "module": "mock_group1_host_agent",
            "healthy": True,
            "latency_ms": self._latency_ms,
        }


class MockGroup2Planner:
    """
    Mock Group2 Planner（规划器）

    模拟将 IntentJSON 转换为执行计划（步骤列表）。
    """

    def __init__(self, latency_ms: float = 20.0) -> None:
        """
        Args:
            latency_ms: 模拟响应延迟（毫秒）
        """
        self._latency_ms = latency_ms

    def plan(self, intent: IntentJSON) -> Dict[str, Any]:
        """
        根据意图生成执行计划

        Args:
            intent: IntentJSON 格式的用户意图

        Returns:
            规划结果字典，包含：
            - steps: 执行步骤列表
            - plan_id: 计划 ID
            - estimated_duration_ms: 预估耗时
        """
        time.sleep(self._latency_ms / 1000)

        action = intent.get("action", "open")
        target = intent.get("target", "/home/user")
        trace_id = intent.get("trace_id", "unknown")

        return {
            "plan_id": f"plan-{trace_id[:8]}",
            "trace_id": trace_id,
            "action": action,
            "target": target,
            "steps": [
                {"step": 1, "op": "validate_path", "args": {"path": target}},
                {"step": 2, "op": action, "args": {"target": target}},
                {"step": 3, "op": "report_status", "args": {"status": "pending"}},
            ],
            "estimated_duration_ms": 100,
            "tool_hint": "file_manager",  # 建议使用的工具
        }

    def ping(self) -> Dict[str, Any]:
        """健康检查"""
        return {
            "module": "mock_group2_planner",
            "healthy": True,
            "latency_ms": self._latency_ms,
        }


class MockGroup3Executor:
    """
    Mock Group3 Executor（执行器）

    模拟执行规划器生成的计划并返回执行结果。
    """

    def __init__(self, latency_ms: float = 30.0) -> None:
        """
        Args:
            latency_ms: 模拟响应延迟（毫秒）
        """
        self._latency_ms = latency_ms

    def execute(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行计划并返回结果

        Args:
            plan: Group2 规划器输出的计划字典

        Returns:
            执行结果字典，包含：
            - output: 执行输出描述
            - executed_steps: 已执行步骤数
            - tool_name: 需要调用的工具名称（可选）
            - tool_params: 工具调用参数（可选）
        """
        time.sleep(self._latency_ms / 1000)

        action = plan.get("action", "open")
        target = plan.get("target", "/home/user")
        steps = plan.get("steps", [])

        return {
            "output": f"成功执行 {action} 操作，目标: {target}",
            "executed_steps": len(steps),
            "action": action,
            "target": target,
            "status": "success",
            # 指示协调器调用 Group4 的工具
            "tool_name": plan.get("tool_hint"),
            "tool_params": {
                "action": action,
                "path": target,
            },
        }

    def ping(self) -> Dict[str, Any]:
        """健康检查"""
        return {
            "module": "mock_group3_executor",
            "healthy": True,
            "latency_ms": self._latency_ms,
        }


class MockGroup4ToolRegistry:
    """
    Mock Group4 ToolRegistry（工具注册表）

    模拟工具调用，如打开文件管理器、执行 Shell 命令等。
    """

    def __init__(self, latency_ms: float = 15.0) -> None:
        """
        Args:
            latency_ms: 模拟响应延迟（毫秒）
        """
        self._latency_ms = latency_ms
        # 已注册的工具（工具名 → 处理函数描述）
        self._tools: Dict[str, str] = {
            "file_manager": "文件管理器工具，支持打开/导航/创建文件夹",
            "shell": "Shell 命令执行工具（受安全沙箱保护）",
            "app_launcher": "应用启动工具",
            "text_editor": "文本编辑工具",
        }

    def call_tool(self, tool_name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        调用指定工具

        Args:
            tool_name: 工具名称
            params: 工具调用参数

        Returns:
            工具执行结果字典
        """
        time.sleep(self._latency_ms / 1000)

        params = params or {}
        action = params.get("action", "open")
        path = params.get("path", "/home/user")

        if tool_name not in self._tools:
            return {
                "tool_name": tool_name,
                "success": False,
                "error": f"工具 '{tool_name}' 未注册",
            }

        return {
            "tool_name": tool_name,
            "success": True,
            "output": f"工具 '{tool_name}' 成功执行: {action} {path}",
            "tool_description": self._tools[tool_name],
            "params": params,
        }

    def list_tools(self) -> Dict[str, str]:
        """
        列出所有已注册的工具

        Returns:
            工具名 → 描述 的字典
        """
        return dict(self._tools)

    def ping(self) -> Dict[str, Any]:
        """健康检查"""
        return {
            "module": "mock_group4_tool_registry",
            "healthy": True,
            "latency_ms": self._latency_ms,
            "tool_count": len(self._tools),
        }


class SlowMockGroup2Planner(MockGroup2Planner):
    """
    慢速 Mock Group2 Planner（用于测试超时场景）

    模拟响应时间超过 3s 的规划器，用于测试 E5002 超时错误。
    """

    def __init__(self, sleep_seconds: float = 4.0) -> None:
        """
        Args:
            sleep_seconds: 模拟的阻塞时间（秒），默认 4s（超过 3s 超时阈值）
        """
        super().__init__(latency_ms=sleep_seconds * 1000)
        self._sleep_seconds = sleep_seconds

    def plan(self, intent: IntentJSON) -> Dict[str, Any]:
        """阻塞 sleep_seconds 秒，模拟超时"""
        time.sleep(self._sleep_seconds)
        return super().plan(intent)


if __name__ == "__main__":
    # 独立运行示例：展示各 Mock 模块功能
    print("=== Mock 模块示例 ===\n")

    # Group1 HostAgent
    agent = MockGroup1HostAgent()
    intent = agent.parse("打开文档文件夹")
    print(f"Group1 解析结果: action={intent['action']}, target={intent['target']}")

    # Group2 Planner
    planner = MockGroup2Planner()
    plan = planner.plan(intent)
    print(f"Group2 规划结果: plan_id={plan['plan_id']}, steps={len(plan['steps'])}")

    # Group3 Executor
    executor = MockGroup3Executor()
    exec_result = executor.execute(plan)
    print(f"Group3 执行结果: output={exec_result['output']}")

    # Group4 ToolRegistry
    tool_registry = MockGroup4ToolRegistry()
    tool_result = tool_registry.call_tool("file_manager", {"action": "open", "path": "/home/user/Documents"})
    print(f"Group4 工具结果: success={tool_result['success']}, output={tool_result['output']}")

    print("\n=== 健康检查 ===")
    for mock in [agent, planner, executor, tool_registry]:
        ping = mock.ping()
        print(f"  ✅ {ping['module']}: latency={ping['latency_ms']}ms")

    print("\n✅ mock_modules.py 验证通过")
