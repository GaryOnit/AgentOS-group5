"""任务串行、只读并发和协作式取消的外部行为测试。"""

import threading
import unittest
from concurrent.futures import CancelledError

from group5.coordinator.task_scheduler import TaskCancelled, TaskScheduler
from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase


class TestTaskScheduler(unittest.TestCase):
    """验证共享桌面调度边界。"""

    def test_gui_and_mutation_tasks_share_single_serial_queue(self) -> None:
        """GUI任务未结束前文件变更任务不得开始。"""
        scheduler = TaskScheduler()
        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        def first(token):
            """阻塞第一个GUI任务直到测试释放。"""
            first_started.set()
            release_first.wait(timeout=2)
            token.checkpoint()
            return "first"

        def second(token):
            """记录第二个变更任务开始。"""
            second_started.set()
            return "second"

        first_handle = scheduler.submit("gui-1", "gui", first)
        self.assertTrue(first_started.wait(timeout=1))
        second_handle = scheduler.submit("mutation-1", "mutation", second)
        self.assertFalse(second_started.wait(timeout=0.1))
        release_first.set()

        try:
            self.assertEqual(first_handle.future.result(timeout=1), "first")
            self.assertEqual(second_handle.future.result(timeout=1), "second")
        finally:
            scheduler.shutdown(wait=True)

    def test_read_only_tasks_can_run_concurrently(self) -> None:
        """两个只读任务应在任一任务释放前同时开始。"""
        scheduler = TaskScheduler(read_only_workers=2)
        first_started = threading.Event()
        second_started = threading.Event()
        release = threading.Event()

        def first(token):
            """记录第一个只读任务并等待释放。"""
            first_started.set()
            release.wait(timeout=2)
            return 1

        def second(token):
            """记录第二个只读任务并等待释放。"""
            second_started.set()
            release.wait(timeout=2)
            return 2

        first_handle = scheduler.submit("read-1", "read_only", first)
        second_handle = scheduler.submit("read-2", "read_only", second)
        try:
            self.assertTrue(first_started.wait(timeout=1))
            self.assertTrue(second_started.wait(timeout=1))
            release.set()
            self.assertEqual(first_handle.future.result(timeout=1), 1)
            self.assertEqual(second_handle.future.result(timeout=1), 2)
        finally:
            scheduler.shutdown(wait=True)

    def test_queued_mutation_can_be_cancelled_without_running(self) -> None:
        """仍在队列中的变更任务取消后不得调用业务函数。"""
        scheduler = TaskScheduler()
        first_started = threading.Event()
        release_first = threading.Event()
        second_called = threading.Event()

        def first(token):
            """占用串行执行槽。"""
            first_started.set()
            release_first.wait(timeout=2)
            return "first"

        def second(token):
            """若被调用则标记测试失败条件。"""
            second_called.set()
            return "second"

        first_handle = scheduler.submit("mutation-first", "mutation", first)
        self.assertTrue(first_started.wait(timeout=1))
        second_handle = scheduler.submit("mutation-second", "mutation", second)
        self.assertTrue(scheduler.cancel("mutation-second"))
        release_first.set()
        first_handle.future.result(timeout=1)

        try:
            with self.assertRaises(CancelledError):
                second_handle.future.result(timeout=1)
            self.assertFalse(second_called.is_set())
        finally:
            scheduler.shutdown(wait=True)

    def test_running_task_stops_at_cooperative_checkpoint(self) -> None:
        """运行任务收到取消后应在下一个检查点抛出TaskCancelled。"""
        scheduler = TaskScheduler()
        started = threading.Event()
        continue_to_checkpoint = threading.Event()

        def operation(token):
            """等待取消后进入安全检查点。"""
            started.set()
            continue_to_checkpoint.wait(timeout=2)
            token.checkpoint()
            return "unexpected"

        handle = scheduler.submit("running-task", "mutation", operation)
        self.assertTrue(started.wait(timeout=1))
        self.assertTrue(scheduler.cancel("running-task"))
        continue_to_checkpoint.set()

        try:
            with self.assertRaises(TaskCancelled):
                handle.future.result(timeout=1)
        finally:
            scheduler.shutdown(wait=True)

    def test_coordinator_cancel_stops_before_next_stage(self) -> None:
        """规划运行期间取消后不得继续控件检测和第三组执行。"""
        planning_started = threading.Event()
        release_planning = threading.Event()

        class _BlockingGroup2:
            """等待测试释放后返回组合计划的v1第二组。"""

            contract_version = "1.0"

            def plan(self, request: dict) -> dict:
                """记录规划开始并等待取消请求。"""
                planning_started.set()
                release_planning.wait(timeout=2)
                return {"steps": [], "elements": {}}

        class _UnexpectedGroup3:
            """若被调用则暴露错误的第三组替身。"""

            contract_version = "1.0"

            def __init__(self) -> None:
                """初始化调用标记。"""
                self.called = False

            def execute(self, request: dict) -> dict:
                """记录不应发生的执行调用。"""
                self.called = True
                return {"status": "completed", "completed_step_ids": []}

        group3 = _UnexpectedGroup3()
        coordinator = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=AuditLogger(),
        )
        coordinator.register("group2", _BlockingGroup2())
        coordinator.register("group3", group3)
        task = {
            "contract_version": "1.0",
            "task_trace_id": "scheduled-task",
            "attempt_id": "scheduled-attempt",
            "timestamp": "2026-09-08T08:00:00+00:00",
            "intent": {
                "category": "应用控制",
                "action": "open",
                "target": "Files",
                "params": {},
                "raw_text": "打开文件管理器",
            },
        }
        handle = coordinator.submit_task(task, "gui")
        self.assertTrue(planning_started.wait(timeout=1))
        self.assertTrue(coordinator.cancel_task("scheduled-task"))
        release_planning.set()
        try:
            result = handle.future.result(timeout=2)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["error"]["code"], "E5004")
            self.assertFalse(group3.called)
        finally:
            coordinator.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
