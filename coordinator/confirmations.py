"""高风险意图的短期一次性确认存储。"""

import copy
import hashlib
import json
import threading
import time
import uuid
from typing import Dict, Optional, TypedDict

from group5.contracts.schemas import TaskEnvelopeV1


class PendingConfirmation(TypedDict):
    """等待用户批准的高风险任务快照。"""

    confirmation_id: str
    intent_digest: str
    task: TaskEnvelopeV1
    expires_at: float


class ConfirmationStore:
    """线程安全、一次消费的高风险任务确认存储。"""

    def __init__(self, ttl_seconds: float = 120.0) -> None:
        """
        初始化确认存储。

        Args:
            ttl_seconds: 确认请求有效秒数；0表示立即过期。
        """
        self._ttl_seconds = max(float(ttl_seconds), 0.0)
        self._items: Dict[str, PendingConfirmation] = {}
        self._lock = threading.Lock()

    def create(self, task: TaskEnvelopeV1) -> PendingConfirmation:
        """
        为高风险任务创建一次性确认请求。

        Args:
            task: 已通过契约和安全策略解析的v1任务。

        Returns:
            包含确认ID、意图摘要和过期时间的副本。
        """
        confirmation_id = str(uuid.uuid4())
        item: PendingConfirmation = {
            "confirmation_id": confirmation_id,
            "intent_digest": digest_intent(task),
            "task": copy.deepcopy(task),
            "expires_at": time.monotonic() + self._ttl_seconds,
        }
        with self._lock:
            self._items[confirmation_id] = item
        return copy.deepcopy(item)

    def consume(self, confirmation_id: str) -> Optional[PendingConfirmation]:
        """
        原子读取并删除确认请求。

        Args:
            confirmation_id: 用户正在响应的确认ID。

        Returns:
            有效确认快照；不存在、过期或已消费时返回None。
        """
        with self._lock:
            item = self._items.pop(confirmation_id, None)
        if item is None or item["expires_at"] <= time.monotonic():
            return None
        return copy.deepcopy(item)


def digest_intent(task: TaskEnvelopeV1) -> str:
    """
    计算确认绑定的规范化意图摘要。

    Args:
        task: v1任务信封。

    Returns:
        覆盖任务ID、尝试ID和完整意图的SHA-256十六进制摘要。
    """
    payload = {
        "task_trace_id": task["task_trace_id"],
        "attempt_id": task["attempt_id"],
        "intent": task["intent"],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["ConfirmationStore", "PendingConfirmation", "digest_intent"]
