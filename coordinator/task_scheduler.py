"""GUI/变更串行、只读并发的任务调度与协作式取消。"""

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict


class TaskCancelled(RuntimeError):
    """任务在安全检查点观察到取消请求。"""


class CancellationToken:
    """可在线程间安全传递的协作式取消令牌。"""

    def __init__(self) -> None:
        """初始化未取消状态。"""
        self._event = threading.Event()

    def cancel(self) -> None:
        """请求任务在下一个安全检查点停止。"""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """返回当前是否已经请求取消。"""
        return self._event.is_set()

    def checkpoint(self) -> None:
        """
        在任务安全边界检查取消状态。

        Raises:
            TaskCancelled: 调用方已经请求取消。
        """
        if self.cancelled:
            raise TaskCancelled("任务已取消")


@dataclass
class ScheduledTask:
    """提交给调度器后返回的公开任务句柄。"""

    task_id: str
    kind: str
    token: CancellationToken
    future: Future


class TaskScheduler:
    """
    为共享Linux桌面提供有界并发调度。

    gui和mutation任务进入同一个单线程池，避免抢占窗口焦点或并发修改文件；
    read_only任务进入独立线程池，可与变更任务并行。
    """

    def __init__(self, read_only_workers: int = 4) -> None:
        """
        初始化变更和只读执行池。

        Args:
            read_only_workers: 只读查询最大并发数，最小为1。
        """
        self._mutation_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="group5-desktop-mutation",
        )
        self._read_executor = ThreadPoolExecutor(
            max_workers=max(int(read_only_workers), 1),
            thread_name_prefix="group5-read-only",
        )
        self._tasks: Dict[str, ScheduledTask] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        task_id: str,
        kind: str,
        operation: Callable[[CancellationToken], Any],
    ) -> ScheduledTask:
        """
        提交一个GUI、变更或只读任务。

        Args:
            task_id: 调用方提供的稳定任务ID。
            kind: gui、mutation或read_only。
            operation: 接收取消令牌的任务函数。

        Returns:
            可等待、取消和检查结果的ScheduledTask。

        Raises:
            ValueError: ID为空、类型未知或同ID任务仍在运行。
        """
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id必须是非空字符串")
        if kind not in {"gui", "mutation", "read_only"}:
            raise ValueError(f"不支持的任务类型: {kind}")
        if not callable(operation):
            raise ValueError("operation必须可调用")

        token = CancellationToken()
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing is not None and not existing.future.done():
                raise ValueError(f"任务仍在运行: {task_id}")
            executor = (
                self._read_executor
                if kind == "read_only"
                else self._mutation_executor
            )
            future = executor.submit(self._run_operation, token, operation)
            handle = ScheduledTask(task_id, kind, token, future)
            self._tasks[task_id] = handle
        return handle

    def cancel(self, task_id: str) -> bool:
        """
        取消排队任务或请求运行任务协作停止。

        Args:
            task_id: 待取消任务ID。

        Returns:
            找到尚未完成任务时返回True。
        """
        with self._lock:
            handle = self._tasks.get(task_id)
            if handle is None or handle.future.done():
                return False
            handle.token.cancel()
            # 排队任务可直接取消；已经运行时cancel()返回False，但令牌仍会
            # 在任务的下一个安全检查点生效。
            handle.future.cancel()
            return True

    def shutdown(self, wait: bool = True) -> None:
        """
        停止调度器并请求所有未完成任务取消。

        Args:
            wait: 是否等待运行任务退出。
        """
        with self._lock:
            handles = list(self._tasks.values())
        for handle in handles:
            if not handle.future.done():
                handle.token.cancel()
                handle.future.cancel()
        self._mutation_executor.shutdown(wait=wait, cancel_futures=True)
        self._read_executor.shutdown(wait=wait, cancel_futures=True)

    @staticmethod
    def _run_operation(
        token: CancellationToken,
        operation: Callable[[CancellationToken], Any],
    ) -> Any:
        """
        在调用业务操作前执行首个取消检查点。

        Args:
            token: 当前任务取消令牌。
            operation: 调用方任务函数。

        Returns:
            业务操作返回值。
        """
        token.checkpoint()
        return operation(token)


__all__ = [
    "CancellationToken",
    "ScheduledTask",
    "TaskCancelled",
    "TaskScheduler",
]
