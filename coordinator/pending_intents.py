"""需要用户补充参数的短期任务上下文存储。"""

import threading
import time
from typing import Dict, List, Optional, TypedDict


class PendingIntent(TypedDict):
    """待补参任务的最小上下文。"""

    task_trace_id: str
    context: List[str]
    expires_at: float


class PendingIntentStore:
    """
    线程安全的短期待补参存储。

    该存储只保存继续理解意图所需的文本上下文，并按单调时钟自动过期。
    它不承担长期审计或RAG职责，SQLite存储也不应直接复用此对象。
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        """
        初始化待补参存储。

        Args:
            ttl_seconds: 每个待补参任务的存活秒数；0 表示立即过期。
        """
        self._ttl_seconds = max(float(ttl_seconds), 0.0)
        self._items: Dict[str, PendingIntent] = {}
        self._lock = threading.Lock()

    def put(self, task_trace_id: str, context: List[str]) -> None:
        """
        保存或更新待补参任务。

        Args:
            task_trace_id: 整个用户任务的稳定追踪 ID。
            context: 已发生的用户指令序列。
        """
        item: PendingIntent = {
            "task_trace_id": task_trace_id,
            "context": [text for text in context if isinstance(text, str) and text],
            "expires_at": time.monotonic() + self._ttl_seconds,
        }
        with self._lock:
            self._items[task_trace_id] = item

    def get(self, task_trace_id: str) -> Optional[PendingIntent]:
        """
        获取仍然有效的待补参任务。

        Args:
            task_trace_id: 待继续任务的追踪 ID。

        Returns:
            上下文副本；不存在或已过期时返回 None。
        """
        with self._lock:
            item = self._items.get(task_trace_id)
            if item is None:
                return None
            if item["expires_at"] <= time.monotonic():
                del self._items[task_trace_id]
                return None
            return {
                "task_trace_id": item["task_trace_id"],
                "context": list(item["context"]),
                "expires_at": item["expires_at"],
            }

    def remove(self, task_trace_id: str) -> bool:
        """
        删除待补参任务。

        Args:
            task_trace_id: 待删除任务的追踪 ID。

        Returns:
            删除前任务存在时返回 True。
        """
        with self._lock:
            return self._items.pop(task_trace_id, None) is not None


__all__ = ["PendingIntent", "PendingIntentStore"]
