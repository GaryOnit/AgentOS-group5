"""SQLite任务事实存储的外部行为测试。"""

import os
import sqlite3
import tempfile
import threading
import unittest

from group5.storage import SQLiteStore, resolve_database_path
from group5.audit.audit_logger import AuditLogger
from group5.coordinator.system_coordinator import SystemCoordinator
from group5.knowledge.rag_kb import RAGKnowledgeBase
from group5.mocks.mock_modules import MockGroup2Planner


def _task(attempt_id: str = "attempt-001") -> dict:
    """
    构造可持久化的v1任务信封。

    Args:
        attempt_id: 当前尝试ID。

    Returns:
        测试任务信封。
    """
    return {
        "contract_version": "1.0",
        "task_trace_id": "sqlite-task-001",
        "attempt_id": attempt_id,
        "timestamp": "2026-09-07T08:00:00+00:00",
        "intent": {
            "category": "应用控制",
            "action": "open",
            "target": "Files",
            "params": {},
            "raw_text": "打开文件管理器",
        },
    }


class TestSQLiteStore(unittest.TestCase):
    """验证任务、阶段、工具、事务、重启和环境隔离。"""

    def setUp(self) -> None:
        """在D盘测试临时目录下创建隔离数据库目录。"""
        temp_root = os.environ.get("TEMP") or os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)

    def tearDown(self) -> None:
        """删除本用例创建的临时数据库。"""
        self.temp_dir.cleanup()

    def test_environment_paths_are_separate(self) -> None:
        """production、demo和test必须解析到不同文件。"""
        paths = {
            resolve_database_path(self.temp_dir.name, environment)
            for environment in ("production", "demo", "test")
        }
        self.assertEqual(len(paths), 3)

    def test_task_trace_contains_attempt_stage_and_tool(self) -> None:
        """公开查询应聚合一次任务的完整基础关系。"""
        path = resolve_database_path(self.temp_dir.name, "test")
        store = SQLiteStore(path, "test")
        try:
            store.record_task_envelope(_task(), "running")
            store.record_stage_summaries(
                "attempt-001",
                [
                    {
                        "name": "security",
                        "status": "completed",
                        "latency_ms": 1.5,
                        "attempts": 1,
                    }
                ],
            )
            store.record_tool_call(
                "tool-001",
                "attempt-001",
                "file_manager",
                "completed",
                {"path": "/home/user"},
                {"success": True},
            )

            trace = store.get_task_trace("sqlite-task-001")
            self.assertEqual(trace["task"]["environment"], "test")
            self.assertEqual(len(trace["attempts"]), 1)
            self.assertEqual(trace["stages"][0]["stage_name"], "security")
            self.assertEqual(trace["tool_calls"][0]["tool_name"], "file_manager")
            self.assertEqual(
                trace["tool_calls"][0]["arguments_json"]["path"],
                "/home/user",
            )
        finally:
            store.close()

    def test_file_database_survives_restart(self) -> None:
        """关闭并重新创建存储后仍应查询到任务。"""
        path = resolve_database_path(self.temp_dir.name, "test")
        first = SQLiteStore(path, "test")
        first.record_task_envelope(_task(), "running")
        first.close()

        second = SQLiteStore(path, "test")
        try:
            trace = second.get_task_trace("sqlite-task-001")
            self.assertIsNotNone(trace)
            self.assertTrue(second.ping()["healthy"])
        finally:
            second.close()

    def test_failed_foreign_key_write_rolls_back(self) -> None:
        """不存在attempt的阶段写入应失败且不污染后续合法事务。"""
        path = resolve_database_path(self.temp_dir.name, "test")
        store = SQLiteStore(path, "test")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_stage_summaries(
                    "missing-attempt",
                    [
                        {
                            "name": "security",
                            "status": "completed",
                            "latency_ms": 1.0,
                            "attempts": 1,
                        }
                    ],
                )
            store.record_task_envelope(_task(), "running")
            self.assertIsNotNone(store.get_task_trace("sqlite-task-001"))
        finally:
            store.close()

    def test_concurrent_attempt_writes_are_serializable(self) -> None:
        """多个线程写同一任务的不同attempt时不应丢失记录。"""
        path = resolve_database_path(self.temp_dir.name, "test")
        store = SQLiteStore(path, "test")
        errors = []

        def write_attempt(index: int) -> None:
            """
            写入一个独立尝试。

            Args:
                index: 用于生成唯一attempt_id的序号。
            """
            try:
                store.record_task_envelope(_task(f"attempt-{index:03d}"), "running")
            except Exception as exc:  # pragma: no cover - 失败时由主线程断言
                errors.append(exc)

        threads = [threading.Thread(target=write_attempt, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        try:
            trace = store.get_task_trace("sqlite-task-001")
            self.assertEqual(errors, [])
            self.assertEqual(len(trace["attempts"]), 5)
        finally:
            store.close()

    def test_coordinator_records_v1_task_and_stages(self) -> None:
        """注入SQLite后，核心入口应持久化最终状态和阶段摘要。"""
        path = resolve_database_path(self.temp_dir.name, "test")
        store = SQLiteStore(path, "test")
        coordinator = SystemCoordinator(
            rag_kb=RAGKnowledgeBase(),
            audit_logger=AuditLogger(),
            state_store=store,
        )
        coordinator.register("group2", MockGroup2Planner())
        # legacy MockGroup3会请求工具，因此本用例移除其工具提示，确保只验证
        # 任务和阶段写入，不混入工具中介行为。
        class _NoToolExecutor:
            """返回不含工具请求的兼容执行器。"""

            def execute(self, plan: dict) -> dict:
                """返回最小成功结果。"""
                return {"status": "success", "output": "done"}

        coordinator.register("group3", _NoToolExecutor())
        try:
            result = coordinator.orchestrate_task(_task())
            self.assertTrue(result["success"])
        finally:
            coordinator.shutdown(wait=True)

        reopened = SQLiteStore(path, "test")
        try:
            trace = reopened.get_task_trace("sqlite-task-001")
            self.assertEqual(trace["task"]["status"], "completed")
            self.assertGreaterEqual(len(trace["stages"]), 4)
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
