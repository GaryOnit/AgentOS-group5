"""进程内工具调用ID占用记录。"""

import threading
from typing import Set


class ToolCallLedger:
    """在SQLite工具调用表落地前防止同一tool_call_id重复执行。"""

    def __init__(self) -> None:
        """初始化空ID集合和线程锁。"""
        self._claimed: Set[str] = set()
        self._lock = threading.Lock()

    def claim(self, tool_call_id: str) -> bool:
        """
        原子占用一个工具调用ID。

        Args:
            tool_call_id: 第三组为逻辑工具调用生成的稳定ID。

        Returns:
            首次占用返回True，重复提交返回False。
        """
        with self._lock:
            if tool_call_id in self._claimed:
                return False
            self._claimed.add(tool_call_id)
            return True


__all__ = ["ToolCallLedger"]
