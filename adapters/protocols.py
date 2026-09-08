"""
第1至第4组的最小公开适配器协议。

这些协议只描述第五组需要调用的稳定边界，不引用任何外组内部类。真实模块
和 Mock 可以通过不同适配器满足同一协议。
"""

from typing import Any, Dict, Optional, Protocol, Sequence


class Group1Adapter(Protocol):
    """第一组意图理解适配器协议。"""

    def understand_intent(
        self, user_input: str, history: Optional[Sequence[str]] = None
    ) -> Dict[str, Any]:
        """
        将自然语言解析为第一组意图结果。

        Args:
            user_input: 当前用户指令。
            history: 可选的最近历史指令。

        Returns:
            第一组成功意图或标准错误对象。
        """
        ...


class Group2Adapter(Protocol):
    """第二组任务规划适配器协议。"""

    def plan(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成任务计划。

        Args:
            request: 版本化规划请求。

        Returns:
            规划结果或组合式规划与检测结果。
        """
        ...


class Group3Adapter(Protocol):
    """第三组计划执行适配器协议。"""

    def execute(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行计划或从检查点恢复。

        Args:
            request: 版本化执行请求。

        Returns:
            完成、暂停或失败结果。
        """
        ...


class Group4Adapter(Protocol):
    """第四组工具注册表适配器协议。"""

    def call_tool(
        self, tool_name: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        调用已经过第五组授权的工具。

        Args:
            tool_name: 注册工具名称。
            params: 工具参数。

        Returns:
            标准工具执行结果。
        """
        ...


__all__ = ["Group1Adapter", "Group2Adapter", "Group3Adapter", "Group4Adapter"]
