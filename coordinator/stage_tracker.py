"""单次编排的阶段状态与耗时追踪。"""

import time
from typing import List, Optional

from group5.contracts.schemas import Stage, StageSummaryV1


class StageTracker:
    """
    顺序编排流程的轻量阶段追踪器。

    每次只允许一个活动阶段。开始新阶段前会完成旧阶段，异常返回时可将仍
    活动的阶段统一标记为失败，避免每个错误分支重复维护计时代码。
    """

    def __init__(self) -> None:
        """初始化空阶段列表。"""
        self._summaries: List[StageSummaryV1] = []
        self._active_name: Optional[Stage] = None
        self._active_started_at: float = 0.0
        self._active_attempts: int = 1

    def begin(self, name: Stage, attempts: int = 1) -> None:
        """
        开始一个新阶段。

        Args:
            name: 标准流程阶段名称。
            attempts: 当前阶段已知的尝试次数，最小为1。

        Raises:
            RuntimeError: 上一阶段尚未结束。
        """
        if self._active_name is not None:
            raise RuntimeError(f"阶段尚未结束: {self._active_name}")
        self._active_name = name
        self._active_started_at = time.perf_counter()
        self._active_attempts = max(int(attempts), 1)

    def finish(self, status: str = "completed", attempts: Optional[int] = None) -> None:
        """
        完成当前活动阶段并记录耗时。

        Args:
            status: 对外可见的阶段状态。
            attempts: 可选的最终尝试次数，用于后续重试策略补充。

        Raises:
            RuntimeError: 当前没有活动阶段。
        """
        if self._active_name is None:
            raise RuntimeError("当前没有活动阶段")
        elapsed_ms = (time.perf_counter() - self._active_started_at) * 1000
        self._summaries.append(
            {
                "name": self._active_name,
                "status": status,
                "latency_ms": round(elapsed_ms, 2),
                "attempts": max(int(attempts or self._active_attempts), 1),
            }
        )
        self._active_name = None
        self._active_started_at = 0.0
        self._active_attempts = 1

    def set_attempts(self, attempts: int) -> None:
        """
        更新当前活动阶段的尝试次数。

        Args:
            attempts: 已开始的调用次数，最小按1记录。

        Raises:
            RuntimeError: 当前没有活动阶段。
        """
        if self._active_name is None:
            raise RuntimeError("当前没有活动阶段")
        self._active_attempts = max(int(attempts), 1)

    def finish_if_active(self, status: str) -> None:
        """
        若存在活动阶段则完成它。

        Args:
            status: 应记录的结束状态。
        """
        if self._active_name is not None:
            self.finish(status)

    def summaries(self) -> List[StageSummaryV1]:
        """
        返回阶段摘要副本。

        Returns:
            按实际执行顺序排列的阶段摘要。
        """
        return [dict(item) for item in self._summaries]  # type: ignore[return-value]


__all__ = ["StageTracker"]
