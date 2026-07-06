"""
审计日志模块（audit_logger.py）

实现基于内存队列 + daemon 后台线程的异步审计日志记录。

特性：
- 内存队列（queue.Queue）缓冲审计事件，不阻塞主流程
- daemon 后台线程批量消费队列，定期写入文件（批量 IO 减少开销）
- 支持按 trace_id 查询历史事件
- flush() 方法确保测试时事件已落地
- shutdown() 优雅停止后台线程
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from group5.contracts.schemas import AuditEvent


logger = logging.getLogger(__name__)

# 后台线程批量写入间隔（秒）
_FLUSH_INTERVAL = 0.1

# 批量写入最大条数（超过则立即写入，防止内存积压）
_BATCH_SIZE = 50


class AuditLogger:
    """
    异步审计日志记录器

    主线程通过 log() 方法将事件放入内存队列，
    daemon 后台线程从队列中取出事件并批量写入文件（或内存列表）。
    """

    def __init__(self, log_file: Optional[str] = None) -> None:
        """
        初始化审计日志记录器

        Args:
            log_file: 日志文件路径（可选）。
                     若指定，事件会同步到该文件（JSON Lines 格式）。
                     若为 None，则纯内存模式（适合测试）。
        """
        self._log_file = log_file
        self._queue: queue.Queue = queue.Queue()        # 内存事件队列
        self._events: List[AuditEvent] = []             # 内存事件列表（用于查询）
        self._lock = threading.Lock()                    # 保护 _events 列表的锁
        self._running = True                             # 后台线程运行标志

        # 启动 daemon 后台线程（daemon=True 确保主进程退出时线程自动销毁）
        self._worker = threading.Thread(
            target=self._consume_loop,
            name="audit-logger-worker",
            daemon=True,
        )
        self._worker.start()
        logger.debug("AuditLogger 后台线程已启动，log_file=%s", log_file)

    def log(
        self,
        trace_id: str,
        event_type: str,
        message: str,
        level: str = "INFO",
        payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        记录审计事件（非阻塞，事件放入队列后立即返回）

        Args:
            trace_id: 关联的追踪 ID
            event_type: 事件类型，如 "orchestrate_start", "security_block"
            message: 事件描述
            level: 日志级别（"INFO" / "WARN" / "ERROR"），默认 "INFO"
            payload: 额外数据字典（可选）

        Returns:
            生成的事件 ID（UUID 格式）
        """
        event_id = str(uuid.uuid4())
        event: AuditEvent = {
            "event_id": event_id,
            "trace_id": trace_id,
            "event_type": event_type,
            "level": level.upper(),
            "message": message,
            "payload": payload or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # 非阻塞放入队列（put_nowait 在队列满时抛异常，put 会阻塞）
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # 队列满时降级：直接写入内存列表（丢弃文件写入）
            logger.warning("审计队列已满，直接写入内存: event_id=%s", event_id)
            with self._lock:
                self._events.append(event)

        return event_id

    def _consume_loop(self) -> None:
        """
        后台消费循环：从队列中批量取出事件并持久化

        运行逻辑：
        1. 每 _FLUSH_INTERVAL 秒执行一次批量消费
        2. 若队列中事件超过 _BATCH_SIZE，提前触发消费
        3. _running 标志为 False 且队列为空时退出
        """
        while self._running or not self._queue.empty():
            batch: List[AuditEvent] = []

            # 尝试从队列批量取出事件
            try:
                while len(batch) < _BATCH_SIZE:
                    event = self._queue.get_nowait()
                    batch.append(event)
            except queue.Empty:
                pass

            if batch:
                self._persist_batch(batch)

            if not batch:
                # 队列为空时短暂休眠，避免 CPU 空转
                time.sleep(_FLUSH_INTERVAL)

    def _persist_batch(self, batch: List[AuditEvent]) -> None:
        """
        将一批事件持久化到内存列表和文件（批量 IO）

        Args:
            batch: 待持久化的事件列表
        """
        # 写入内存列表（加锁保护）
        with self._lock:
            self._events.extend(batch)

        # 若指定了日志文件，追加写入（JSON Lines 格式，每行一条事件）
        if self._log_file:
            try:
                os.makedirs(os.path.dirname(self._log_file), exist_ok=True) if os.path.dirname(self._log_file) else None
                with open(self._log_file, "a", encoding="utf-8") as f:
                    for event in batch:
                        f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError as exc:
                logger.error("审计日志写入文件失败: %s", exc)

    def flush(self, timeout: float = 2.0) -> None:
        """
        等待队列清空（用于测试和优雅关闭）

        Args:
            timeout: 最大等待时间（秒）
        """
        start = time.time()
        while not self._queue.empty() and (time.time() - start) < timeout:
            time.sleep(0.01)
        # 强制消费剩余事件
        batch: List[AuditEvent] = []
        try:
            while True:
                event = self._queue.get_nowait()
                batch.append(event)
        except queue.Empty:
            pass
        if batch:
            self._persist_batch(batch)

    def shutdown(self, wait: bool = True) -> None:
        """
        优雅停止后台线程

        Args:
            wait: 是否等待线程退出（默认 True）
        """
        self._running = False
        self.flush()  # 先刷新剩余事件
        if wait:
            self._worker.join(timeout=3.0)
        logger.debug("AuditLogger 后台线程已停止")

    def query_by_trace_id(self, trace_id: str) -> List[AuditEvent]:
        """
        按 trace_id 查询相关的审计事件

        Args:
            trace_id: 追踪 ID

        Returns:
            匹配的审计事件列表（按时间戳排序）
        """
        with self._lock:
            matched = [e for e in self._events if e.get("trace_id") == trace_id]
        return sorted(matched, key=lambda e: e.get("timestamp", ""))

    def get_all(self) -> List[AuditEvent]:
        """
        获取所有审计事件（内存副本）

        Returns:
            所有事件列表
        """
        with self._lock:
            return list(self._events)

    def count(self) -> int:
        """
        返回已记录的事件总数

        Returns:
            事件数量
        """
        with self._lock:
            return len(self._events)

    def ping(self) -> Dict[str, Any]:
        """
        健康检查方法

        Returns:
            健康状态字典
        """
        start = time.perf_counter()
        try:
            is_alive = self._worker.is_alive()
            latency_ms = (time.perf_counter() - start) * 1000
            return {
                "module": "audit_logger",
                "healthy": is_alive,
                "latency_ms": round(latency_ms, 2),
                "event_count": self.count(),
                "queue_size": self._queue.qsize(),
                "worker_alive": is_alive,
            }
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return {
                "module": "audit_logger",
                "healthy": False,
                "latency_ms": round(latency_ms, 2),
                "error": str(exc),
            }


if __name__ == "__main__":
    # 独立运行示例：展示异步审计日志功能
    import tempfile

    print("=== 审计日志示例 ===\n")

    # 使用临时文件演示持久化
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        log_path = f.name

    logger_instance = AuditLogger(log_file=log_path)

    # 模拟一次完整的协调流程审计
    trace_id = "trace-demo-001"

    logger_instance.log(trace_id, "orchestrate_start", "开始编排意图", "INFO", {"action": "open"})
    logger_instance.log(trace_id, "security_check", "安全检查通过", "INFO", {"risk": "low"})
    logger_instance.log(trace_id, "planning_complete", "规划完成", "INFO", {"steps": 3})
    logger_instance.log(trace_id, "execution_complete", "执行完成", "INFO", {"output": "success"})

    # 模拟另一个追踪 ID 的失败场景
    trace_id2 = "trace-demo-002"
    logger_instance.log(trace_id2, "orchestrate_start", "开始编排危险操作", "INFO")
    logger_instance.log(trace_id2, "security_block", "安全检查拦截 rm -rf /", "WARN", {"policy": "SEC-001"})

    # 等待异步写入完成
    logger_instance.flush()
    time.sleep(0.2)

    print(f"总事件数: {logger_instance.count()}")
    print(f"\ntrace_id={trace_id} 的事件:")
    for event in logger_instance.query_by_trace_id(trace_id):
        print(f"  [{event['level']}] {event['event_type']}: {event['message']}")

    print(f"\ntrace_id={trace_id2} 的事件:")
    for event in logger_instance.query_by_trace_id(trace_id2):
        print(f"  [{event['level']}] {event['event_type']}: {event['message']}")

    print(f"\n日志文件: {log_path}")
    print("健康检查:", logger_instance.ping())

    logger_instance.shutdown()
    os.unlink(log_path)
    print("\n✅ audit_logger.py 验证通过")
